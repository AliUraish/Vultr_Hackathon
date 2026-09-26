"""Job intake, the rule-based dispatcher, and the demo job generator."""
from __future__ import annotations

import asyncio
import logging
import random
import time

from replay_core.dispatch import choose_robot, make_cmd, requirements
from replay_core.world import CELL, JOB_DEADLINE_TICKS, W

from .db import log_event
from .runtime import Runtime
from .simnode_client import SimNodeError

log = logging.getLogger("replay.fleet")

DISPATCH_RULE = "capable -> free -> fewest spare capabilities -> nearest to first pick -> lowest id"
INFLIGHT_SECONDS = 3.0
MAX_OPEN_AUTO_JOBS = 4


class JobError(ValueError):
    pass


async def create_job(rt: Runtime, slots: list[str], dock: str, source: str) -> dict:
    if not 1 <= len(slots) <= 3:
        raise JobError("a job picks 1 to 3 slots")
    bad = [s for s in slots if s not in W.slots]
    if bad:
        raise JobError(f"unknown slot {', '.join(bad)}: slots are A1-F8")
    if dock not in W.docks:
        raise JobError(f"unknown dock {dock}: docks are {', '.join(sorted(W.docks))}")
    lines = [{"slot": s, "sku": W.slots[s]["sku"], "cls": W.slots[s]["cls"]} for s in slots]
    kind = "multi" if len(lines) > 1 else "single"
    async with rt.pool.acquire() as c, c.transaction():
        seq = await c.fetchval("SELECT nextval('job_seq')")
        jid = f"J{seq}"
        row = await c.fetchrow(
            "INSERT INTO jobs (id, kind, lines, dock, status, run_id, created_tick, deadline_tick) "
            "VALUES ($1, $2, $3, $4, 'pending', $5, $6, $7) RETURNING *",
            jid, kind, lines, dock, rt.run_id, rt.last_tick, rt.last_tick + JOB_DEADLINE_TICKS)
        await log_event(c, "job.created", {"job": jid, "lines": lines, "dock": dock, "source": source},
                        run_id=rt.run_id, tick=rt.last_tick)
    job = dict(row)
    rt.hub.publish("job", job)
    return job


async def dispatch_once(rt: Runtime) -> None:
    if not rt.sim_online or rt.frame is None:
        return
    now = time.monotonic()
    rt.inflight = {r: t for r, t in rt.inflight.items() if now - t < INFLIGHT_SECONDS}
    busy = set(rt.inflight)
    async with rt.pool.acquire() as c:
        pending = await c.fetch("SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at LIMIT 20")
    for j in pending:
        created = j["created_tick"] if j["run_id"] == rt.run_id and j["created_tick"] is not None else rt.last_tick
        job = {"id": j["id"], "kind": j["kind"], "lines": j["lines"], "dock": j["dock"],
               "deadline": created + JOB_DEADLINE_TICKS}
        rid = choose_robot(rt.frame, job, busy)
        if rid is None:
            continue
        cmd = make_cmd(rid, job)
        try:
            res = await rt.sim.command(rid, cmd["steps"], cmd["job"])
        except SimNodeError as exc:
            log.warning("dispatch of %s failed: %s", j["id"], exc)
            return
        busy.add(rid)
        rt.inflight[rid] = now
        r = next(x for x in rt.frame["robots"] if x["id"] == rid)
        ax, ay = W.slots[job["lines"][0]["slot"]]["access"]
        reason = {"rule": DISPATCH_RULE, "needs": sorted(requirements(job)),
                  "distance_cells": abs(r["x"] // CELL - ax) + abs(r["y"] // CELL - ay)}
        async with rt.pool.acquire() as c, c.transaction():
            await c.execute("UPDATE jobs SET status = 'assigned', robot_id = $2, run_id = $3, "
                            "created_tick = $4, deadline_tick = $5 WHERE id = $1 AND status = 'pending'",
                            j["id"], rid, rt.run_id, created, job["deadline"])
            await c.execute("INSERT INTO assignments (job_id, robot_id, run_id, steps, input_id, reason) "
                            "VALUES ($1, $2, $3, $4, $5, $6)",
                            j["id"], rid, rt.run_id, cmd["steps"], res.get("input_id"), reason)
            await log_event(c, "dispatch", {"job": j["id"], "robot": rid, **reason},
                            run_id=rt.run_id, tick=rt.last_tick, robot_id=rid)
        rt.hub.publish("job", {"id": j["id"], "status": "assigned", "robot_id": rid})


def _random_job(rng: random.Random) -> tuple[list[str], str]:
    # Heavy (rack F) picks are rare: only R4 can lift them.
    pool = [s for s in sorted(W.slots) if not s.startswith("F") or rng.random() < 0.08]
    return rng.sample(pool, rng.choice([1, 1, 1, 2])), rng.choice(sorted(W.docks))


async def loop(rt: Runtime) -> None:
    rng = random.Random()
    last_gen = 0.0
    while True:
        await asyncio.sleep(1.0)
        try:
            if rt.auto_jobs and rt.sim_online and time.monotonic() - last_gen > 3.0:
                async with rt.pool.acquire() as c:
                    open_jobs = await c.fetchval(
                        "SELECT count(*) FROM jobs WHERE status IN ('pending', 'assigned', 'active')")
                if open_jobs < MAX_OPEN_AUTO_JOBS:
                    slots, dock = _random_job(rng)
                    await create_job(rt, slots, dock, "auto")
                    last_gen = time.monotonic()
            await dispatch_once(rt)
        except Exception:  # keep the fleet loop alive; the error is in the log
            log.exception("fleet loop")
