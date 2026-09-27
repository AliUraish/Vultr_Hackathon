"""Haul dispatch: load tickets from the dig faces, and the AI dispatcher that sends trucks to them.

Every excavator wants a truck under it and one on the way (TARGET_PER_FACE open tickets).
When tickets wait and trucks are free, the dispatch AI decides which truck goes where: it
sees each free truck's road distance to every face (routed around known potholes, rocks
and closed roads), how long each excavator has been left alone, and what is on the way
already. The rules check every assignment (only free trucks, only waiting tickets, one
ticket per truck) and dispatch on their own when the model is off, slow or failing:
capable -> free -> nearest to the face -> lowest id.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from replay_core.dispatch import choose_robot, make_cmd, requirements
from replay_core.nav import astar
from replay_core.world import CELL, JOB_DEADLINE_TICKS, POTHOLE_COST, W

from . import enterprise
from .db import log_event
from .llm import ModelError, obj, structured
from .runtime import Runtime
from .simnode_client import SimNodeError

log = logging.getLogger("replay.fleet")

DISPATCH_RULE = "capable -> free -> nearest to the face -> lowest id"
INFLIGHT_SECONDS = 3.0
TARGET_PER_FACE = 2          # one truck under the excavator, one on its way
TICKET_GAP_S = 3.0           # a face raises at most one ticket this often
AI_EVERY_S = 2.0             # at most one dispatch question to the model this often
AI_TIMEOUT_S = 15.0
AI_COOLDOWN_S = 60.0         # after 3 model failures in a row the rules dispatch alone for a minute


class JobError(ValueError):
    pass


def cname(c: list[int] | tuple[int, int]) -> str:
    return f"c{c[0]}_{c[1]}"


def zone_name(cell: tuple[int, int]) -> str:
    zones = [z for z in W.cell_zones.get(cell, ()) if z not in ("haul_roads", "ramps", "benches", "bench_upper", "bench_lower")]
    return zones[0].replace("_", " ") if zones else "road"


def face_of(job: dict) -> str | None:
    lines = job.get("lines") or []
    return lines[0]["slot"] if lines else None


class Dispatcher:
    """Load tickets, excavator waiting times and the AI dispatch loop state."""

    def __init__(self) -> None:
        self.rng = random.Random()
        self.last_ticket: dict[str, float] = {}
        self.alone_since: dict[str, float] = {s: time.monotonic() for s in W.slots}
        self.last_ai = 0.0
        self.fails = 0
        self.cooldown_until = 0.0
        self.thinking = False

    def track_faces(self, rt: Runtime, open_jobs: list[dict]) -> dict[str, dict]:
        """Per face: trucks on the way or under the excavator, and how long it has been alone."""
        frame = rt.frame or {"robots": []}
        by_robot = {r["id"]: r for r in frame["robots"]}
        now = time.monotonic()
        out: dict[str, dict] = {}
        for slot in W.slots:
            coming = [j["robot_id"] for j in open_jobs if face_of(j) == slot and j["robot_id"]
                      and not (by_robot.get(j["robot_id"]) or {}).get("carry")]
            if coming:
                self.alone_since[slot] = None  # type: ignore[assignment]
            elif self.alone_since.get(slot) is None:
                self.alone_since[slot] = now
            since = self.alone_since.get(slot)
            out[slot] = {"trucks": coming, "alone_s": round(now - since) if since else 0}
        return out


# ------------------------------------------------------------------ tickets

async def create_job(rt: Runtime, slots: list[str], dock: str, source: str, order: dict | None = None) -> dict:
    if len(slots) != 1:
        raise JobError("a load comes from exactly one dig face")
    bad = [s for s in slots if s not in W.slots]
    if bad:
        raise JobError(f"unknown dig face {', '.join(bad)}: faces are {', '.join(sorted(W.slots))}")
    if dock not in W.docks:
        raise JobError(f"unknown dump point {dock}: dump points are {', '.join(sorted(W.docks))}")
    lines = [{"slot": s, "sku": W.slots[s]["sku"], "cls": W.slots[s]["cls"]} for s in slots]
    async with rt.pool.acquire() as c, c.transaction():
        seq = await c.fetchval("SELECT nextval('job_seq')")
        jid = f"J{seq}"
        if order is not None:
            await c.execute(
                "INSERT INTO orders (id, customer_id, dock, carrier, lines, value, priority, status, job_id, ship_by) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, 'released', $8, $9)",
                order["id"], order["customer_id"], order["dock"], order["carrier"], order["lines"], order["value"],
                order["priority"], jid, order["ship_by"])
        row = await c.fetchrow(
            "INSERT INTO jobs (id, kind, lines, dock, status, run_id, created_tick, deadline_tick, order_id) "
            "VALUES ($1, 'single', $2, $3, 'pending', $4, $5, $6, $7) RETURNING *",
            jid, lines, dock, rt.run_id, rt.last_tick, rt.last_tick + JOB_DEADLINE_TICKS,
            order["id"] if order else None)
        await log_event(c, "job.created", {"job": jid, "lines": lines, "dock": dock, "source": source,
                                           **({"order": order["id"], "customer": order["customer"],
                                               "tonnes": order["lines"][0]["qty"], "value": order["value"]} if order else {})},
                        run_id=rt.run_id, tick=rt.last_tick)
    job = dict(row)
    rt.hub.publish("job", job)
    return job


async def release_load(rt: Runtime, rng: random.Random, slot: str) -> dict:
    """A dig face asks for a truck: a load ticket from its excavator to its material's destination."""
    async with rt.pool.acquire() as c:
        seq = await c.fetchval("SELECT nextval('order_seq')")
    order = enterprise.make_order(rt.site, rng, seq, slot)
    return await create_job(rt, [slot], order["dock"], "face", order)


async def open_jobs(rt: Runtime) -> list[dict]:
    async with rt.pool.acquire() as c:
        return [dict(r) for r in await c.fetch(
            "SELECT j.*, o.priority, o.value FROM jobs j LEFT JOIN orders o ON o.id = j.order_id "
            "WHERE j.status IN ('pending', 'assigned', 'active') ORDER BY j.created_at")]


async def demand_once(rt: Runtime, d: Dispatcher, jobs: list[dict]) -> None:
    now = time.monotonic()
    closed = {z["zone"] for z in (rt.frame or {}).get("restricted", [])}
    for slot in sorted(W.slots):
        if any(z in closed for z in W.cell_zones.get(tuple(W.slots[slot]["access"]), ())):
            continue  # the face is inside a blast exclusion zone: no loads until it reopens
        n = sum(1 for j in jobs if face_of(j) == slot)
        if n < TARGET_PER_FACE and now - d.last_ticket.get(slot, 0) > TICKET_GAP_S:
            d.last_ticket[slot] = now
            await release_load(rt, d.rng, slot)


# ------------------------------------------------------------------ road distances

def road_m(frame: dict, start: tuple[int, int], goal: tuple[int, int]) -> int | None:
    """Metres by road from a truck's segment to a face, around known potholes, rocks and closed roads."""
    hard = {tuple(p["cell"]) for p in frame.get("pallets", [])}
    for z in frame.get("restricted", []):
        hard |= {tuple(c) for c in W.zones.get(z["zone"], [])}
    soft = {tuple(p["cell"]): POTHOLE_COST for p in frame.get("potholes", []) if p.get("known")}
    path = astar(start, goal, hard - {start, goal}, soft)
    return None if path is None else (len(path) - 1) * CELL // 1000


# ------------------------------------------------------------------ the AI dispatcher

DISPATCH_SCHEMA = obj({
    "assignments": {"type": "array", "items": obj({"truck": {"type": "string"}, "ticket": {"type": "string"},
                                                   "reason": {"type": "string"}})},
    "summary": {"type": "string"},
})

I_DISPATCH = """You are the haul dispatcher of an open-pit mine run by autonomous haul trucks. Excavators at the dig
faces need trucks; tickets waiting for a truck and the trucks that are free are listed. Assign free trucks to waiting
tickets. Priorities, in order: (1) an excavator left alone with no truck under it or on its way stops production:
serve the one alone longest first; (2) ore feeds the crusher, so ore loads marked expedite come next; (3) send the
truck with the shortest road distance (already routed around known potholes, fallen rocks and closed roads);
(4) don't send two trucks into the same one-lane bench road from opposite ends at once. Each truck takes at most one
ticket; use only the listed truck ids and ticket ids. Leave a ticket waiting only if no free truck suits it. For each
assignment give one short, concrete sentence a shift supervisor would accept (distance, why this truck, what it
avoids). summary: one sentence."""


def _truck_line(r: dict) -> str:
    st = r.get("st")
    return f"{st} at {cname([r['x'] // CELL, r['y'] // CELL])}"


async def ai_assign(rt: Runtime, d: Dispatcher, pending: list[dict], free: list[dict], faces: dict[str, dict]
                    ) -> tuple[list[dict], str, int, str]:
    """(assignments, source, tokens, summary) from the model. Raises on failure."""
    frame = rt.frame or {}
    site = rt.site or {}
    exc = {e["slot"]: e for e in site.get("excavators", [])}
    cat = site.get("catalog", {})
    dest = {c["dock"]: c["carrier"] for c in site.get("carriers", [])}
    trucks = []
    for r in free:
        here = (r["x"] // CELL, r["y"] // CELL)
        trucks.append({"id": r["id"], "where": f"{cname(here)} ({zone_name(here)})", "status": r["st"],
                       "road_m_to_face": {s: road_m(frame, here, tuple(W.slots[s]["access"])) for s in sorted(W.slots)}})
    tickets = []
    for j in pending:
        s = face_of(j)
        tickets.append({"ticket": j["id"], "face": s, "excavator": exc.get(s, {}).get("id", f"face {s}"),
                        "material": cat.get(s, {}).get("name", W.slots[s]["cls"]), "class": W.slots[s]["cls"],
                        "destination": dest.get(j["dock"], j["dock"]), "priority": j.get("priority") or "standard",
                        "excavator_alone_s": faces[s]["alone_s"], "trucks_already_coming": faces[s]["trucks"]})
    payload = {"free_trucks": trucks, "waiting_tickets": tickets,
               "road": {"known_potholes": [cname(p["cell"]) for p in frame.get("potholes", []) if p.get("known")],
                        "fallen_rocks": [cname(p["cell"]) for p in frame.get("pallets", [])],
                        "closed_roads": [z["zone"] for z in frame.get("restricted", [])]}}
    ans, source, tokens = await structured(rt.settings, I_DISPATCH, payload, DISPATCH_SCHEMA, "dispatch",
                                           max_tokens=900, timeout=AI_TIMEOUT_S)
    items = ans.get("assignments")
    if not isinstance(items, list):
        raise ModelError("no assignments list")
    return items, source, tokens, str(ans.get("summary") or "")[:300]


def rules_assign(frame: dict, pending: list[dict], busy: set[str], faces: dict[str, dict]) -> list[dict]:
    out, taken = [], set(busy)
    for j in sorted(pending, key=lambda j: -faces[face_of(j)]["alone_s"]):   # lonely excavators first
        job = {"id": j["id"], "kind": j["kind"], "lines": j["lines"], "dock": j["dock"]}
        rid = choose_robot(frame, job, taken)
        if rid:
            taken.add(rid)
            out.append({"truck": rid, "ticket": j["id"], "reason": "rule: nearest free truck"})
    return out


async def dispatch_once(rt: Runtime, d: Dispatcher) -> None:
    if not rt.sim_online or rt.frame is None or rt.site is None:
        return
    now = time.monotonic()
    rt.inflight = {r: t for r, t in rt.inflight.items() if now - t < INFLIGHT_SECONDS}
    jobs = await open_jobs(rt)
    faces = d.track_faces(rt, jobs)
    rt.faces = faces
    await demand_once(rt, d, jobs)
    pending = [j for j in await open_jobs(rt) if j["status"] == "pending"][:12]
    free = [r for r in rt.frame["robots"] if r["free"] and r["id"] not in rt.inflight]
    if not pending or not free:
        return
    source, tokens, summary, ms = "rules", 0, "", 0
    decisions: list[dict] = []
    use_ai = rt.settings.inference_enabled and now >= d.cooldown_until
    if use_ai and now - d.last_ai < AI_EVERY_S:
        return
    if use_ai:
        d.last_ai = now
        lonely = sorted((s for s in faces if faces[s]["alone_s"] >= 5 and not faces[s]["trucks"]),
                        key=lambda s: -faces[s]["alone_s"])
        rt.decide({"kind": "dispatch", "phase": "ask", "faces": lonely,
                   "tickets": [j["id"] for j in pending], "trucks": [r["id"] for r in free]})
        t0 = time.monotonic()
        try:
            decisions, source, tokens, summary = await ai_assign(rt, d, pending, free, faces)
            d.fails = 0
        except Exception as exc:  # the model is down, slow or wrong: the rules dispatch this round
            d.fails += 1
            if d.fails >= 3:
                d.cooldown_until = time.monotonic() + AI_COOLDOWN_S
            log.warning("dispatch AI unavailable (%s); rules dispatch", str(exc)[:160])
            decisions, source = [], "rules"
        ms = int((time.monotonic() - t0) * 1000)
    if source == "rules":
        decisions = rules_assign(rt.frame, pending, set(rt.inflight), faces)
    await apply_assignments(rt, decisions, pending, source, tokens, ms, summary, faces)


async def apply_assignments(rt: Runtime, decisions: list[dict], pending: list[dict], source: str, tokens: int,
                            ms: int, summary: str, faces: dict[str, dict]) -> None:
    by_id = {j["id"]: j for j in pending}
    frame = rt.frame or {"robots": []}
    free = {r["id"]: r for r in frame["robots"] if r["free"] and r["id"] not in rt.inflight}
    used_t: set[str] = set()
    used_j: set[str] = set()
    for a in decisions:
        if not isinstance(a, dict):
            continue
        rid, jid = str(a.get("truck", "")), str(a.get("ticket", ""))
        j = by_id.get(jid)
        if rid not in free or j is None or rid in used_t or jid in used_j:
            continue   # the rules check: only free trucks, only waiting tickets, one each
        created = j["created_tick"] if j["run_id"] == rt.run_id and j["created_tick"] is not None else rt.last_tick
        job = {"id": jid, "kind": j["kind"], "lines": j["lines"], "dock": j["dock"],
               "deadline": created + JOB_DEADLINE_TICKS}
        if not requirements(job) <= W.robot_caps[rid]:
            continue
        cmd = make_cmd(rid, job)
        try:
            res = await rt.sim.command(rid, cmd["steps"], cmd["job"])
        except SimNodeError as exc:
            log.warning("dispatch of %s failed: %s", jid, exc)
            return
        used_t.add(rid)
        used_j.add(jid)
        rt.inflight[rid] = time.monotonic()
        r = free[rid]
        slot = face_of(j)
        here = (r["x"] // CELL, r["y"] // CELL)
        dist = road_m(frame, here, tuple(W.slots[slot]["access"]))
        reason = str(a.get("reason") or "")[:300]
        by = "ai" if source != "rules" else "rules"
        payload = {"job": jid, "robot": rid, "face": slot, "dock": j["dock"], "road_m": dist, "reason": reason,
                   "by": by, "source": source, "alone_s": faces.get(slot, {}).get("alone_s", 0),
                   "rule": DISPATCH_RULE if by == "rules" else None}
        async with rt.pool.acquire() as c, c.transaction():
            await c.execute("UPDATE jobs SET status = 'assigned', robot_id = $2, run_id = $3, "
                            "created_tick = $4, deadline_tick = $5 WHERE id = $1 AND status = 'pending'",
                            jid, rid, rt.run_id, created, job["deadline"])
            await c.execute("INSERT INTO assignments (job_id, robot_id, run_id, steps, input_id, reason) "
                            "VALUES ($1, $2, $3, $4, $5, $6)",
                            jid, rid, rt.run_id, cmd["steps"], res.get("input_id"), payload)
            await log_event(c, "ai.dispatch" if by == "ai" else "dispatch", payload,
                            run_id=rt.run_id, tick=rt.last_tick, robot_id=rid)
        rt.hub.publish("job", {"id": jid, "status": "assigned", "robot_id": rid})
        rt.decide({"kind": "dispatch", "phase": "done", "robot": rid, "job": jid, "face": slot,
                   "dock": j["dock"], "road_m": dist, "reason": reason, "by": by, "source": source,
                   "ms": ms, "tokens": tokens, "alone_s": payload["alone_s"], "summary": summary})


async def loop(rt: Runtime) -> None:
    d = Dispatcher()
    rt.dispatcher = d
    while True:
        await asyncio.sleep(1.0)
        try:
            if rt.auto_jobs:
                await dispatch_once(rt, d)
        except Exception:  # keep the fleet loop alive; the error is in the log
            log.exception("fleet loop")


__all__ = ["DISPATCH_RULE", "Dispatcher", "JobError", "ai_assign", "apply_assignments", "create_job", "dispatch_once",
           "loop", "release_load", "road_m", "rules_assign"]
