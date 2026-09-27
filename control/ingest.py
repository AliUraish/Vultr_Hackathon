"""Telemetry from the live sim: flight recorder, event log, failure detector, projections."""
from __future__ import annotations

import logging
import time

from fastapi import HTTPException

from replay_core.detector import detect
from replay_core.frames import ui_frame
from replay_core.world import MAP_HASH

from . import policies
from .db import log_event
from .runtime import Runtime

log = logging.getLogger("replay.ingest")


async def hello(rt: Runtime, body: dict) -> dict:
    """A sim node (re)started a run. Answer with the policy it must run."""
    run_id, seed = body["run_id"], int(body["seed"])
    if body.get("map") != MAP_HASH:
        raise HTTPException(409, "map mismatch: VM A and VM B must run the same replay_core")
    async with rt.pool.acquire() as c, c.transaction():
        pol = await policies.current(c)
        await c.execute("INSERT INTO runs (id, seed, map_hash, policy_version) VALUES ($1, $2, $3, $4) "
                        "ON CONFLICT (id) DO NOTHING", run_id, seed, MAP_HASH, pol["version"])
        await log_event(c, "run.started", {"seed": seed, "policy_version": pol["version"]}, run_id=run_id)
        # Work the previous run never finished goes back in the queue.
        await c.execute("UPDATE jobs SET status = 'pending', robot_id = NULL, created_tick = NULL "
                        "WHERE status IN ('assigned', 'active') AND run_id IS DISTINCT FROM $1", run_id)
    rt.run_id, rt.last_tick, rt.frame = run_id, 0, None
    return {"policy": {"version": pol["version"], "rules": pol["rules"], "signature": pol.get("signature")}}


async def telemetry(rt: Runtime, body: dict) -> dict:
    run = body["run"]
    run_id = run["id"]
    ticks: list[dict] = body.get("ticks", [])
    snaps: list[dict] = body.get("snapshots", [])
    new_failures: list[dict] = []
    alarms: list[tuple[int, dict]] = []

    async with rt.pool.acquire() as c, c.transaction():
        await c.execute("INSERT INTO runs (id, seed, map_hash) VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING",
                        run_id, int(run["seed"]), MAP_HASH)
        if ticks:
            await c.executemany(
                "INSERT INTO ticks (run_id, tick, state_hash, inputs, frame) VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT DO NOTHING",
                [(run_id, t["tick"], t["hash"], t["inputs"], t["frame"]) for t in ticks])
        if snaps:
            await c.executemany(
                "INSERT INTO snapshots (run_id, tick, state, state_hash) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT DO NOTHING",
                [(run_id, s["tick"], s["state"], s["hash"]) for s in snaps])

        pending: list[tuple] = []

        async def flush() -> None:
            if pending:
                await c.executemany("INSERT INTO events (run_id, tick, robot_id, type, payload) "
                                    "VALUES ($1, $2, $3, $4, $5)", pending)
                pending.clear()

        for t in ticks:
            tick = t["tick"]
            for inp in t["inputs"]:
                pending.append((run_id, tick - 1, inp.get("robot") or inp.get("target"),
                                f"input.{inp.get('kind')}", inp))
                if inp.get("kind") == "cmd" and inp.get("job"):
                    await c.execute(
                        "UPDATE jobs SET status = 'active', assigned_tick = $2, robot_id = $3, run_id = $4 "
                        "WHERE id = $1 AND status IN ('pending', 'assigned')",
                        inp["job"]["id"], tick - 1, inp["robot"], run_id)
                    await c.execute("UPDATE orders SET status = 'picking' WHERE job_id = $1 AND status = 'released'",
                                    inp["job"]["id"])
            for e in t["frame"]["ev"]:
                if e["type"] == "cmd":
                    continue  # same fact as the input above
                pending.append((run_id, tick, e.get("robot"), f"sim.{e['type']}", e))
                if e["type"] == "job_done" and e.get("job"):
                    await c.execute("UPDATE jobs SET status = $2, done_tick = $3, result = $4 WHERE id = $1",
                                    e["job"], "done" if e["ok"] else "wrong_item", tick, e)
                    await c.execute("UPDATE orders SET status = $2, shipped_at = now() WHERE job_id = $1",
                                    e["job"], "shipped" if e["ok"] else "short_shipped")
                elif e["type"] == "job_released" and e.get("job"):  # a faulted robot hands its job back
                    await c.execute("UPDATE jobs SET status = 'pending', robot_id = NULL, created_tick = NULL "
                                    "WHERE id = $1 AND status IN ('assigned', 'active')", e["job"])
                    await c.execute("UPDATE orders SET status = 'released' WHERE job_id = $1 AND status = 'picking'", e["job"])
                elif e["type"] == "fault_alarm":
                    alarms.append((tick, e))
                elif e["type"] == "job_exception" and e.get("job"):
                    await c.execute("UPDATE jobs SET status = 'exception', done_tick = $2, result = $3 "
                                    "WHERE id = $1", e["job"], tick, e)
                    await c.execute("UPDATE orders SET status = 'exception' WHERE job_id = $1", e["job"])
            for f in detect(t["frame"]):
                await flush()  # keep the log in tick order
                eid = await log_event(c, "failure", f, run_id=run_id, tick=f["tick"], robot_id=f["robot"])
                fid = await c.fetchval(
                    "INSERT INTO failures (event_id, run_id, tick, type, robot_id, detail, status) "
                    "VALUES ($1, $2, $3, $4, $5, $6, 'open') ON CONFLICT DO NOTHING RETURNING id",
                    eid, run_id, f["tick"], f["type"], f["robot"], f["detail"])
                if fid:
                    new_failures.append({"id": fid, "status": "open", "run_id": run_id, **f})
        for n in body.get("notices", []):
            pending.append((run_id, n.get("tick"), None, f"sim.{n.get('type', 'notice')}", n))
        await flush()
        if ticks:
            await c.execute("UPDATE runs SET last_tick = GREATEST(last_tick, $2), policy_version = $3 "
                            "WHERE id = $1", run_id, ticks[-1]["tick"], body.get("policy_version"))

    if rt.run_id is None:  # control plane restarted while the sim kept running
        rt.run_id = run_id
    if ticks and run_id == rt.run_id:
        rt.frame, rt.last_tick, rt.frame_at = ticks[-1]["frame"], ticks[-1]["tick"], time.monotonic()
    rt.sim_policy_version = body.get("policy_version")
    rt.sim_host = body.get("host") or rt.sim_host
    if rt.service is not None:
        for t in ticks:
            rt.service.observe(t["frame"])
        for tick, e in alarms:
            await rt.service.open_case(run_id, tick, e)
    if ticks:
        rt.meters["ticks"].add(len(ticks))
        rt.hub.publish("frames", [ui_frame(t["frame"]) for t in ticks])
    for f in new_failures:
        log.info("failure %s %s at tick %s", f["type"], f["robot"], f["tick"])
        rt.hub.publish("failure", f)
    for n in body.get("notices", []):
        rt.hub.publish("notice", n)
    if rt.orchestrator is not None:
        rt.orchestrator.kick()
    return {"ok": True, "last_tick": rt.last_tick}
