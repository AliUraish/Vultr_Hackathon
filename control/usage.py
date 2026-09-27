"""Vultr Serverless Inference usage: every model call the control plane makes, metered and priced.

Each call site records (purpose, prompt tokens, completion tokens) from the response's own `usage`
block. Totals are kept per minute and per purpose in Postgres (ai_usage), so the spend survives
restarts, and streamed to the browsers every few seconds: calls, tokens, dollars, the current
$/hour and what that projects to over 60 hours.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any

log = logging.getLogger("replay.usage")

PRICE_IN = float(os.environ.get("INFERENCE_PRICE_IN_PER_M", "0.10"))     # USD per million prompt tokens
PRICE_OUT = float(os.environ.get("INFERENCE_PRICE_OUT_PER_M", "0.25"))   # USD per million completion tokens
TARGET_USD_H = float(os.environ.get("AI_TARGET_USD_PER_HOUR", "2.0"))    # what the governor steers the spend toward
HORIZON_H = 60


# Vultr Serverless Inference list prices (USD per million tokens: prompt, completion), from the inference usage API
PRICES = {"deepseek-v4-flash-0731": (.10, .25), "deepseek-v4.1-flash": (.15, .60), "glm-5.3-flash": (.10, .35),
          "laguna-s-2.1": (.09, .18), "mimo-v2.6-flash-rl": (.10, .25), "minimax-m3": (.20, .90),
          "qwen3.8-27b": (.15, 1.00), "qwen3.8-flash-next": (.10, .20)}


def cost(tin: int, tout: int, model: str | None = None) -> float:
    pin, pout = PRICES.get(model or "", (PRICE_IN, PRICE_OUT))
    return tin * pin / 1e6 + tout * pout / 1e6


class Usage:
    def __init__(self) -> None:
        self.started = time.time()
        self.by: dict[str, dict[str, float]] = {}          # purpose -> totals since this process started
        self.base = {"calls": 0, "tokens": 0, "usd": 0.0, "since": None}   # everything before, from Postgres
        self.recent: deque[tuple[float, float, int, int, str]] = deque()     # (t, usd, calls, tokens, purpose) for rates
        self.pending: dict[tuple[str, str], list[float]] = {}               # (minute, purpose) -> [calls, errors, in, out, usd]
        self.pool = None
        self.hub = None
        self.target = TARGET_USD_H

    # ---------------------------------------------------------------- recording
    def record(self, purpose: str, tin: int, tout: int, ok: bool = True, ms: int = 0, model: str | None = None) -> None:
        usd = cost(tin, tout, model)
        b = self.by.setdefault(purpose, {"calls": 0, "errors": 0, "tin": 0, "tout": 0, "usd": 0.0, "ms": 0})
        b["calls"] += 1
        b["errors"] += 0 if ok else 1
        b["tin"] += tin
        b["tout"] += tout
        b["usd"] += usd
        b["ms"] += ms
        now = time.time()
        self.recent.append((now, usd, 1, tin + tout, purpose))
        minute = time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime(now))
        row = self.pending.setdefault((minute, purpose), [0, 0, 0, 0, 0.0])
        row[0] += 1
        row[1] += 0 if ok else 1
        row[2] += tin
        row[3] += tout
        row[4] += usd

    def failed(self, purpose: str) -> None:
        self.record(purpose, 0, 0, ok=False)

    # ---------------------------------------------------------------- reading
    def _window(self, seconds: float, exclude: tuple[str, ...] = ()) -> tuple[float, int, int, float]:
        now = time.time()
        while self.recent and self.recent[0][0] < now - 3600:
            self.recent.popleft()
        span = min(seconds, max(1.0, now - self.started))
        usd = calls = tokens = 0
        for t, u, c, k, p in self.recent:
            if t >= now - span and p not in exclude:
                usd += u
                calls += c
                tokens += k
        return usd, calls, tokens, span

    def usd_per_hour(self, seconds: float = 300, exclude: tuple[str, ...] = ()) -> float:
        usd, _, _, span = self._window(seconds, exclude)
        return usd * 3600 / span

    def usd_total(self) -> float:
        return self.base["usd"] + sum(b["usd"] for b in self.by.values())

    def snapshot(self) -> dict[str, Any]:
        usd5, calls5, tok5, span5 = self._window(300)
        usd60, _, _, span60 = self._window(3600)
        session = {k: sum(b[k] for b in self.by.values()) for k in ("calls", "errors", "tin", "tout", "usd")}
        rate = usd5 * 3600 / span5
        return {
            "provider": "Vultr Serverless Inference",
            "price_in_per_m": PRICE_IN, "price_out_per_m": PRICE_OUT,
            "usd_total": round(self.base["usd"] + session["usd"], 4),
            "usd_session": round(session["usd"], 4),
            "calls_total": int(self.base["calls"] + session["calls"]),
            "tokens_total": int(self.base["tokens"] + session["tin"] + session["tout"]),
            "since": self.base["since"] or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started)),
            "usd_per_hour": round(rate, 3),
            "usd_per_hour_60m": round(usd60 * 3600 / span60, 3),
            "calls_per_min": round(calls5 * 60 / span5, 1),
            "tokens_per_min": int(tok5 * 60 / span5),
            "projected_60h": round(rate * HORIZON_H, 2),
            "target_usd_per_hour": self.target,
            "errors": int(session["errors"]),
            "by": {p: {"calls": int(b["calls"]), "tokens": int(b["tin"] + b["tout"]), "usd": round(b["usd"], 4),
                       "errors": int(b["errors"]), "avg_ms": int(b["ms"] / max(1, b["calls"] - b["errors"]))}
                   for p, b in sorted(self.by.items(), key=lambda kv: -kv[1]["usd"])},
        }

    # ---------------------------------------------------------------- persistence + streaming
    async def attach(self, pool, hub) -> None:
        self.pool, self.hub = pool, hub
        try:
            async with pool.acquire() as c:
                row = await c.fetchrow("SELECT COALESCE(SUM(calls), 0) AS calls, COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens, "
                                       "COALESCE(SUM(usd), 0) AS usd, MIN(minute) AS since FROM ai_usage")
            self.base = {"calls": int(row["calls"]), "tokens": int(row["tokens"]), "usd": float(row["usd"]),
                         "since": row["since"].strftime("%Y-%m-%dT%H:%M:%SZ") if row["since"] else None}
        except Exception:
            log.exception("loading AI usage totals")

    async def flush(self) -> None:
        if not self.pending or self.pool is None:
            return
        rows, self.pending = self.pending, {}
        async with self.pool.acquire() as c:
            await c.executemany(
                "INSERT INTO ai_usage (minute, purpose, calls, errors, tokens_in, tokens_out, usd) "
                "VALUES ($1::timestamptz, $2, $3, $4, $5, $6, $7) ON CONFLICT (minute, purpose) DO UPDATE SET "
                "calls = ai_usage.calls + EXCLUDED.calls, errors = ai_usage.errors + EXCLUDED.errors, "
                "tokens_in = ai_usage.tokens_in + EXCLUDED.tokens_in, tokens_out = ai_usage.tokens_out + EXCLUDED.tokens_out, "
                "usd = ai_usage.usd + EXCLUDED.usd",
                [(_ts(m), p, int(v[0]), int(v[1]), int(v[2]), int(v[3]), v[4]) for (m, p), v in rows.items()])

    async def loop(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(2)
            n += 1
            if self.hub is not None:
                self.hub.publish("usage", self.snapshot())
            if n % 5 == 0:
                try:
                    await self.flush()
                except Exception:
                    log.exception("flushing AI usage")

    async def history(self, hours: int = 60) -> list[dict]:
        """Spend per hour for the chart, newest last."""
        if self.pool is None:
            return []
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT date_trunc('hour', minute) AS h, SUM(calls) AS calls, SUM(tokens_in + tokens_out) AS tokens, "
                                 "SUM(usd) AS usd FROM ai_usage WHERE minute > now() - make_interval(hours => $1) "
                                 "GROUP BY 1 ORDER BY 1", hours)
        return [{"hour": r["h"].strftime("%Y-%m-%dT%H:00Z"), "calls": int(r["calls"]), "tokens": int(r["tokens"]),
                 "usd": round(float(r["usd"]), 4)} for r in rows]


def _ts(minute: str):
    from datetime import datetime, timezone
    return datetime.strptime(minute, "%Y-%m-%dT%H:%M:00Z").replace(tzinfo=timezone.utc)


USAGE = Usage()

__all__ = ["USAGE", "Usage", "cost"]
