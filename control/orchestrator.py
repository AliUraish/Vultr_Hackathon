"""The workflow engine: deterministic rules that move each failure through the pipeline.

    open --(T+2 s recorded)--> reproducing --(replay matches live)--> diagnosing (the investigator
      runs its experiments) --> trials --(fix avoids the incident and is robust across variants)-->
      regression --(suite still clean)--> awaiting_approval
      --(human approves)--> fixed   (+ policy vN+1 pushed to the fleet, capsule joins the suite)

Every transition is idempotent and is written to the event log in the same
transaction, so the sweep can safely re-run after a restart.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time

import asyncpg

from replay_core import capsule as caps

from . import diagnosis, enterprise, policies
from .db import log_event
from .investigator import MAX_SIMS, MAX_STEPS, DbRecorder, Investigation
from .lab import MIN_ROBUSTNESS
from .runtime import Runtime
from .simnode_client import SimNodeError

log = logging.getLogger("replay.orchestrator")

TERMINAL = ("fixed", "no_fix", "not_reproducible", "dismissed", "lost")
SUITE_SIZE = 10
STUCK_SECONDS = 120


class WorkflowError(ValueError):
    pass


def _robustness(h: asyncpg.Record | dict) -> dict:
    """Stress-test numbers shown with the gate (empty for fixes that did not come from an investigation)."""
    ev = h["evidence"] or {}
    if not ev:
        return {}
    return {"robustness": ev.get("robustness"), "cost_pct": ev.get("cost_pct"), "min_robustness": MIN_ROBUSTNESS,
            "stress_experiment": ev.get("experiment")}


CAUSES = {"collision": "pallet_drop", "wrong_item": "mislabel_bin", "zone_breach": "worker_in_aisle"}


async def original_chaos(c: asyncpg.Connection, run_id: str, fail_type: str, fail_tick: int,
                         capsule_inputs: list | None) -> dict | None:
    """The canned-scenario input behind a failure: from the capsule, else the event log (the cause
    can predate the capsule window, e.g. a bin mislabeled long before the wrong item reached a dock)."""
    want = CAUSES.get(fail_type)
    chaos = [i for _, ins in (capsule_inputs or []) for i in ins
             if i.get("kind") == "chaos" and i.get("scenario") not in (None, "clear_floor")]
    match = next((i for i in reversed(chaos) if i.get("scenario") == want), None)
    if match:
        return match
    if want:
        found = await c.fetchval(
            "SELECT payload FROM events WHERE run_id = $1 AND type = 'input.chaos' AND tick <= $2 "
            "AND tick >= $2 - 6000 AND payload->>'scenario' = $3 ORDER BY tick DESC LIMIT 1",
            run_id, fail_tick, want)
        if found:
            return found
    return chaos[-1] if chaos else None


class Orchestrator:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._wake = asyncio.Event()
        self._diagnosing: set[int] = set()
        self._last_policy_push = 0.0

    def kick(self) -> None:
        self._wake.set()

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.sweep()
            except Exception:
                log.exception("sweep failed")

    # ------------------------------------------------------------ helpers

    async def _status(self, c: asyncpg.Connection, f: asyncpg.Record | dict, status: str,
                      note: str | None = None) -> None:
        await c.execute("UPDATE failures SET status = $2, note = COALESCE($3, note), updated_at = now() "
                        "WHERE id = $1", f["id"], status, note)
        await log_event(c, "failure.status", {"failure": f["id"], "from": f["status"], "to": status, "note": note},
                        run_id=f["run_id"], tick=f["tick"], robot_id=f["robot_id"])
        self.rt.hub.publish("failure", {"id": f["id"], "status": status, "note": note})

    async def _enqueue(self, c: asyncpg.Connection, capsule_id: int, kind: str, *, hypothesis_id: int | None = None,
                       rules: list[str] | None = None, version: int | None = None,
                       control_rules: list[str] | None = None, control_failures: list | None = None) -> int:
        rid = await c.fetchval(
            "INSERT INTO replays (capsule_id, hypothesis_id, kind, policy_version, policy_rules, control_rules, "
            "control_failures) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            capsule_id, hypothesis_id, kind, version, rules, control_rules, control_failures)
        await log_event(c, "replay.queued", {"replay": rid, "capsule": capsule_id, "kind": kind,
                                             "hypothesis": hypothesis_id, "rules": rules})
        self.rt.hub.publish("replay", {"id": rid, "capsule_id": capsule_id, "kind": kind, "status": "queued"})
        return rid

    async def _hyp_status(self, c: asyncpg.Connection, h: asyncpg.Record, status: str, gate: dict | None) -> None:
        await c.execute("UPDATE hypotheses SET status = $2, gate = $3 WHERE id = $1", h["id"], status, gate)
        await log_event(c, "hypothesis.status", {"hypothesis": h["id"], "fix": h["fix_dsl"], "from": h["status"],
                                                 "to": status, "gate": gate})
        self.rt.hub.publish("hypothesis", {"id": h["id"], "capsule_id": h["capsule_id"], "status": status, "gate": gate})

    # ------------------------------------------------------------ sweep

    async def sweep(self) -> None:
        async with self.rt.pool.acquire() as c:
            await c.execute("UPDATE replays SET status = 'queued', worker = NULL, started_at = NULL "
                            f"WHERE status = 'running' AND started_at < now() - interval '{STUCK_SECONDS} seconds'")
            ids = [r["id"] for r in await c.fetch(
                "SELECT id FROM failures WHERE status <> ALL($1::text[]) ORDER BY id", list(TERMINAL))]
        for fid in ids:
            try:
                await self.advance(fid)
            except Exception:
                log.exception("advancing failure %s", fid)
        await self._reconcile_policy()

    async def advance(self, fid: int) -> None:
        async with self.rt.pool.acquire() as c:
            f = await c.fetchrow("SELECT * FROM failures WHERE id = $1", fid)
        if f is None:
            return
        st = f["status"]
        if st == "open":
            await self._cut(f)
        elif st == "reproducing":
            await self._check_reproduced(f)
        elif st == "diagnosing":
            if fid not in self._diagnosing:  # e.g. after a restart
                self._start_diagnosis(fid)
        elif st in ("trials", "awaiting_approval"):
            await self._evaluate(f)

    # ------------------------------------------------------------ 1. capsule cut

    async def _cut(self, f: asyncpg.Record) -> None:
        pool = self.rt.pool
        async with pool.acquire() as c:
            last = await c.fetchval("SELECT last_tick FROM runs WHERE id = $1", f["run_id"]) or 0
            if last < f["tick"] + caps.POST_TICKS:
                age = (dt.datetime.now(dt.timezone.utc) - f["created_at"]).total_seconds()
                if f["run_id"] != self.rt.run_id and age > 60:
                    async with c.transaction():
                        await self._status(c, f, "lost", "the sim restarted before T+2 s was recorded")
                return
            job = f["detail"].get("job")
            if job is None:
                frame = await c.fetchval("SELECT frame FROM ticks WHERE run_id = $1 AND tick = $2",
                                         f["run_id"], f["tick"])
                if frame:
                    job = next((r["job"] for r in frame["robots"] if r["id"] == f["robot_id"]), None)
            job_start = await c.fetchval("SELECT assigned_tick FROM jobs WHERE id = $1 AND run_id = $2",
                                         job, f["run_id"]) if job else None
            start, end = caps.window(f["tick"], job_start)
            snaps = await c.fetch("SELECT id, tick FROM snapshots WHERE run_id = $1 ORDER BY tick", f["run_id"])
            try:
                snap_tick = caps.pick_snapshot([s["tick"] for s in snaps], start)
            except caps.CapsuleError as exc:
                async with c.transaction():
                    await self._status(c, f, "lost", str(exc))
                return
            snap = await c.fetchrow("SELECT id, state FROM snapshots WHERE run_id = $1 AND tick = $2",
                                    f["run_id"], snap_tick)
            records = await c.fetch("SELECT tick, state_hash AS hash, inputs, frame FROM ticks "
                                    "WHERE run_id = $1 AND tick > $2 AND tick <= $3 ORDER BY tick",
                                    f["run_id"], snap_tick, end)
        spec = {"type": f["type"], "robot": f["robot_id"], "tick": f["tick"]}
        try:
            blob = await asyncio.to_thread(caps.build, f["run_id"], snap["state"], [dict(r) for r in records], spec)
        except caps.CapsuleError as exc:
            async with pool.acquire() as c, c.transaction():
                await self._status(c, f, "lost", str(exc))
            return
        async with pool.acquire() as c:
            cause = await original_chaos(c, f["run_id"], f["type"], f["tick"], blob["inputs"])
        scenario = cause.get("scenario") if cause else None
        size = len(json.dumps(blob))
        async with pool.acquire() as c, c.transaction():
            cid = await c.fetchval(
                "INSERT INTO capsules (failure_id, run_id, snapshot_id, start_tick, end_tick, fail_tick, hash, "
                "blob, size_bytes) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                "ON CONFLICT (failure_id) DO NOTHING RETURNING id",
                f["id"], f["run_id"], snap["id"], blob["start"], blob["end"], f["tick"], blob["hash"], blob, size)
            if cid is None:
                return
            await c.execute("UPDATE failures SET scenario = $2 WHERE id = $1", f["id"], scenario)
            await log_event(c, "capsule.cut", {"failure": f["id"], "capsule": cid, "hash": blob["hash"],
                                               "start": blob["start"], "end": blob["end"], "bytes": size,
                                               "snapshot_tick": snap_tick},
                            run_id=f["run_id"], tick=f["tick"], robot_id=f["robot_id"])
            await self._enqueue(c, cid, "reproduce")
            await self._status(c, f, "reproducing")

    # ------------------------------------------------------------ 2. reproduction

    async def _check_reproduced(self, f: asyncpg.Record) -> None:
        async with self.rt.pool.acquire() as c:
            r = await c.fetchrow(
                "SELECT r.* FROM replays r JOIN capsules k ON k.id = r.capsule_id WHERE k.failure_id = $1 "
                "AND r.kind = 'reproduce' AND r.status IN ('done', 'error') ORDER BY r.id LIMIT 1", f["id"])
            if r is None:
                return
            async with c.transaction():
                if r["status"] == "done" and r["outcome"] == "reproduced" and r["matches_live"]:
                    await self._status(c, f, "diagnosing")
                else:
                    await self._status(c, f, "not_reproducible",
                                       r["error"] or f"replay diverged at tick {r['first_divergence']}")
                    return
        self._start_diagnosis(f["id"])

    # ------------------------------------------------------------ 3. diagnosis

    def _start_diagnosis(self, fid: int) -> None:
        self._diagnosing.add(fid)
        asyncio.create_task(self._diagnose(fid))

    async def _diagnose(self, fid: int) -> None:
        pool = self.rt.pool
        try:
            async with pool.acquire() as c:
                f = await c.fetchrow("SELECT * FROM failures WHERE id = $1", fid)
                k = await c.fetchrow("SELECT id, blob FROM capsules WHERE failure_id = $1", fid)
                repro = await c.fetchrow("SELECT failures FROM replays WHERE capsule_id = $1 AND kind = 'reproduce' "
                                         "AND status = 'done' ORDER BY id LIMIT 1", k["id"])
                events = await c.fetch("SELECT tick, type, payload FROM events WHERE run_id = $1 AND tick <= $2 "
                                       "AND (type LIKE 'sim.%' OR type LIKE 'input.%' OR type = 'dispatch') "
                                       "ORDER BY id DESC LIMIT 50", f["run_id"], f["tick"])
                cur = await policies.current(c)
                business = await enterprise.business_context(c, self.rt.site, f)
            ctx = diagnosis.context(k["blob"], dict(f), [dict(e) for e in reversed(events)], list(cur["rules"]))
            recorded = caps.policy_at(k["blob"], f["tick"])  # what the fleet ran when it failed
            ctx["recorded_policy"] = recorded
            if business:
                ctx["business"] = business
            hyps, source, notes, inv = await self._investigate(f, k["id"], ctx)
            async with pool.acquire() as c, c.transaction():
                rnd = (await c.fetchval("SELECT max(round) FROM hypotheses WHERE capsule_id = $1", k["id"]) or 0) + 1
                await c.execute("UPDATE hypotheses SET status = 'superseded' WHERE capsule_id = $1 "
                                "AND status IN ('trial', 'regression', 'ready')", k["id"])
                await log_event(c, "diagnosis", {"failure": fid, "capsule": k["id"], "round": rnd, "source": source,
                                                 "hypotheses": [{k2: h[k2] for k2 in ("cause", "fix", "rationale")}
                                                                for h in hyps],
                                                 "notes": notes, "investigation": inv},
                                run_id=f["run_id"], tick=f["tick"], robot_id=f["robot_id"])
                for rank, h in enumerate(hyps, 1):
                    hid = await c.fetchval(
                        "INSERT INTO hypotheses (capsule_id, round, rank, cause, fix_dsl, rationale, source, status, "
                        "investigation_id, evidence) VALUES ($1, $2, $3, $4, $5, $6, $7, 'trial', $8, $9) RETURNING id",
                        k["id"], rnd, rank, h["cause"], h["fix"], h["rationale"], source, inv, h.get("evidence"))
                    rules = recorded + ([h["fix"]] if h["fix"] not in recorded else [])
                    await self._enqueue(c, k["id"], "trial", hypothesis_id=hid, rules=rules,
                                        control_failures=repro["failures"] if repro else None)
                f = await c.fetchrow("SELECT * FROM failures WHERE id = $1", fid)
                await self._status(c, f, "trials" if hyps else "no_fix",
                                   None if hyps else "the investigation produced no valid fix")
        except Exception:
            log.exception("diagnosis of failure %s", fid)
        finally:
            self._diagnosing.discard(fid)
            self.kick()

    async def _investigate(self, f: asyncpg.Record, cid: int, ctx: dict
                           ) -> tuple[list[dict], str, list[str], int | None]:
        """Run the incident investigator; (hypotheses, source, notes, investigation id).
        If it crashes outright, fall back to the one-shot diagnosis so the incident still moves."""
        rt = self.rt
        async with rt.pool.acquire() as c, c.transaction():
            await c.execute("UPDATE investigations SET status = 'error', error = 'interrupted', finished_at = now() "
                            "WHERE failure_id = $1 AND status = 'running'", f["id"])
            inv = await c.fetchval("INSERT INTO investigations (failure_id, capsule_id, source, budget) "
                                   "VALUES ($1, $2, 'starting', $3) RETURNING id",
                                   f["id"], cid, {"steps": MAX_STEPS, "sims": MAX_SIMS,
                                                  "min_robustness": MIN_ROBUSTNESS})
            await log_event(c, "investigation.started", {"investigation": inv, "failure": f["id"], "capsule": cid},
                            run_id=f["run_id"], tick=f["tick"], robot_id=f["robot_id"])
        rt.hub.publish("investigation", {"id": inv, "failure_id": f["id"], "capsule_id": cid, "status": "running"})
        assert rt.lab is not None
        investigation = Investigation(rt.settings, rt.lab, cid, ctx, DbRecorder(rt, inv, f["id"], cid))
        try:
            report = await investigation.run()
        except Exception as exc:
            log.exception("investigation %s", inv)
            async with rt.pool.acquire() as c:
                await c.execute("UPDATE investigations SET status = 'error', error = $2, finished_at = now() "
                                "WHERE id = $1", inv, f"{type(exc).__name__}: {exc}"[:500])
            rt.hub.publish("investigation", {"id": inv, "failure_id": f["id"], "status": "error"})
            hyps, source, notes = await diagnosis.diagnose(rt.settings, ctx)
            return hyps, source, notes + [f"investigation failed: {exc}"], None
        async with rt.pool.acquire() as c, c.transaction():
            await c.execute("UPDATE investigations SET status = 'done', source = $2, report = $3, finished_at = now() "
                            "WHERE id = $1", inv, report["source"], report)
            await log_event(c, "investigation.done", {
                "investigation": inv, "failure": f["id"], "capsule": cid, "source": report["source"],
                "root_cause": report["root_cause"], "fixes": [x["fix"] for x in report["fixes"]],
                "rejected": [x["fix"] for x in report["rejected"]], "steps": report["steps"],
                "simulations": report["simulations"], "tokens": report["tokens"]},
                run_id=f["run_id"], tick=f["tick"], robot_id=f["robot_id"])
        rt.hub.publish("investigation", {"id": inv, "failure_id": f["id"], "capsule_id": cid, "status": "done",
                                         "fixes": [x["fix"] for x in report["fixes"]]})
        hyps = [{"cause": report["root_cause"] or "see the investigation", "fix": x["fix"], "rationale": x["why"],
                 "evidence": x.get("stress")} for x in report["fixes"]]
        return hyps, report["source"], report["notes"], inv

    # ------------------------------------------------------------ 4-5. trials, regression, gate

    async def _evaluate(self, f: asyncpg.Record) -> None:
        async with self.rt.pool.acquire() as c, c.transaction():
            k = await c.fetchrow("SELECT id FROM capsules WHERE failure_id = $1", f["id"])
            hyps = await c.fetch("SELECT * FROM hypotheses WHERE capsule_id = $1 AND round = "
                                 "(SELECT max(round) FROM hypotheses WHERE capsule_id = $1) ORDER BY rank FOR UPDATE",
                                 k["id"])
            cur = await policies.current(c)
            for h in hyps:
                if h["status"] == "trial":
                    await self._after_trial(c, k["id"], h, cur)
                elif h["status"] == "regression":
                    await self._after_regression(c, h)
            states = [r["status"] for r in await c.fetch(
                "SELECT status FROM hypotheses WHERE capsule_id = $1 AND round = "
                "(SELECT max(round) FROM hypotheses WHERE capsule_id = $1)", k["id"])]
            if "ready" in states and f["status"] != "awaiting_approval":
                await self._status(c, f, "awaiting_approval")
            elif states and all(s in ("failed", "rejected") for s in states):
                await self._status(c, f, "no_fix", "no hypothesis survived its trial and the regression suite")

    async def _after_trial(self, c: asyncpg.Connection, capsule_id: int, h: asyncpg.Record, cur: dict) -> None:
        trial = await c.fetchrow("SELECT * FROM replays WHERE hypothesis_id = $1 AND kind = 'trial' "
                                 "ORDER BY id DESC LIMIT 1", h["id"])
        if trial is None or trial["status"] not in ("done", "error"):
            return
        if trial["status"] != "done" or trial["outcome"] != "avoided":
            await self._hyp_status(c, h, "failed", {"passed": False, "trial": trial["outcome"] or "error",
                                                    "trial_replay": trial["id"], **_robustness(h)})
            return
        if h["investigation_id"] is not None:  # the investigator's fixes must also hold up across variants
            rob = (h["evidence"] or {}).get("robustness")
            if rob is None or rob < MIN_ROBUSTNESS:
                await self._hyp_status(c, h, "failed", {
                    "passed": False, "trial": "avoided", "trial_replay": trial["id"], **_robustness(h),
                    "reason": "not stress-tested" if rob is None else
                    f"only {rob * 100:.0f}% of variants safe (needs {MIN_ROBUSTNESS * 100:.0f}%)"})
                return
        await self._start_regression(c, capsule_id, h, cur, trial["id"])

    async def _start_regression(self, c: asyncpg.Connection, capsule_id: int, h: asyncpg.Record | dict,
                                cur: dict, trial_id: int | None) -> None:
        suite = await c.fetch("SELECT id FROM capsules WHERE in_regression AND id <> $1 ORDER BY id DESC LIMIT $2",
                              capsule_id, SUITE_SIZE)
        gate = {"trial": "avoided", "trial_replay": trial_id, "policy_version": cur["version"],
                "suite": [s["id"] for s in suite], **_robustness(h)}
        if not suite:
            gate.update(passed=True, regression={"passed": 0, "total": 0, "failed": []})
            await self._hyp_status(c, h, "ready", gate)
            return
        rules = list(cur["rules"]) + ([h["fix_dsl"]] if h["fix_dsl"] not in cur["rules"] else [])
        for s in suite:
            await self._enqueue(c, s["id"], "regression", hypothesis_id=h["id"], rules=rules,
                                version=cur["version"], control_rules=list(cur["rules"]))
        await self._hyp_status(c, h, "regression", gate)

    async def _after_regression(self, c: asyncpg.Connection, h: asyncpg.Record) -> None:
        gate = dict(h["gate"] or {})
        regs = await c.fetch("SELECT id, capsule_id, status, outcome FROM replays WHERE hypothesis_id = $1 "
                             "AND kind = 'regression' AND policy_version = $2", h["id"], gate.get("policy_version"))
        if not regs or any(r["status"] not in ("done", "error") for r in regs):
            return
        failed = [r["capsule_id"] for r in regs if r["status"] != "done" or r["outcome"] != "avoided"]
        gate.update(passed=not failed, regression={"passed": len(regs) - len(failed), "total": len(regs),
                                                   "failed": failed})
        await self._hyp_status(c, h, "ready" if not failed else "rejected", gate)

    # ------------------------------------------------------------ 6. promotion (human)

    async def approve(self, hid: int, user: str) -> dict:
        stale: int | None = None
        async with self.rt.pool.acquire() as c, c.transaction():
            h = await c.fetchrow("SELECT * FROM hypotheses WHERE id = $1 FOR UPDATE", hid)
            if h is None:
                raise WorkflowError("no such hypothesis")
            if h["status"] != "ready":
                raise WorkflowError(f"hypothesis is {h['status']}, not ready: it must pass its trial and the "
                                    "regression suite first")
            k = await c.fetchrow("SELECT id, failure_id FROM capsules WHERE id = $1", h["capsule_id"])
            f = await c.fetchrow("SELECT * FROM failures WHERE id = $1 FOR UPDATE", k["failure_id"])
            cur = await policies.current(c)
            if (h["gate"] or {}).get("policy_version") != cur["version"]:
                # The fleet policy moved on since this fix was checked; re-check against the new one
                # (committed below), and make the operator approve again once it passes.
                await self._start_regression(c, k["id"], h, cur, (h["gate"] or {}).get("trial_replay"))
                stale = cur["version"]
        if stale is not None:
            self.kick()
            raise WorkflowError(f"policy is now v{stale}; re-running the regression suite against it first")
        async with self.rt.pool.acquire() as c, c.transaction():
            h = await c.fetchrow("SELECT * FROM hypotheses WHERE id = $1 FOR UPDATE", hid)
            if h is None or h["status"] != "ready":
                raise WorkflowError("hypothesis changed while approving; try again")
            f = await c.fetchrow("SELECT * FROM failures WHERE id = $1 FOR UPDATE", k["failure_id"])
            new = await policies.promote(c, h["fix_dsl"], user, k["id"], self.rt.signer)
            await c.execute("UPDATE hypotheses SET status = 'superseded' WHERE capsule_id = $1 AND id <> $2 "
                            "AND status IN ('trial', 'regression', 'ready')", k["id"], hid)
            await self._hyp_status(c, h, "approved", {**(h["gate"] or {}), "approved_by": user,
                                                      "policy_version": new["version"]})
            await c.execute("UPDATE capsules SET in_regression = true WHERE id = $1", k["id"])
            await log_event(c, "approval", {"hypothesis": hid, "fix": h["fix_dsl"], "by": user,
                                            "policy_version": new["version"], "capsule": k["id"]},
                            run_id=f["run_id"], tick=f["tick"], robot_id=f["robot_id"])
            repro = await c.fetchval("SELECT failures FROM replays WHERE capsule_id = $1 AND kind = 'reproduce' "
                                     "AND status = 'done' ORDER BY id LIMIT 1", k["id"])
            await self._enqueue(c, k["id"], "proof", rules=list(new["rules"]), version=new["version"],
                                control_failures=repro)
            await self._status(c, f, "fixed", f"policy v{new['version']}: {h['fix_dsl']}")
        self.rt.hub.publish("policy", {"version": new["version"], "rules": new["rules"]})
        await self.push_policy(new)
        return new

    async def push_policy(self, pol: dict) -> None:
        try:
            await self.rt.sim.policy(pol["version"], list(pol["rules"]), pol.get("signature"))
            self._last_policy_push = time.monotonic()
        except SimNodeError as exc:
            log.warning("policy push failed, will retry: %s", exc)

    async def _reconcile_policy(self) -> None:
        """The live fleet must run the latest approved policy; re-push if it reports an older one."""
        rt = self.rt
        if not rt.sim_online or rt.sim_policy_version is None or time.monotonic() - self._last_policy_push < 5:
            return
        async with rt.pool.acquire() as c:
            cur = await policies.current(c)
        if rt.sim_policy_version < cur["version"]:
            await self.push_policy(cur)

    # ------------------------------------------------------------ operator actions

    async def _failure_capsule(self, c: asyncpg.Connection, fid: int) -> tuple[asyncpg.Record, asyncpg.Record]:
        f = await c.fetchrow("SELECT * FROM failures WHERE id = $1", fid)
        k = await c.fetchrow("SELECT id FROM capsules WHERE failure_id = $1", fid)
        if f is None or k is None:
            raise WorkflowError("no capsule for this failure yet")
        return f, k

    async def replay_again(self, fid: int) -> int:
        async with self.rt.pool.acquire() as c, c.transaction():
            _, k = await self._failure_capsule(c, fid)
            return await self._enqueue(c, k["id"], "reproduce")

    async def rediagnose(self, fid: int) -> None:
        async with self.rt.pool.acquire() as c, c.transaction():
            f, _ = await self._failure_capsule(c, fid)
            if f["status"] not in ("trials", "awaiting_approval", "no_fix"):
                raise WorkflowError(f"cannot re-diagnose a failure that is {f['status']}")
            await self._status(c, f, "diagnosing")
        self._start_diagnosis(fid)

    async def dismiss(self, fid: int, user: str) -> None:
        async with self.rt.pool.acquire() as c, c.transaction():
            f = await c.fetchrow("SELECT * FROM failures WHERE id = $1", fid)
            if f is None:
                raise WorkflowError("no such failure")
            await self._status(c, f, "dismissed", f"dismissed by {user}")

    async def run_suite(self) -> int:
        """Check every regression capsule against the current fleet policy."""
        async with self.rt.pool.acquire() as c, c.transaction():
            cur = await policies.current(c)
            suite = await c.fetch("SELECT id FROM capsules WHERE in_regression ORDER BY id")
            for s in suite:
                repro = await c.fetchval("SELECT failures FROM replays WHERE capsule_id = $1 AND kind = 'reproduce' "
                                         "AND status = 'done' ORDER BY id LIMIT 1", s["id"])
                await self._enqueue(c, s["id"], "suite", rules=list(cur["rules"]), version=cur["version"],
                                    control_failures=repro)
        return len(suite)
