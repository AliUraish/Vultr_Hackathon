"""The investigator's lab. Each instrument call is one experiment row, run as jobs on the VM B workers.

  reproduce  replay the capsule N times with the recorded policy: identical trajectory hashes, matching live
  isolate    delta debugging over the recorded inputs: the smallest set of events that still causes it
  what_if    the exact incident with extra rules in force: avoided, still happens, or something new breaks
  stress     a fix against N variants of the incident vs today's policy: robustness and throughput cost
  tune       stress at a range of settings of one fix: the safety/throughput curve, cheapest safe setting
  render     one variant with frames, to watch in the viewer

Experiments, their worker jobs and their results all live in Postgres, so every number an
investigation reports can be traced to the simulations behind it and re-run.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict

import asyncpg

from replay_core import capsule as caps
from replay_core import experiments as xp
from replay_core.policy import PolicyError, normalize

from .db import log_event
from .runtime import Runtime

log = logging.getLogger("replay.lab")

MIN_ROBUSTNESS = 0.95   # a fix must keep at least this share of stress variants safe
DEFAULT_VARIANTS = 30
MAX_VARIANTS = 60
CHUNK = 15              # variants per worker job, so both workers share a stress test
TIMEOUT = 180.0


class LabError(ValueError):
    pass


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


class Lab:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._waiters: set[asyncio.Event] = set()
        self._blobs: OrderedDict[int, dict] = OrderedDict()
        self._locks: dict[tuple, asyncio.Lock] = {}

    def wake(self) -> None:
        """A worker delivered a result: let every waiting experiment re-check."""
        for e in self._waiters:
            e.set()

    # ------------------------------------------------------------ plumbing

    async def blob(self, cid: int) -> dict:
        if cid in self._blobs:
            self._blobs.move_to_end(cid)
            return self._blobs[cid]
        async with self.rt.pool.acquire() as c:
            blob = await c.fetchval("SELECT blob FROM capsules WHERE id = $1", cid)
        if blob is None:
            raise LabError(f"no capsule {cid}")
        self._blobs[cid] = blob
        while len(self._blobs) > 8:
            self._blobs.popitem(last=False)
        return blob

    async def _start(self, cid: int, kind: str, params: dict, inv: int | None) -> int:
        async with self.rt.pool.acquire() as c, c.transaction():
            eid = await c.fetchval("INSERT INTO experiments (capsule_id, investigation_id, kind, params) "
                                   "VALUES ($1, $2, $3, $4) RETURNING id", cid, inv, kind, params)
            await log_event(c, "experiment.started", {"experiment": eid, "capsule": cid, "kind": kind,
                                                      "investigation": inv, "params": params})
        self.rt.hub.publish("experiment", {"id": eid, "capsule_id": cid, "kind": kind, "status": "running",
                                           "investigation_id": inv, "params": params})
        return eid

    async def _finish(self, eid: int, cid: int, kind: str, inv: int | None, t0: float, *, result: dict | None = None,
                      summary: str | None = None, sims: int = 0, error: str | None = None) -> None:
        ms = int((time.monotonic() - t0) * 1000)
        status = "error" if error else "done"
        async with self.rt.pool.acquire() as c, c.transaction():
            await c.execute("UPDATE experiments SET status = $2, result = $3, summary = $4, sims = $5, "
                            "duration_ms = $6, error = $7, finished_at = now() WHERE id = $1",
                            eid, status, result, summary, sims, ms, error)
            if inv is not None and sims:
                await c.execute("UPDATE investigations SET sims = sims + $2 WHERE id = $1", inv, sims)
            await log_event(c, "experiment.done", {"experiment": eid, "capsule": cid, "kind": kind,
                                                   "investigation": inv, "status": status, "summary": summary,
                                                   "sims": sims, "duration_ms": ms, "error": error})
        self.rt.hub.publish("experiment", {"id": eid, "capsule_id": cid, "kind": kind, "status": status,
                                           "investigation_id": inv, "summary": summary, "sims": sims,
                                           "duration_ms": ms})

    async def _jobs(self, cid: int, eid: int, kind: str, specs: list[dict | None], *,
                    rules: list[str] | None = None, control_failures: list | None = None) -> list[int]:
        async with self.rt.pool.acquire() as c, c.transaction():
            return [await c.fetchval(
                "INSERT INTO replays (capsule_id, experiment_id, kind, policy_rules, control_failures, spec) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id", cid, eid, kind, rules, control_failures, spec)
                for spec in specs]

    async def _await(self, ids: list[int], timeout: float = TIMEOUT) -> list[asyncpg.Record]:
        deadline = time.monotonic() + timeout
        ev = asyncio.Event()
        self._waiters.add(ev)
        try:
            while True:
                ev.clear()
                async with self.rt.pool.acquire() as c:
                    rows = await c.fetch("SELECT * FROM replays WHERE id = ANY($1::int[]) ORDER BY id", ids)
                if all(r["status"] in ("done", "error") for r in rows):
                    bad = [r for r in rows if r["status"] == "error"]
                    if bad:
                        raise LabError(f"simulation {bad[0]['id']} failed: {bad[0]['error']}")
                    return rows
                left = deadline - time.monotonic()
                if left <= 0:
                    raise LabError("the VM B workers did not finish in time (are they running?)")
                try:
                    await asyncio.wait_for(ev.wait(), timeout=min(1.0, left))
                except asyncio.TimeoutError:
                    pass
        finally:
            self._waiters.discard(ev)

    async def _run(self, cid: int, kind: str, params: dict, inv: int | None, body) -> dict:
        """Create the experiment row, run `body(eid)` -> (result, summary, sims), record the outcome."""
        eid = await self._start(cid, kind, params, inv)
        t0 = time.monotonic()
        try:
            result, summary, sims = await body(eid)
        except (LabError, PolicyError) as exc:
            await self._finish(eid, cid, kind, inv, t0, error=str(exc))
            raise LabError(str(exc)) from exc
        except Exception as exc:
            log.exception("experiment %s", eid)
            await self._finish(eid, cid, kind, inv, t0, error=f"{type(exc).__name__}: {exc}")
            raise LabError(f"experiment failed: {exc}") from exc
        await self._finish(eid, cid, kind, inv, t0, result=result, summary=summary, sims=sims)
        return {"experiment": eid, "summary": summary, "sims": sims, **result}

    async def _control_failures(self, cid: int) -> list | None:
        async with self.rt.pool.acquire() as c:
            return await c.fetchval("SELECT failures FROM replays WHERE capsule_id = $1 AND kind = 'reproduce' "
                                     "AND status = 'done' ORDER BY id LIMIT 1", cid)

    @staticmethod
    def _chunks(variants: list[dict]) -> list[list[dict]]:
        return [variants[i:i + CHUNK] for i in range(0, len(variants), CHUNK)]

    async def _variant_runs(self, cid: int, eid: int, rules: list[str], variants: list[dict]) -> list[int]:
        return await self._jobs(cid, eid, "variants", [{"rules": rules, "variants": ch} for ch in self._chunks(variants)])

    @staticmethod
    def _collect(rows: list[asyncpg.Record]) -> list[dict]:
        return [run for r in rows for run in (r["result"] or {}).get("runs", [])]

    # ------------------------------------------------------------ instruments

    async def reproduce(self, cid: int, times: int = 3, inv: int | None = None) -> dict:
        times = max(1, min(int(times), 5))
        blob = await self.blob(cid)

        async def body(eid: int):
            rows = await self._await(await self._jobs(cid, eid, "verify", [None] * times))
            runs = [{"replay": r["id"], "worker": r["worker"], "outcome": r["outcome"], "hash": r["trajectory_hash"],
                     "matches_live": r["matches_live"], "first_divergence": r["first_divergence"],
                     "ms": r["duration_ms"]} for r in rows]
            same = len({r["hash"] for r in runs}) == 1
            live = all(r["matches_live"] for r in runs)
            hit = all(r["outcome"] == "reproduced" for r in runs)
            workers = sorted({r["worker"] for r in runs if r["worker"]})
            f = blob["failure"]
            if same and live and hit:
                summary = (f"{times}/{times} replays on VM B ({', '.join(workers)}) reproduced the {f['type']} of "
                           f"{f['robot']} at t{f['tick']}: identical trajectory hash {runs[0]['hash'][:12]}, "
                           f"matching the live run tick for tick")
            else:
                div = next((r["first_divergence"] for r in runs if r["first_divergence"]), None)
                summary = (f"NOT deterministic: {sum(r['outcome'] == 'reproduced' for r in runs)}/{times} reproduced"
                           + (f", first divergence at t{div}" if div else "")
                           + ("" if same else ", trajectory hashes differ"))
            return ({"runs": runs, "identical": same, "matches_live": live, "reproduced": hit,
                     "hash": runs[0]["hash"]}, summary, times)

        return await self._run(cid, "reproduce", {"times": times}, inv, body)

    async def isolate(self, cid: int, budget: int = 90, inv: int | None = None) -> dict:
        budget = max(10, min(int(budget), 150))

        async def body(eid: int):
            rows = await self._await(await self._jobs(cid, eid, "isolate", [{"budget": budget}]))
            res = dict(rows[0]["result"] or {})
            res.pop("sims", None)
            if not res.get("ok"):
                return res, f"could not isolate: {res.get('reason')}", res.get("tested", 0)
            summary = f"{res['summary']} ({res['tested']} simulations)"
            if res["minimal"] and len(res["minimal"]) < res["total"]:
                summary += ": " + "; ".join(m["input"] for m in res["minimal"])
            return res, summary, res["tested"]

        return await self._run(cid, "isolate", {"budget": budget}, inv, body)

    async def what_if(self, cid: int, rules: list[str], inv: int | None = None) -> dict:
        extra = normalize(rules)
        if not extra:
            raise LabError("what_if needs at least one rule")
        blob = await self.blob(cid)
        recorded = caps.policy_at(blob, blob["failure"]["tick"])
        full = recorded + [r for r in extra if r not in recorded]

        async def body(eid: int):
            ctrl = await self._control_failures(cid)
            rows = await self._await(await self._jobs(cid, eid, "whatif", [None], rules=full, control_failures=ctrl))
            r = rows[0]
            first = (r["result"] or {}).get("first_failure")
            f = blob["failure"]
            what = " + ".join(extra)
            if r["outcome"] == "avoided":
                summary = f"with {what}: the {f['type']} does not happen"
            elif r["outcome"] == "reproduced":
                at = first["tick"] if first else f["tick"]
                shift = (at - f["tick"]) / 10
                summary = (f"with {what}: the {f['type']} still happens at t{at}"
                           + (f" ({shift:+.1f} s vs the incident)" if shift else " (same tick)"))
            else:
                news = ", ".join(sorted({f"{x['type']} {x['robot']}" for x in r["new_failures"] or []}))
                summary = f"with {what}: the {f['type']} is avoided but something new breaks ({news})"
            warn = [f"{w['type']} {w['robot']}" for w in r["warnings"] or []]
            if warn:
                summary += f"; later warnings: {', '.join(warn)}"
            return ({"rules": extra, "outcome": r["outcome"], "replay": r["id"], "first_failure": first,
                     "new_failures": r["new_failures"] or [], "warnings": r["warnings"] or []}, summary, 1)

        return await self._run(cid, "what_if", {"rules": extra}, inv, body)

    async def _baseline(self, cid: int, n: int, seed: int, inv: int | None) -> tuple[list[dict], list[dict], int]:
        """Variants and their outcome under today's (recorded) policy; computed once per capsule, n, seed."""
        blob = await self.blob(cid)
        variants = xp.make_variants(blob, n, seed)
        rules = xp.baseline_rules(blob)
        params = {"n": n, "seed": seed, "rules": rules}
        lock = self._locks.setdefault((cid, n, seed, tuple(rules)), asyncio.Lock())
        async with lock:
            async with self.rt.pool.acquire() as c:
                done = await c.fetchval("SELECT result FROM experiments WHERE capsule_id = $1 AND kind = 'baseline' "
                                        "AND status = 'done' AND params = $2 ORDER BY id DESC LIMIT 1", cid, params)
            if done:
                return variants, done["runs"], 0

            async def body(eid: int):
                runs = self._collect(await self._await(await self._variant_runs(cid, eid, rules, variants)))
                bad = sum(1 for r in runs if r["fired"] and r["target"])
                placed = sum(1 for r in runs if r["fired"])
                return ({"runs": runs, "variants": [{"id": v["id"], "label": v["label"]} for v in variants]},
                        f"today's policy: the hazard causes the {blob['failure']['type']} in {bad} of {placed} "
                        f"variants", len(runs))

            out = await self._run(cid, "baseline", params, inv, body)
            return variants, out["runs"], out["sims"]

    def _clamp_n(self, n: int | None) -> int:
        return max(6, min(int(n or DEFAULT_VARIANTS), MAX_VARIANTS))

    async def stress(self, cid: int, fix: str, n: int | None = None, seed: int = 0, inv: int | None = None) -> dict:
        n = self._clamp_n(n)
        fix = normalize([fix])[0]
        blob = await self.blob(cid)
        rules = xp.baseline_rules(blob)
        cand = rules + ([fix] if fix not in rules else [])

        async def body(eid: int):
            variants = xp.make_variants(blob, n, seed)
            (_, base, bsims), rows = await asyncio.gather(
                self._baseline(cid, n, seed, inv),
                self._jobs(cid, eid, "variants", [{"rules": cand, "variants": ch} for ch in self._chunks(variants)]))
            runs = self._collect(await self._await(rows))
            agg = xp.aggregate(variants, base, runs)
            safe = agg["placed"] - agg["still_fails"] - agg["introduced"]
            summary = (f"{fix}: {safe}/{agg['placed']} variants safe ({_pct(agg['robustness'])}; today "
                       f"{_pct(agg['baseline_robustness'])}), prevents {agg['prevented']} of {agg['baseline_failed']}"
                       f" failures, {agg['introduced']} new; throughput cost {agg['cost_pct']}%")
            return ({"fix": fix, "n": n, "seed": seed, "passes": (agg["robustness"] or 0) >= MIN_ROBUSTNESS,
                     "min_robustness": MIN_ROBUSTNESS, "baseline_sims": bsims, **agg}, summary, len(runs))

        return await self._run(cid, "stress", {"fix": fix, "n": n, "seed": seed}, inv, body)

    async def tune(self, cid: int, fix: str, values: list[float] | None = None, n: int | None = None,
                   seed: int = 0, inv: int | None = None) -> dict:
        n = self._clamp_n(n)
        points = xp.sweep(fix, values)
        blob = await self.blob(cid)
        rules = xp.baseline_rules(blob)

        async def body(eid: int):
            variants = xp.make_variants(blob, n, seed)
            specs, owner = [], []
            for p in points:
                cand = rules + ([p] if p not in rules else [])
                for ch in self._chunks(variants):
                    specs.append({"rules": cand, "variants": ch})
                    owner.append(p)
            (_, base, bsims), ids = await asyncio.gather(self._baseline(cid, n, seed, inv),
                                                         self._jobs(cid, eid, "variants", specs))
            rows = await self._await(ids)
            by_point: dict[str, list[dict]] = {p: [] for p in points}
            for p, r in zip(owner, rows):
                by_point[p].extend((r["result"] or {}).get("runs", []))
            out = []
            for p in points:
                agg = xp.aggregate(variants, base, by_point[p])
                out.append({"fix": p, "setting": xp.setting(p), **agg})
            best = xp.pick_setting(out, MIN_ROBUSTNESS)
            base_rob = out[0]["baseline_robustness"] if out else None
            if best:
                summary = (f"cheapest setting that keeps ≥{_pct(MIN_ROBUSTNESS)} of variants safe: {best['fix']} "
                           f"({_pct(best['robustness'])} safe, throughput cost {best['cost_pct']}%; today "
                           f"{_pct(base_rob)} safe)")
            else:
                top = max(out, key=lambda p: p["robustness"] or 0)
                summary = (f"no setting of {points[0].split('(')[0]} reaches {_pct(MIN_ROBUSTNESS)}; best is "
                           f"{top['fix']} at {_pct(top['robustness'])}")
            curve = ", ".join(f"{p['setting']}: {_pct(p['robustness'])}/{p['cost_pct']}%" for p in out)
            return ({"points": out, "best": best["fix"] if best else None, "min_robustness": MIN_ROBUSTNESS,
                     "baseline_robustness": base_rob, "n": n, "seed": seed, "curve": curve, "baseline_sims": bsims},
                    summary, sum(len(v) for v in by_point.values()))

        return await self._run(cid, "tune", {"fix": fix, "values": values, "n": n, "seed": seed, "points": points},
                               inv, body)

    async def render(self, eid: int, variant_id: int, which: str = "fix", fix: str | None = None) -> dict:
        """Frames for one variant of a stress/tune experiment ('base' = today's policy, 'fix' = with the fix)."""
        async with self.rt.pool.acquire() as c:
            e = await c.fetchrow("SELECT * FROM experiments WHERE id = $1", eid)
        if e is None or e["kind"] not in ("stress", "tune", "baseline"):
            raise LabError("render needs a stress or tune experiment")
        p = e["params"]
        blob = await self.blob(e["capsule_id"])
        variants = xp.make_variants(blob, p["n"], p.get("seed", 0))
        v = next((x for x in variants if x["id"] == variant_id), None)
        if v is None:
            raise LabError(f"no variant {variant_id}")
        rules = xp.baseline_rules(blob)
        if which == "fix":
            chosen = fix or p.get("fix")
            if not chosen:
                raise LabError("which fix? pass one of the tuned settings")
            chosen = normalize([chosen])[0]
            rules = rules + ([chosen] if chosen not in rules else [])
        spec = {"rules": rules, "variant": v}
        async with self.rt.pool.acquire() as c:
            done = await c.fetchrow("SELECT id, frames, result FROM replays WHERE capsule_id = $1 AND kind = 'render' "
                                    "AND status = 'done' AND spec = $2 ORDER BY id DESC LIMIT 1",
                                    e["capsule_id"], spec)
        if done is None:
            rows = await self._await(await self._jobs(e["capsule_id"], eid, "render", [spec]), timeout=60)
            done = rows[0]
        return {"replay": done["id"], "variant": v, "rules": rules, "run": (done["result"] or {}).get("run"),
                "frames": done["frames"] or []}

    # ------------------------------------------------------------ reading back

    async def history(self, cid: int, inv: int | None = None) -> list[dict]:
        async with self.rt.pool.acquire() as c:
            rows = await c.fetch("SELECT id, investigation_id, kind, params, status, summary, sims, duration_ms, error, "
                                 "created_at, finished_at FROM experiments WHERE capsule_id = $1 "
                                 "AND ($2::int IS NULL OR investigation_id = $2) ORDER BY id", cid, inv)
        return [dict(r) for r in rows]

    async def get(self, eid: int) -> dict:
        async with self.rt.pool.acquire() as c:
            e = await c.fetchrow("SELECT * FROM experiments WHERE id = $1", eid)
            if e is None:
                raise LabError("no such experiment")
            jobs = await c.fetch("SELECT id, kind, status, worker, duration_ms, error FROM replays "
                                 "WHERE experiment_id = $1 ORDER BY id", eid)
        return {**dict(e), "jobs": [dict(j) for j in jobs]}


__all__ = ["Lab", "LabError", "MIN_ROBUSTNESS"]
