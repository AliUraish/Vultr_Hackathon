"""Operations copilot: answers an operator's question about one robot or the whole site.

The answer is grounded in live data only: the latest frame from the fleet, the robot's recent
events from the log, its sensors (derived from the frame exactly as the sim senses), its job,
order and load, the asset register, open incidents and the fleet policy. It explains; it never
acts. Every question and answer is written to the event log.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from typing import Any

import httpx

from replay_core.world import CELL, DEFAULT_CLEARANCE, PALLET_HALF, ROBOT_HALF, SENSOR_RANGE, TICK_HZ, W

from . import enterprise, policies
from .db import log_event
from .diagnosis import _pick_model, _stopping_m
from .runtime import Runtime

log = logging.getLogger("replay.assistant")

MAX_PER_MINUTE = 12
_recent: dict[str, deque] = defaultdict(deque)

RULES = (f"Traffic rules the fleet runs (deterministic, not the model): robots plan the shortest safe route (A*, "
         f"turns cost extra, other robots' parked cells avoided), reserve up to 2 cells ahead in a shared table and "
         f"never enter a cell another robot holds. A robot that meets a held cell stops and waits. If two robots "
         f"wait on each other (a standoff), the arbiter rule decides: the higher id yields after {2} s and "
         f"re-routes, the lower id tries after {4} s if that failed; a robot blocked by a parked robot re-routes "
         f"after 2 s; one queued behind a moving robot after 6 s. Forward sensor range {SENSOR_RANGE / 1000} m; "
         f"robots brake at 1 m/s^2 and keep {DEFAULT_CLEARANCE / 1000} m clearance.")


class AskError(ValueError):
    pass


def throttle(user: str) -> None:
    q, now = _recent[user], time.monotonic()
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= MAX_PER_MINUTE:
        raise AskError("too many questions this minute; try again shortly")
    q.append(now)


# ---------------------------------------------------------------- live sensors

_DIRS = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}


def sensors(frame: dict, rid: str, rules: list[str]) -> dict:
    """What the robot senses and must respect right now (same geometry as the sim)."""
    r = next((x for x in frame["robots"] if x["id"] == rid), None)
    if r is None:
        return {}
    cell = [r["x"] // CELL, r["y"] // CELL]
    zones = sorted(W.cell_zones.get((cell[0], cell[1]), ()))
    dx, dy = _DIRS.get(r["dir"], (1, 0))
    nearest: tuple[int, str] | None = None
    for p in frame.get("pallets", []):
        px, py = p["cell"][0] * CELL + CELL // 2, p["cell"][1] * CELL + CELL // 2
        ahead, side = (px - r["x"]) * dx + (py - r["y"]) * dy, abs((px - r["x"]) * dy - (py - r["y"]) * dx)
        if ahead > 0 and side < ROBOT_HALF + PALLET_HALF:
            gap = ahead - ROBOT_HALF - PALLET_HALF
            if nearest is None or gap < nearest[0]:
                nearest = (gap, f"pallet at c{p['cell'][0]}_{p['cell'][1]}")
    for o in frame["robots"]:
        if o["id"] == rid:
            continue
        ahead, side = (o["x"] - r["x"]) * dx + (o["y"] - r["y"]) * dy, abs((o["x"] - r["x"]) * dy - (o["y"] - r["y"]) * dx)
        if ahead > 0 and side < 2 * ROBOT_HALF:
            gap = ahead - 2 * ROBOT_HALF
            if nearest is None or gap < nearest[0]:
                nearest = (gap, f"robot {o['id']}")
    caps = []
    for rule in rules:
        if rule.startswith("speed_cap("):
            zone, v = [a.strip() for a in rule[len("speed_cap("):-1].split(",")]
            if zone in zones:
                caps.append(float(v))
    stop = _stopping_m(r["v"]) + DEFAULT_CLEARANCE / 1000
    return {
        "cell": f"c{cell[0]}_{cell[1]}", "zones": [z for z in zones if z != "racks"] or ["open floor"],
        "heading": r["dir"], "speed_mps": round(r["v"] * TICK_HZ / 1000, 2), "status": r["st"],
        "forward_obstacle": ({"what": nearest[1], "gap_m": round(max(0, nearest[0]) / 1000, 2),
                              "within_sensor_range": nearest[0] <= SENSOR_RANGE} if nearest and nearest[0] < 3000 else None),
        "stopping_distance_m": round(stop, 2), "sensor_range_m": SENSOR_RANGE / 1000,
        "can_stop_within_sensor_range": stop <= SENSOR_RANGE / 1000,
        "speed_limit_mps": min(caps) if caps else 1.2,
        "reserved_cells": [f"c{c[0]}_{c[1]}" for c in r.get("res", [])],
        "waiting_on": r.get("wait_on") or None,
        "route_next_cells": [f"c{c[0]}_{c[1]}" for c in r.get("path", [])[:8]],
        "current_step": r.get("op"), "goal_cell": f"c{r['goal'][0]}_{r['goal'][1]}" if r.get("goal") else None,
        "odometer_m": round(r.get("odo", 0) / 1000, 1),
        "carrying_skus": r.get("carry", []),
    }


def _brief(payload: dict) -> dict:
    keep = ("robot", "on", "to", "with", "rule", "waited", "after", "cell", "slot", "sku", "job", "dock", "ok",
            "expected", "actual", "zone", "reason", "v", "type", "scenario")
    return {k: v for k, v in (payload or {}).items() if k in keep}


# ---------------------------------------------------------------- context

async def context(rt: Runtime, scope: str, rid: str | None) -> dict:
    site = rt.site or {}
    frame = rt.frame or {"robots": [], "pallets": [], "restricted": []}
    catalog = site.get("catalog", {})

    def item(sku: str) -> str:
        it = catalog.get(sku.replace("SKU-", ""))
        return f"{it['name']} ({it['sku']}, bin {it['slot']})" if it else sku

    async with rt.pool.acquire() as c:
        pol = await policies.current(c)
        kpi = await enterprise.kpis(rt)
        jobs = {j["id"]: dict(j) for j in await c.fetch(
            "SELECT j.id, j.lines, j.dock, j.status, j.order_id, o.value, o.priority, cu.name AS customer, cu.tier "
            "FROM jobs j LEFT JOIN orders o ON o.id = j.order_id LEFT JOIN customers cu ON cu.id = o.customer_id "
            "WHERE j.status IN ('pending', 'assigned', 'active')")}
        incidents = [dict(r) for r in await c.fetch(
            "SELECT id, type, robot_id, tick, status, note FROM failures ORDER BY id DESC LIMIT 8")]
        radio = [dict(r) for r in await c.fetch(
            "SELECT tick, robot_id, type, payload FROM events WHERE run_id = $1 AND type IN "
            "('sim.wait', 'sim.standoff', 'sim.yield', 'input.chaos', 'dispatch') ORDER BY id DESC LIMIT 25", rt.run_id)]
        mine = [dict(r) for r in await c.fetch(
            "SELECT tick, type, payload FROM events WHERE run_id = $1 AND robot_id = $2 "
            "ORDER BY id DESC LIMIT 40", rt.run_id, rid)] if rid else []

    def activity(r: dict, job: dict | None) -> str:
        """What the robot is doing, in words, from the same state the 3D view reads."""
        goal = r.get("goal")
        slot = next((s for s, v in W.slots.items() if goal and list(v["access"]) == list(goal)), None)
        dock = next((d for d, c in W.docks.items() if goal and list(c) == list(goal)), None)
        st = r["st"]
        if st in ("picking", "scanning"):
            return f"{st} {item(W.slots[slot]['sku']) if slot else 'an item'} at bin {slot or '?'} (stopped while it works)"
        if st == "dropping":
            return f"unloading at {dock or 'the dock'}"
        if st == "waiting":
            return f"holding: {r.get('wait_on') or 'another robot'} has the next cell reserved (traffic rule, not a fault)"
        if st == "held":
            return "holding outside a closed zone until it reopens"
        if st == "blocked":
            return "stopped: an obstacle blocks its route"
        if st == "estop":
            return "emergency stop"
        if st == "moving" and slot:
            return f"driving to bin {slot} to pick {item(W.slots[slot]['sku'])}"
        if st == "moving" and dock:
            return f"driving to {dock} to deliver {len(r.get('carry', []))} item(s)"
        if st == "moving":
            return "driving back to its charge bay" if goal and tuple(goal) in set(map(tuple, W.homes)) else "repositioning"
        return "idle, waiting for a job" if not job else f"about to start {job['id']}"

    def robot_view(r: dict) -> dict:
        job = jobs.get(r.get("job") or "")
        asset = next((a for a in site.get("robots", []) if a["id"] == r["id"]), {})
        return {
            "id": r["id"], "model": asset.get("model"), "serial": asset.get("serial"), "firmware": asset.get("firmware"),
            "payload_kg": asset.get("payload_kg"), "status": r["st"], "activity": activity(r, job),
            **sensors(frame, r["id"], list(pol["rules"])),
            "carrying": [item(s) for s in r.get("carry", [])],
            "job": ({"id": job["id"], "picks": [item(line["sku"]) for line in job["lines"]], "dock": job["dock"],
                     "order": job["order_id"], "customer": job["customer"], "tier": job["tier"],
                     "order_value_usd": float(job["value"]) if job["value"] is not None else None,
                     "priority": job["priority"]} if job else None),
        }

    t = rt.last_tick
    out: dict[str, Any] = {
        "now": {"tick": t, "sim_seconds": round(t / TICK_HZ, 1)},
        "site": {"company": site.get("facility", {}).get("company"), "facility": site.get("name"),
                 "code": site.get("code"), "city": site.get("facility", {}).get("city"),
                 "docks": site.get("carriers")},
        "kpis": {k: v for k, v in kpi.items() if k != "last_safety_incident"},
        "policy": {"version": pol["version"], "rules": pol["rules"], "signed": bool(pol.get("signature"))},
        "traffic_rules": RULES,
        "open_incidents": [i for i in incidents if i["status"] not in ("fixed", "dismissed", "lost")],
        "recent_incidents": incidents,
        "pallets_on_floor": [f"c{p['cell'][0]}_{p['cell'][1]}" for p in frame.get("pallets", [])],
        "closed_zones": [z["zone"] for z in frame.get("restricted", [])],
    }
    if scope == "robot" and rid:
        r = next((x for x in frame["robots"] if x["id"] == rid), None)
        if r is None:
            raise AskError(f"no live robot {rid}")
        out["robot"] = robot_view(r)
        out["robot_recent_events"] = [{"t": e["tick"], "type": e["type"], **_brief(e["payload"])} for e in reversed(mine)]
        out["other_robots"] = [{"id": o["id"], "status": o["st"], "cell": f"c{o['x'] // CELL}_{o['y'] // CELL}"}
                               for o in frame["robots"] if o["id"] != rid]
    else:
        out["fleet"] = [robot_view(r) for r in frame["robots"]]
    out["fleet_coordination_recent"] = [{"t": e["tick"], "robot": e["robot_id"], "type": e["type"], **_brief(e["payload"])}
                                        for e in reversed(radio)]
    return out


INSTRUCTIONS = """You are the operations copilot for a robotic fulfilment centre. An operator is watching the live
3D floor and asks about {what}. Answer from the live data provided (JSON): robot telemetry and sensors, the event log,
jobs, orders, incidents and fleet policy. Be specific and concrete: robot ids, cells (cX_Y), zones, products, orders,
customers, seconds. A robot's `activity` field says what it is doing right now; trust it over inferring from raw
fields. Explain traffic decisions using the traffic rules given. Keep it under 110 words unless asked for
more; plain sentences, at most a short list. If the data does not say, say so; never invent readings. You explain; you
cannot command robots."""


# ---------------------------------------------------------------- answering

def _fallback(ctx: dict) -> str:
    r = ctx.get("robot")
    if r:
        bits = [f"{r['id']} is {r['status']} at {r['cell']} ({', '.join(r['zones'])}), {r['speed_mps']} m/s heading {r['heading']}."]
        if r.get("waiting_on"):
            bits.append(f"It is holding for {r['waiting_on']}, which has the next cell reserved.")
        if r.get("job"):
            j = r["job"]
            bits.append(f"Job {j['id']} for {j['customer'] or 'an internal move'}: picks {', '.join(j['picks'])} to {j['dock']}.")
        if r["carrying"]:
            bits.append(f"Carrying {', '.join(r['carrying'])}.")
        return " ".join(bits) + " (Model not configured: this is a rule-based summary.)"
    k = ctx["kpis"]
    return (f"{k['fleet_busy']} of {k['fleet_size']} robots busy, {k['orders_per_hour']} orders/hour, "
            f"{k['open']} orders in progress, {k['incidents_open']} open incidents. (Model not configured.)")


async def answer(rt: Runtime, question: str, scope: str, rid: str | None, history: list[dict],
                 transport: httpx.AsyncBaseTransport | None = None) -> dict:
    ctx = await context(rt, scope, rid)
    s = rt.settings
    if not s.inference_enabled:
        return {"answer": _fallback(ctx), "source": "rules", "tokens": 0}
    what = f"robot {rid}" if scope == "robot" else "the whole site"
    turns = [{"role": h["role"], "content": str(h["text"])[:800]} for h in history[-6:]
             if h.get("role") in ("user", "assistant") and h.get("text")]
    data = "LIVE DATA\n" + json.dumps(ctx, default=str)
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {s.inference_key}"}, timeout=60.0,
                                 transport=transport) as http:
        model = await _pick_model(http, s)
        if s.inference_provider == "openai":
            body: dict[str, Any] = {"model": model, "instructions": INSTRUCTIONS.format(what=what),
                                    "input": [{"role": "user", "content": data}, *turns,
                                              {"role": "user", "content": question}],
                                    "max_output_tokens": 1800, "reasoning": {"effort": "low"}}
            r = await http.post(f"{s.inference_url}/responses", json=body)
            if r.status_code == 400 and "reasoning" in r.text:
                body.pop("reasoning")
                r = await http.post(f"{s.inference_url}/responses", json=body)
            r.raise_for_status()
            out = r.json()
            text = "".join(c.get("text", "") for it in out.get("output") or [] if it.get("type") == "message"
                           for c in it.get("content") or [] if c.get("type") == "output_text")
            u = out.get("usage") or {}
            tokens = int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
        else:
            body = {"model": model, "temperature": 0.2, "max_tokens": 500,
                    "messages": [{"role": "system", "content": INSTRUCTIONS.format(what=what)},
                                 {"role": "user", "content": data}, *turns, {"role": "user", "content": question}]}
            r = await http.post(f"{s.inference_url}/chat/completions", json=body)
            r.raise_for_status()
            out = r.json()
            text = out["choices"][0]["message"]["content"] or ""
            u = out.get("usage") or {}
            tokens = int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
    return {"answer": text.strip() or "No answer.", "source": f"{s.inference_provider}:{model}", "tokens": tokens}


async def ask(rt: Runtime, user: str, question: str, scope: str, rid: str | None, history: list[dict]) -> dict:
    throttle(user)
    t0 = time.monotonic()
    try:
        res = await answer(rt, question, scope, rid, history)
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        if isinstance(exc, AskError):
            raise
        log.warning("assistant failed: %s", exc)
        ctx = await context(rt, scope, rid)
        res = {"answer": _fallback(ctx).replace("(Model not configured: this is a rule-based summary.)",
                                                "(The model is unavailable right now: rule-based summary.)"),
               "source": "rules", "tokens": 0}
    res["ms"] = int((time.monotonic() - t0) * 1000)
    async with rt.pool.acquire() as c:
        await log_event(c, "assistant.asked", {"by": user, "scope": scope, "robot": rid, "question": question[:300],
                                               "answer": res["answer"][:600], "source": res["source"],
                                               "tokens": res["tokens"], "ms": res["ms"]},
                        run_id=rt.run_id, tick=rt.last_tick, robot_id=rid)
    return res


__all__ = ["AskError", "ask", "context", "sensors"]
