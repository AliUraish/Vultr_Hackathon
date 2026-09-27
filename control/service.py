"""Truck service cases: a haul truck faults in the pit and the copilot runs the recovery.

    detected    the truck safety-stopped and raised an alarm (the ping in the pit); the crew is paged:
                "T04 is not working. Please fix it."
    safing      the copilot picks a safe spot (beside the road first; off a one-lane bench if it is
                blocking one) from spots the rules offer, and drives it there at limp speed
    diagnosing  the copilot reads the health telemetry (tyre pressures, wheel slip, drive current,
                vibration, lidar) and names the fault; a rule reading is kept alongside as a check
  sensor fault (it can still drive):
    recovering  the copilot takes remote control, drives it to a free workshop bay and deploys the spare
    in_repair   a fitter recalibrates it in the bay; it becomes the new standby truck
  tyre fault (it can't drive):
    dispatched  the copilot assigns a tyre fitter from the crew with a work order; they drive out to it
    repairing   the fitter changes the tyre and marks the job done; the truck rejoins the fleet
    resolved

The model decides; the rules validate every decision (only offered spots, only real crew members, a
tyre fault is never driven); the deterministic sim executes it. Every step lands on the case and in
the event log, and the model can be switched off without breaking the pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from typing import Any

import asyncpg
import httpx

from replay_core.nav import astar
from replay_core.world import CELL, TICK_HZ, W

from .db import log_event
from .llm import ModelError, obj, structured
from .runtime import Runtime

log = logging.getLogger("replay.service")

OPEN = ("detected", "safing", "diagnosing", "recovering", "in_repair", "dispatched", "repairing")
UTE_CELLS_PER_S = 0.6       # fitters drive out in a service ute: 12 m/s on the haul roads
TIRE_REPAIR_S = 20
SENSOR_REPAIR_S = 14
TOOL_CRIB = (27, 19)         # where the fitters start, outside the workshop
LANES = ("bench_", "cuts", "crest_road", "middle_road", "pit_floor", "ramp_", "dump_area")
ACTIVE_TARGET = 8            # trucks the mine plans to have hauling
NOMINAL = {"tire_psi": "100-110", "slip_pct": "< 5 (moving)", "drive_imbalance_pct": "< 6",
           "vibration_g": "< 0.2", "lidar_returns_pct": "> 95", "lidar_range_mm": 60000}
_DIRS = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}
_WHEEL = {"FL": "front-left", "FR": "front-right", "RL1": "rear-left outer", "RL2": "rear-left inner",
          "RR1": "rear-right outer", "RR2": "rear-right inner"}


# ---------------------------------------------------------------- pure helpers (tested)

def cell_of(r: dict) -> tuple[int, int]:
    return (r["x"] // CELL, r["y"] // CELL)


def is_lane(c: tuple[int, int]) -> bool:
    return any(z.startswith(LANES) for z in W.cell_zones.get(c, ()))


def safe_cells(frame: dict, rid: str, max_steps: int = 4) -> list[dict]:
    """Spots the truck could pull over to: reachable in a few segments, not reserved or planned by another
    truck, no rock, not a loading or tipping pocket; scored so beside the road and off the one-lane benches win."""
    me = next(r for r in frame["robots"] if r["id"] == rid)
    start = cell_of(me)
    dx, dy = _DIRS.get(me.get("dir") or "E", (1, 0))
    left, right = (dy, -dx), (-dy, dx)
    taken = {tuple(c) for o in frame["robots"] if o["id"] != rid for c in o.get("res", [])}
    taken |= {cell_of(o) for o in frame["robots"] if o["id"] != rid}
    routes = {}
    for o in frame["robots"]:
        if o["id"] != rid:
            for c in o.get("path", []):
                routes[tuple(c)] = routes.get(tuple(c), 0) + 1
    pallets = {tuple(p["cell"]) for p in frame.get("pallets", [])}
    closed = {tuple(c) for z in frame.get("restricted", []) for c in W.zones.get(z["zone"], [])}
    docks = set(W.pockets)   # loading and tipping pockets stay clear for the fleet
    seen, frontier, out = {start: 0}, [start], []
    while frontier:
        nxt = []
        for c in frontier:
            for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (c[0] + ddx, c[1] + ddy)
                if n in seen or not W.passable(n) or n in taken or n in pallets or n in closed:
                    continue
                seen[n] = seen[c] + 1
                if seen[n] < max_steps:
                    nxt.append(n)
                if n in docks:
                    continue
                off = (n[0] - start[0], n[1] - start[1])
                side = ("left" if off == left else "right" if off == right else "ahead" if off == (dx, dy)
                        else "behind" if off == (-dx, -dy) else "nearby")
                lane = any(z.startswith(("bench_", "cuts")) for z in W.cell_zones.get(n, ()))   # one-lane: blocks everyone
                score = seen[n] * 2 + (6 if lane else 0) + routes.get(n, 0) * 4 + (0 if side in ("left", "right") else 1.5)
                out.append({"id": f"c{n[0]}_{n[1]}", "cell": [n[0], n[1]], "side": side, "segments_away": seen[n],
                            "one_lane_road": lane, "other_truck_routes": routes.get(n, 0),
                            "zone": [z for z in W.cell_zones.get(n, ()) if z not in ("haul_roads", "ramps", "benches")] or ["road"],
                            "score": score})
        frontier = nxt
    out.sort(key=lambda x: (x["score"], x["id"]))
    return out[:6]


def read_health(h: dict | None) -> dict:
    """The rule reading of the health telemetry: the check the model's diagnosis is held against."""
    if not h:
        return {"fault": "unknown", "component": "", "movable": False, "evidence": ["no telemetry"]}
    tires = h.get("tires", {})
    low = sorted((p, w) for w, p in tires.items() if p < 60)
    if low or h.get("slip", 0) > 12 or h.get("imbalance", 0) > 20:
        w = low[0][1] if low else min(tires, key=tires.get) if tires else "FL"
        ev = [f"{_WHEEL.get(w, w)} tyre {tires.get(w)} psi (others {', '.join(str(p) for k, p in sorted(tires.items()) if k != w)})",
              f"wheel slip {h.get('slip')}%", f"drive current imbalance {h.get('imbalance')}%", f"vibration {h.get('vib')} g"]
        return {"fault": "tire", "component": f"{_WHEEL.get(w, w)} tyre", "movable": False, "evidence": ev}
    if h.get("lidar", 100) < 70 or h.get("range", 60000) < 60000:
        return {"fault": "sensor", "component": "front lidar", "movable": True,
                "evidence": [f"lidar returns {h.get('lidar')}%", f"usable range {h.get('range', 0) / 1000:.0f} m of 60",
                             f"tyres nominal ({', '.join(str(p) for p in tires.values())} psi)"]}
    return {"fault": "unknown", "component": "", "movable": False, "evidence": ["all readings nominal"]}


def walk_route(start: tuple[int, int], goal: tuple[int, int], frame: dict) -> list[list[int]]:
    pallets = {tuple(p["cell"]) for p in frame.get("pallets", [])}
    return astar(start, goal, pallets - {start, goal}, {}) or [[start[0], start[1]], [goal[0], goal[1]]]


def beside(frame: dict, target: tuple[int, int]) -> tuple[int, int]:
    """Where a fitter parks to work on a truck: a free road segment next to it."""
    robots = {cell_of(o) for o in frame["robots"]}
    for ddx, ddy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        n = (target[0] + ddx, target[1] + ddy)
        if W.passable(n) and n not in robots:
            return n
    return target


# ---------------------------------------------------------------- the desk

PULL_OVER = obj({"candidate": {"type": "string"}, "reason": {"type": "string"}})
DIAGNOSE = obj({"fault": {"type": "string", "enum": ["tire", "sensor", "unknown"]}, "component": {"type": "string"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]}, "movable": {"type": "boolean"},
                "evidence": {"type": "array", "items": {"type": "string"}}, "explanation": {"type": "string"}})
RECOVER = obj({"bay": {"type": "string"}, "deploy_spare": {"type": "boolean"}, "technician": {"type": "string"},
               "work_order": {"type": "string"}, "note": {"type": "string"}})

I_PULL = """You are the fleet copilot of an open-pit mine run by autonomous haul trucks. A truck just raised a
hardware alarm and safety-stopped where it was, possibly blocking a road. Choose where it should pull over, from the
candidate spots offered (they are all free and reachable). Prefer: a spot to its left or right, off the one-lane bench
roads and cuts (a truck parked there blocks everyone), not on other trucks' planned routes, and as few segments away
as possible, because the truck may be damaged and will crawl. Answer with the candidate id and one sentence."""

I_DIAG = """You are the fleet copilot diagnosing a haul truck's hardware alarm from its health telemetry.
Readings: six tyre pressures (psi; the rears are duals), wheel slip (%), drive current imbalance between wheels (%),
chassis vibration (g), lidar return rate (%) and usable lidar range (mm); nominal ranges are given. Name the fault
(tire or sensor), the component, whether the truck can safely drive itself to the workshop (a damaged tyre cannot;
a degraded lidar can, slowly, under remote control), your confidence, the evidence readings, and one or two sentences."""

I_RECOVER = """You are the fleet copilot planning a haul truck's recovery. If the fault is a sensor, the truck can drive:
pick a free workshop bay for it and decide whether to deploy the standby truck so the fleet keeps hauling.
If it is a tyre, the truck stays where it is: pick the person from the crew (the tyre fitter if there is one, else
the maintenance fitter) and write a short, specific work order. Fields that don't apply: bay "none",
technician "none", deploy_spare false. Keep the note to one sentence."""


class ServiceDesk:
    def __init__(self, rt: Runtime, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.rt = rt
        self.transport = transport
        self.busy: set[int] = set()
        self.health: dict[str, deque] = defaultdict(lambda: deque(maxlen=60))
        self._wake = asyncio.Event()
        self._last_deploy = 0.0

    def kick(self) -> None:
        self._wake.set()

    def observe(self, frame: dict) -> None:
        """Keep a short history of health readings for robots that are faulted."""
        for r in frame["robots"]:
            if r.get("fault"):
                self.health[r["id"]].append({"t": frame["t"], "v_mps": round(r["v"] * TICK_HZ / 1000, 2), **(r.get("health") or {})})

    # -- persistence ---------------------------------------------------------

    async def _load(self, cid: int) -> dict | None:
        async with self.rt.pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM service_cases WHERE id = $1", cid)
        return dict(row) if row else None

    async def _save(self, case: dict, status: str, step: dict | None = None, **fields: Any) -> dict:
        steps = list(case["steps"]) + ([{"at": time.time(), "status": status, **step}] if step else [])
        if step and step.get("by") in ("copilot", "rules"):   # the decision feed on the live pit
            self.rt.decide({"kind": "service", "phase": "done", "robot": case["robot_id"], "case": case["id"],
                            "status": status, "title": step.get("title"), "reason": step.get("detail"),
                            "by": "ai" if step.get("by") == "copilot" else "rules", "source": step.get("model", "rules")})
        sets = {"status": status, "steps": steps, **fields}
        cols = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(sets))
        async with self.rt.pool.acquire() as c, c.transaction():
            row = await c.fetchrow(f"UPDATE service_cases SET {cols}, updated_at = now()"
                                   f"{', resolved_at = now()' if status in ('resolved', 'failed', 'closed') else ''} "
                                   "WHERE id = $1 RETURNING *", case["id"], *sets.values())
            if step:
                await log_event(c, "service.step", {"case": case["id"], "robot": case["robot_id"], "status": status,
                                                    "title": step.get("title"), "by": step.get("by"),
                                                    "detail": step.get("detail")},
                                run_id=case["run_id"], tick=self.rt.last_tick, robot_id=case["robot_id"])
        out = dict(row)
        self.rt.hub.publish("service", public(out))
        return out

    async def open_case(self, run_id: str, tick: int, ev: dict) -> None:
        rid = ev["robot"]
        async with self.rt.pool.acquire() as c, c.transaction():
            if await c.fetchval("SELECT id FROM service_cases WHERE robot_id = $1 AND status = ANY($2::text[])",
                                rid, list(OPEN)):
                return
            step = {"at": time.time(), "status": "detected", "title": f"{rid} raised {ev.get('code')}",
                    "detail": f"safety stop at c{ev['cell'][0]}_{ev['cell'][1]}"
                              + (f"; load {ev['job']} handed back to the fleet" if ev.get("job") else ""), "by": "robot"}
            row = await c.fetchrow(
                "INSERT INTO service_cases (run_id, robot_id, tick, code, status, steps, job_released, start_cell) "
                "VALUES ($1, $2, $3, $4, 'detected', $5, $6, $7) RETURNING *",
                run_id, rid, tick, ev.get("code", ""), [step], ev.get("job"), ev.get("cell"))
            await log_event(c, "service.opened", {"case": row["id"], "robot": rid, "code": ev.get("code"),
                                                  "cell": ev.get("cell"), "job": ev.get("job")},
                            run_id=run_id, tick=tick, robot_id=rid)
            message = (f"{rid} is not working ({ev.get('code')}). The copilot is pulling it off the road; "
                       f"please fix it.")
            await log_event(c, "service.alert", {"case": row["id"], "robot": rid, "message": message,
                                                 "to": "shift crew"}, run_id=run_id, tick=tick, robot_id=rid)
        self.rt.hub.publish("service", public(dict(row)))
        self.rt.hub.publish("alert", {"case": row["id"], "robot": rid, "message": message, "code": ev.get("code")})
        self.rt.decide({"kind": "service", "phase": "alert", "robot": rid, "case": row["id"], "title": f"{rid} is not working",
                        "reason": message, "by": "rules"})
        self.kick()

    # -- the loop --------------------------------------------------------------

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                async with self.rt.pool.acquire() as c:
                    ids = [r["id"] for r in await c.fetch("SELECT id FROM service_cases WHERE status = ANY($1::text[]) "
                                                          "ORDER BY id", list(OPEN))]
                for cid in ids:
                    if cid not in self.busy:
                        self.busy.add(cid)
                        asyncio.create_task(self._advance(cid))
                await self._keep_capacity(ids)
            except Exception:
                log.exception("service loop")

    async def _keep_capacity(self, open_ids: list[int]) -> None:
        """Keep the fleet hauling: if a case left it short and a healthy standby is parked (e.g. one just
        repaired in the workshop), deploy it and record it on the case that needed it."""
        f = self.rt.frame
        if not f or not self.rt.sim_online:
            return
        working = [r for r in f["robots"] if not r.get("svc") and not r.get("fault")]
        standby = [r for r in f["robots"] if r.get("svc") == "standby" and not r.get("fault")]
        if len(working) >= ACTIVE_TARGET or not standby or time.time() - self._last_deploy < 5:
            return
        self._last_deploy = time.time()
        spare = standby[0]
        await self.rt.sim.service(spare["id"], "deploy", None, "capacity rule")
        case = None
        for cid in sorted(open_ids, reverse=True):
            c = await self._load(cid)
            if c and c["status"] in ("recovering", "in_repair") and not c["spare"]:
                case = c
                break
        if case:
            await self._save(case, case["status"], {"title": f"Standby {spare['id']} deployed to restore capacity",
                                                    "by": "rules", "detail": f"{len(working)} trucks were hauling; target {ACTIVE_TARGET}"},
                             spare=spare["id"])
        else:
            async with self.rt.pool.acquire() as c:
                await log_event(c, "service.capacity", {"deployed": spare["id"], "working": len(working)},
                                run_id=self.rt.run_id, tick=self.rt.last_tick, robot_id=spare["id"])

    async def _advance(self, cid: int) -> None:
        try:
            case = await self._load(cid)
            if case and case["status"] in OPEN and self.rt.run_id and case["run_id"] != self.rt.run_id:
                await self._save(case, "closed", {"title": "Closed: the sim restarted", "by": "fleet",
                                                  "detail": "the truck came back healthy in the new run"})
            elif case and case["status"] in OPEN:
                await getattr(self, f"_{case['status']}")(case)
        except Exception:
            log.exception("service case %s", cid)
        finally:
            self.busy.discard(cid)

    def _robot(self, rid: str) -> dict | None:
        f = self.rt.frame
        return next((r for r in f["robots"] if r["id"] == rid), None) if f else None

    async def _ask(self, case: dict, instructions: str, payload: dict, schema: dict, name: str) -> tuple[dict | None, str, int]:
        try:
            ans, source, tokens = await structured(self.rt.settings, instructions, payload, schema, name, self.transport)
            return ans, source, tokens
        except (ModelError, httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("service copilot (%s) unavailable: %s", name, exc)
            return None, "rules", 0

    # -- steps -----------------------------------------------------------------

    async def _detected(self, case: dict) -> None:
        r = self._robot(case["robot_id"])
        if r is None:
            return
        cands = safe_cells(self.rt.frame, case["robot_id"], max_steps=4)
        if not cands:
            await self._save(case, "diagnosing", {"title": "Holding in place", "by": "rules",
                                                  "detail": "no free cell within reach; the fleet routes around it"})
            return
        payload = {"truck": case["robot_id"], "alarm": case["code"], "at": f"c{cell_of(r)[0]}_{cell_of(r)[1]}",
                   "heading": r.get("dir"),
                   "road": [z for z in W.cell_zones.get(cell_of(r), ()) if z not in ("haul_roads", "ramps", "benches")],
                   "candidates": [{k: v for k, v in c.items() if k != "score"} for c in cands],
                   "other_trucks": [{"id": o["id"], "at": f"c{cell_of(o)[0]}_{cell_of(o)[1]}", "status": o["st"]}
                                    for o in self.rt.frame["robots"] if o["id"] != case["robot_id"]]}
        ans, source, tokens = await self._ask(case, I_PULL, payload, PULL_OVER, "pull_over")
        pick = next((c for c in cands if ans and c["id"] == ans.get("candidate")), None)
        by, reason = ("copilot", str(ans.get("reason", ""))[:300]) if pick else ("rules", "")
        if pick is None:
            pick = cands[0]
            reason = f"best-scored free spot ({pick['side']}, {pick['segments_away']} segment(s) away, {'on' if pick['one_lane_road'] else 'off'} a one-lane road)"
        await self.rt.sim.service(case["robot_id"], "move", pick["cell"], by)
        await self._save(case, "safing", {"title": f"Pulling over to {pick['id']} ({pick['side']})", "by": by,
                                          "model": source, "detail": reason, "cell": pick["cell"]},
                         safe_cell=pick["cell"], deadline=time.time() + 90, source=source, tokens=case["tokens"] + tokens)

    async def _safing(self, case: dict) -> None:
        r = self._robot(case["robot_id"])
        arrived = r is not None and list(cell_of(r)) == list(case["safe_cell"]) and r.get("svc") == "fault"
        if not arrived and time.time() < (case["deadline"] or 0):
            return
        await self._save(case, "diagnosing", {"title": "Pulled over, lane clear" if arrived else "Could not reach the cell; diagnosing in place",
                                              "by": "fleet", "detail": f"at c{cell_of(r)[0]}_{cell_of(r)[1]}" if r else ""})

    async def _diagnosing(self, case: dict) -> None:
        rid = case["robot_id"]
        r = self._robot(rid)
        hist = list(self.health.get(rid, []))[-12:] or ([{"t": self.rt.last_tick, **(r.get("health") or {})}] if r else [])
        rules = read_health(hist[-1] if hist else None)
        payload = {"truck": rid, "alarm": case["code"], "nominal": NOMINAL,
                   "readings": [{"t": h.get("t"), "tires_psi": h.get("tires"), "slip_pct": h.get("slip"),
                                 "drive_imbalance_pct": h.get("imbalance"), "vibration_g": h.get("vib"),
                                 "lidar_returns_pct": h.get("lidar"), "lidar_range_mm": h.get("range"),
                                 "speed_mps": h.get("v_mps")} for h in hist[::2]]}
        ans, source, tokens = await self._ask(case, I_DIAG, payload, DIAGNOSE, "diagnose")
        diag, by, note = rules, "rules", ""
        if ans and ans.get("fault") in ("tire", "sensor"):
            if rules["fault"] in ("tire", "sensor") and ans["fault"] != rules["fault"]:
                note = f"the copilot said {ans['fault']}, the rule reading says {rules['fault']}: going with the rules"
            else:
                diag = {"fault": ans["fault"], "component": str(ans.get("component", ""))[:80] or rules["component"],
                        "movable": bool(ans.get("movable")) and ans["fault"] != "tire",   # a damaged tire is never driven
                        "evidence": [str(e)[:160] for e in ans.get("evidence", [])][:5] or rules["evidence"],
                        "confidence": ans.get("confidence"), "explanation": str(ans.get("explanation", ""))[:400]}
                by = "copilot"
        if diag["fault"] == "unknown":
            diag = {**diag, "fault": "sensor" if "PER" in case["code"] else "tire"}
            diag["movable"] = diag["fault"] == "sensor"
        title = f"Diagnosed: {diag['component'] or diag['fault']} ({'can drive slowly' if diag['movable'] else 'must not drive'})"
        detail = diag.get("explanation") or "; ".join(diag["evidence"])
        case = await self._save(case, "diagnosing", {"title": title, "by": by, "model": source,
                                                     "detail": (detail + (f" ({note})" if note else ""))[:500],
                                                     "evidence": diag["evidence"]},
                                fault=diag["fault"], component=diag["component"], movable=diag["movable"],
                                tokens=case["tokens"] + tokens)
        await self._plan(case, diag)

    async def _plan(self, case: dict, diag: dict) -> None:
        rid, frame = case["robot_id"], self.rt.frame
        occupied = {cell_of(o) for o in frame["robots"] if o["id"] != rid}
        bays = [f"G{i + 1}" for i, b in enumerate(W.garage) if b not in occupied]
        spare = next((o for o in frame["robots"] if o.get("svc") == "standby" and not o.get("fault")), None)
        team = (self.rt.site or {}).get("associates", [])
        payload = {"truck": rid, "diagnosis": diag, "free_workshop_bays": bays or ["none"],
                   "standby_truck": spare["id"] if spare else None,
                   "crew": [{"name": a["name"], "role": a["role"], "shift": a.get("shift")} for a in team]}
        ans, source, tokens = await self._ask(case, I_RECOVER, payload, RECOVER, "recover")
        now = time.time()
        if diag["movable"]:
            bay = ans.get("bay") if ans and ans.get("bay") in bays else (bays[0] if bays else None)
            by = "copilot" if ans and ans.get("bay") in bays else "rules"
            if bay is None:
                await self._save(case, "dispatched", {"title": "No free workshop bay: the fitter comes to it", "by": "rules"})
                return await self._send_tech(case, diag, ans, source, tokens)
            cell = list(W.garage[int(bay[1:]) - 1])
            await self.rt.sim.service(rid, "move", cell, "copilot")
            spare_note = ""
            if spare and (ans is None or ans.get("deploy_spare", True)):
                await self.rt.sim.service(spare["id"], "deploy", None, "copilot")
                spare_note = f"; standby {spare['id']} deployed to keep the fleet hauling"
            await self._save(case, "recovering", {"title": f"Copilot has remote control: driving {rid} to workshop bay {bay}", "by": by,
                                                  "model": source, "detail": ((ans or {}).get("note") or "limp speed 0.4 m/s on a degraded lidar") + spare_note},
                             bay=cell, spare=spare["id"] if spare else None, deadline=now + 180,
                             tokens=case["tokens"] + tokens)
        else:
            await self._send_tech(case, diag, ans, source, tokens)

    def _tech(self, name: str | None, fault: str = "") -> dict:
        team = (self.rt.site or {}).get("associates", [])
        pick = next((a for a in team if name and a["name"] == name), None)
        want = "tyre" if fault == "tire" else "maint"
        pick = pick or next((a for a in team if want in a["role"].lower()), None) \
            or next((a for a in team if "fitter" in a["role"].lower() or "maint" in a["role"].lower()), None) \
            or (team[0] if team else None)
        return pick or {"name": "Maintenance fitter", "role": "Maintenance fitter"}

    async def _send_tech(self, case: dict, diag: dict, ans: dict | None, source: str, tokens: int) -> None:
        frame, rid = self.rt.frame, case["robot_id"]
        r = self._robot(rid)
        target = beside(frame, cell_of(r)) if r else TOOL_CRIB
        route = walk_route(TOOL_CRIB, target, frame)
        tech = self._tech((ans or {}).get("technician"), diag.get("fault", ""))
        by = "copilot" if ans and tech["name"] == ans.get("technician") else "rules"
        now = time.time()
        walk = max(3.0, (len(route) - 1) / UTE_CELLS_PER_S)
        order = (ans or {}).get("work_order") or f"Change the {diag['component']} on {rid}, check the rim and wheel motor, test drive 20 m."
        await self._save(case, "dispatched", {"title": f"{tech['name']} ({tech['role']}) dispatched to {rid}", "by": by,
                                              "model": source, "detail": f"work order: {order[:300]} · ETA {walk:.0f} s"},
                         technician={"name": tech["name"], "role": tech["role"], "route": route, "depart_at": now,
                                     "arrive_at": now + walk, "work_order": order[:300]},
                         tokens=case["tokens"] + tokens)

    async def _recovering(self, case: dict) -> None:
        r = self._robot(case["robot_id"])
        arrived = r is not None and list(cell_of(r)) == list(case["bay"]) and r.get("svc") == "fault"
        if not arrived and time.time() < (case["deadline"] or 0):
            return
        frame = self.rt.frame
        target = beside(frame, tuple(case["bay"]))
        route = walk_route(TOOL_CRIB, target, frame)
        tech = self._tech(None, "sensor")
        now = time.time()
        walk = max(2.0, (len(route) - 1) / UTE_CELLS_PER_S)
        await self._save(case, "in_repair", {"title": f"In workshop bay G{list(map(list, W.garage)).index(list(case['bay'])) + 1}: {tech['name']} recalibrating the lidar",
                                             "by": "fleet", "detail": "parked under remote control; the fleet carries on with the spare"},
                         technician={"name": tech["name"], "role": tech["role"], "route": route, "depart_at": now,
                                     "arrive_at": now + walk, "repair_until": now + walk + SENSOR_REPAIR_S,
                                     "work_order": f"Recalibrate and clean the {case['component'] or 'lidar'}, verify returns > 95%."})

    async def _in_repair(self, case: dict) -> None:
        t = case["technician"] or {}
        if time.time() < t.get("repair_until", 0):
            return
        await self.rt.sim.service(case["robot_id"], "repair", None, t.get("name", "technician"))
        await self.rt.sim.service(case["robot_id"], "standby", None, "copilot")
        await self._save(case, "resolved", {"title": f"Fixed and marked done by {t.get('name')}", "by": "technician",
                                            "detail": f"{case['robot_id']} is the new standby truck in its bay"})

    async def _dispatched(self, case: dict) -> None:
        t = case["technician"] or {}
        if time.time() < t.get("arrive_at", 0):
            return
        await self._save(case, "repairing", {"title": f"{t.get('name')} on site: changing the {case['component'] or 'tyre'}",
                                             "by": "technician", "detail": t.get("work_order", "")},
                         technician={**t, "repair_until": time.time() + TIRE_REPAIR_S})

    async def _repairing(self, case: dict) -> None:
        t = case["technician"] or {}
        if time.time() < t.get("repair_until", 0):
            return
        await self.rt.sim.service(case["robot_id"], "repair", None, t.get("name", "technician"))
        await self._save(case, "resolved", {"title": f"Tyre changed, marked done by {t.get('name')}", "by": "technician",
                                            "detail": f"{case['robot_id']} is back in service"})

    async def mark_done(self, cid: int, user: str) -> dict:
        case = await self._load(cid)
        if case is None or case["status"] not in ("dispatched", "repairing", "in_repair"):
            raise ValueError("only a case waiting on a technician can be marked done")
        t = {**(case["technician"] or {}), "arrive_at": time.time(), "repair_until": time.time()}
        status = "repairing" if case["status"] == "dispatched" else case["status"]
        case = await self._save(case, status, {"title": f"Marked done by {user}", "by": "operator"}, technician=t)
        self.kick()
        return public(case)


def public(case: dict) -> dict:
    out = {k: v for k, v in case.items() if k not in ("deadline",)}
    out["server_now"] = time.time()
    return json.loads(json.dumps(out, default=str))


async def open_cases(pool: asyncpg.Pool, limit: int = 20) -> list[dict]:
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM service_cases WHERE status = ANY($1::text[]) OR resolved_at > now() - interval '10 minutes' "
                             "ORDER BY id DESC LIMIT $2", list(OPEN), limit)
    return [public(dict(r)) for r in rows]


__all__ = ["ServiceDesk", "beside", "open_cases", "public", "read_health", "safe_cells", "walk_route"]
