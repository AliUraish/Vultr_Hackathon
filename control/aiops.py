"""The pit runs on Vultr Serverless Inference: every operating decision is a model call, and a second model
call checks it.

  drivers     each haul truck has an AI driver: every few seconds it reads the truck's lidar picture, the
              shared road map (sand drifts, bumps, potholes), the route and the trucks around it, and sets
              the truck's speed for the next stretch (a recorded `advice` input: it can only slow the truck,
              the physics and fleet rules still cap it)
  road crew   after every lidar survey the road-crew AI picks which mapped defect to repair and with what
              (grader, loader, fill team); the crew drives out and the repair lands in the sim
  auditor     every decision any agent makes (dispatch, right of way, recovery, repairs, speed plans,
              hazard drills, approvals) is reviewed by an auditor model: sound, questionable or wrong
  supervisor  a shift supervisor model reads the whole pit twice a minute: headline, risks, actions
  hazards     a safety-drill planner keeps testing the fleet: it picks the next hazard to inject, so
              incidents keep flowing through replay, investigation and proof
  autopilot   fixes that pass every gate (exact trial, stress variants, regression suite) are reviewed by
              a model acting as the safety officer and approved, so the proven-fix suite keeps growing
  governor    paces the drivers so the spend tracks the configured $/hour on Vultr inference

Models propose, rules validate: every answer is range-checked before it touches the fleet, and nothing here
can make a truck faster than the sim's own limits.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import deque
from typing import Any

from replay_core.world import CELL, SURVEY_TICKS, TICK_HZ, W, kmh

from . import assistant, service
from .db import log_event
from .fleet import cname, zone_name
from .llm import ModelError, obj, structured
from .runtime import Runtime
from .simnode_client import SimNodeError
from .usage import USAGE

log = logging.getLogger("replay.aiops")

AUTOPILOT = os.environ.get("AUTOPILOT", "1") == "1"
HAZARD_EVERY_S = float(os.environ.get("HAZARD_EVERY_S", "180"))
BURST_USD = float(os.environ.get("AI_BURST_USD", "0"))           # run the log analysts flat out until this much is spent
LOG_CHARS = int(os.environ.get("AI_LOG_CHARS", "640000"))        # characters of log + telemetry per analysis call (~180k tokens)
LOG_CONCURRENCY = int(os.environ.get("AI_LOG_CONCURRENCY", "48"))
LOG_STEADY_S = float(os.environ.get("AI_LOG_EVERY_S", "45"))     # after the burst: one analysis this often
# The analysts spread over Vultr's other models (each has its own capacity), never the pit's own model.
LOG_MODELS = [m for m in os.environ.get("AI_LOG_MODELS", "glm-5.3-flash,qwen3.8-flash-next,deepseek-v4.1-flash,laguna-s-2.1,"
                                                         "minimax-m3,qwen3.8-27b,mimo-v2.6-flash-rl").split(",") if m]
PER_MODEL = int(os.environ.get("AI_LOG_PER_MODEL", "8"))
MAX_DRIVERS_INFLIGHT = int(os.environ.get("AI_DRIVERS_INFLIGHT", "28"))
SCENARIOS = ("rockfall", "grade_mixup", "blast_closure", "road_damage", "tire_fault", "sensor_fault")


def _mm_tick(kmh_: float) -> int:
    return int(kmh_ * 1_000_000 / 3600 / TICK_HZ)


def _dist_m(a: dict, cell: list[int] | tuple[int, int]) -> int:
    return int((((cell[0] * CELL + CELL // 2) - a["x"]) ** 2 + ((cell[1] * CELL + CELL // 2) - a["y"]) ** 2) ** .5 / 1000)


# ---------------------------------------------------------------- AI drivers

I_DRIVER = """You are the AI driver of one autonomous ultra-class haul truck (930E class, 290 t payload) in an open-pit
mine. Every few seconds you set the truck's speed limit for the next stretch from its sensors, the fleet's shared road
map and the traffic around it. The sim still enforces the hard limits (43 km/h empty, 32 loaded, 18 through bends,
7 through a mapped pothole, 14 over a bump, 18 through a sand drift) and brakes for obstacles on its own; you decide
how fast the truck should go below that. Drive like a careful, productive operator: ease off before mapped potholes,
bumps and sand drifts on the route, on one-lane bench roads under a highwall, loaded on a ramp, close behind another
truck, into loading and tipping pockets, and when the lidar is degraded; otherwise keep production up. A truck that is
stopped, loading or tipping keeps the limit it should have when it moves off. Answer max_kmh (12 to 43), hold_s (5 to
15, how long the limit holds), reason (at most 18 words: the fact that decided it) and watch (at most 10 words)."""
DRIVER = obj({"max_kmh": {"type": "integer"}, "hold_s": {"type": "integer"}, "reason": {"type": "string"},
              "watch": {"type": "string"}})


def driver_context(frame: dict, rid: str, rules: list[str], site: dict | None, last: dict | None) -> dict | None:
    r = next((x for x in frame["robots"] if x["id"] == rid), None)
    if r is None or r.get("svc") == "standby":
        return None
    feats = {tuple(p["cell"]): p for p in frame.get("potholes", []) if p.get("known")}
    route = [tuple(c) for c in (r.get("path") or [])[:10]]
    here = (r["x"] // CELL, r["y"] // CELL)
    elev0 = W.elev[here[1]][here[0]]
    ahead = []
    for c in route:
        f = feats.get(c)
        ahead.append({"seg": cname(c), "zone": zone_name(c), "grade_m": round(W.elev[c[1]][c[0]] - elev0, 1),
                      **({"defect": f.get("kind", "pothole"), "size_mm": f["depth"]} if f else {}),
                      **({"one_lane": True} if c in W.sections else {})})
    road = sorted(({"id": p["id"], "kind": p.get("kind", "pothole"), "at": cname(p["cell"]), "size_mm": p["depth"],
                    "distance_m": _dist_m(r, p["cell"]), "on_route": tuple(p["cell"]) in route}
                   for p in frame.get("potholes", []) if p.get("known")), key=lambda f: f["distance_m"])[:6]
    near = []
    for o in frame["robots"]:
        if o["id"] == rid or o.get("svc") == "standby":
            continue
        d = int(((o["x"] - r["x"]) ** 2 + (o["y"] - r["y"]) ** 2) ** .5 / 1000)
        if d <= 120:
            near.append({"id": o["id"], "distance_m": d, "speed_kmh": round(kmh(o["v"]), 1), "status": o["st"],
                         "loaded": bool(o.get("carry")), "heading": o["dir"],
                         "on_my_route": [o["x"] // CELL, o["y"] // CELL] in [list(c) for c in route]})
    near.sort(key=lambda n: n["distance_m"])
    cat = (site or {}).get("catalog", {})
    load = [cat.get(s.replace("MAT-", ""), {}).get("name", s) for s in r.get("carry", [])]
    return {"truck": rid, "sensors": assistant.sensors(frame, rid, rules), "load": load or None,
            "doing": {"op": r.get("op"), "going_to": cname(r["goal"]) if r.get("goal") else None},
            "route_ahead": ahead, "road_map": road, "trucks_near": near[:5],
            "current_limit_kmh": round(kmh(r["adv"])) if r.get("adv") else None,
            "last_plan": last, "fleet_rules": rules}


class Drivers:
    def __init__(self, rt: Runtime, ops: "AIOps") -> None:
        self.rt, self.ops = rt, ops
        self.interval = 0.5          # seconds between two driver calls; the governor moves it
        self.inflight = 0
        self.cursor = 0
        self.latest: dict[str, dict] = {}
        self.fails = 0

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            rt = self.rt
            if not rt.settings.inference_enabled or not rt.sim_online or self.inflight >= MAX_DRIVERS_INFLIGHT:
                continue
            trucks = [r["id"] for r in rt.frame["robots"] if r.get("svc") != "standby"]
            if not trucks:
                continue
            rid = trucks[self.cursor % len(trucks)]
            self.cursor += 1
            self.inflight += 1
            asyncio.create_task(self.drive(rid))

    async def drive(self, rid: str) -> None:
        rt = self.rt
        try:
            ctx = driver_context(rt.frame, rid, self.ops.rules(), rt.site, self.latest.get(rid))
            if ctx is None:
                return
            t0 = time.monotonic()
            ans, source, tokens = await structured(rt.settings, I_DRIVER, ctx, DRIVER, "driver", max_tokens=160, timeout=40)
            ms = int((time.monotonic() - t0) * 1000)
            v = max(12, min(43, int(ans.get("max_kmh") or 43)))
            hold = max(5, min(15, int(ans.get("hold_s") or 10)))
            reason, watch = str(ans.get("reason") or "")[:160], str(ans.get("watch") or "")[:80]
            await rt.sim.advice(rid, _mm_tick(v), ttl=hold * TICK_HZ + 30, reason=reason, by=f"ai:{source}")
            prev = self.latest.get(rid)
            plan = {"robot": rid, "kmh": v, "hold_s": hold, "reason": reason, "watch": watch, "ms": ms,
                    "tokens": tokens, "at": time.time(), "tick": rt.last_tick, "source": source}
            self.latest[rid] = plan
            rt.hub.publish("driver", plan)
            self.fails = 0
            if prev is None or abs(prev["kmh"] - v) >= 5:
                async with rt.pool.acquire() as c:
                    await log_event(c, "ai.driver", plan, run_id=rt.run_id, tick=rt.last_tick, robot_id=rid)
        except SimNodeError as exc:
            log.debug("driver advice not delivered: %s", exc)
        except Exception as exc:
            self.fails += 1
            if "429" in str(exc):             # Vultr is rate-limiting: ease off so dispatch and traffic calls get through
                self.interval = min(20.0, self.interval * 2)
            if self.fails in (1, 10, 100) or self.fails % 500 == 0:
                log.warning("AI driver %s failed (%s: %s)", rid, type(exc).__name__, str(exc)[:160])
        finally:
            self.inflight -= 1


# ---------------------------------------------------------------- auditor

I_AUDIT = """You are the mine's AI safety and production auditor. Other AI agents run the pit: dispatch (which truck goes
to which excavator), traffic (who has right of way when two trucks meet), service (how a broken-down truck is handled),
road crew (which road defect is repaired first), drivers (each truck's speed plan), hazard drills and fix approvals.
Review each decision below against the facts recorded with it and the mine's rules: loaded trucks have right of way;
send the nearest suitable truck to an excavator that is waiting; slow for mapped potholes, bumps and sand drifts; one-lane
bench roads under a highwall and loaded ramps are the riskiest places; production matters, never over safety. For every
decision return its id, a verdict (sound, questionable or wrong) and a note of at most 20 words with the specific fact."""
AUDIT = obj({"reviews": {"type": "array", "items": obj({"id": {"type": "integer"},
                                                         "verdict": {"type": "string", "enum": ["sound", "questionable", "wrong"]},
                                                         "note": {"type": "string"}})}})
_AUDIT_KEYS = ("kind", "robot", "face", "road_m", "reason", "first", "yield", "how", "trucks", "summary", "title",
               "detail", "fix", "feature", "crew", "scenario", "verdict", "kmh", "plans", "headline", "risks", "actions")


class Auditor:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.q: deque[dict] = deque(maxlen=200)
        self.stats = {"sound": 0, "questionable": 0, "wrong": 0}

    def submit(self, d: dict) -> None:
        if d.get("kind") in ("audit",) or d.get("phase") not in ("done", None):
            return
        self.q.append(d)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(1.5)
            if not self.q or not self.rt.settings.inference_enabled:
                continue
            batch = [self.q.popleft() for _ in range(min(6, len(self.q)))]
            asyncio.create_task(self.review(batch))

    async def review(self, batch: list[dict]) -> None:
        rt = self.rt
        items = [{"id": d["id"], **{k: d[k] for k in _AUDIT_KEYS if d.get(k) not in (None, "", [], {})}} for d in batch]
        try:
            ans, source, _ = await structured(rt.settings, I_AUDIT, {"decisions": items}, AUDIT, "audit", max_tokens=700, timeout=60)
        except Exception as exc:
            log.debug("audit failed: %s", exc)
            return
        known = {d["id"]: d for d in batch}
        for rv in ans.get("reviews") or []:
            try:
                did = int(rv.get("id"))
            except (TypeError, ValueError):
                continue
            if did not in known:
                continue
            verdict = rv.get("verdict") if rv.get("verdict") in self.stats else "sound"
            review = {"verdict": verdict, "note": str(rv.get("note") or "")[:200], "source": source}
            known[did]["review"] = review
            self.stats[verdict] += 1
            rt.hub.publish("review", {"id": did, **review})
            if verdict != "sound":
                async with rt.pool.acquire() as c:
                    await log_event(c, "ai.audit", {"decision": {k: known[did].get(k) for k in _AUDIT_KEYS if known[did].get(k)},
                                                    **review}, run_id=rt.run_id, tick=rt.last_tick,
                                    robot_id=known[did].get("robot") or known[did].get("yield"))


# ---------------------------------------------------------------- road crew

I_ROADS = """You run the road maintenance crews of an open-pit mine whose haul roads keep changing: sand drifts blow in,
bumps and washboard build up, potholes open. The trucks' lidar surveys map every defect. Pick the one defect to repair
next and the crew for it: grader (bumps and shallow potholes: cut and re-grade), loader (sand drifts: scoop and haul
away) or fill_team (deep potholes: fill, water, compact). Only crews listed as free can go. Priorities: defects on the
route of trucks driving now, then on one-lane bench roads and ramps, then near loading and tipping pockets; deeper
before shallower. Answer repair (the defect id, or none), crew, and reason (at most 20 words)."""
ROADS = obj({"repair": {"type": "string"}, "crew": {"type": "string", "enum": ["grader", "loader", "fill_team"]},
             "reason": {"type": "string"}})
CREWS = {"grader": ("Grader G-1", "motor grader"), "loader": ("Loader L-2", "wheel loader"), "fill_team": ("Fill team F-3", "water cart + roller")}
WORK_S = {"bump": 14, "sand": 18, "pothole": 26}


class RoadCrew:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.busy: dict[str, dict] = {}       # crew -> job
        self.claimed: set[str] = set()
        self.beat = -1
        self.last_ask = 0.0

    def jobs(self) -> list[dict]:
        return [{"id": f"crew-{k}", "robot": None, "name": CREWS[k][0], "role": CREWS[k][1], **{x: j[x] for x in
                 ("route", "depart_at", "arrive_at", "repair_until", "task")}} for k, j in self.busy.items()]

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            rt = self.rt
            if not rt.sim_online or not rt.settings.inference_enabled:
                continue
            beat = rt.last_tick // SURVEY_TICKS
            free = [c for c in CREWS if c not in self.busy]
            todo = [p for p in rt.frame.get("potholes", []) if p.get("known") and p["id"] not in self.claimed]
            if free and todo and (beat != self.beat or time.monotonic() - self.last_ask > 12):
                self.beat, self.last_ask = beat, time.monotonic()
                await self.assign(free, todo)

    async def assign(self, free: list[str], todo: list[dict]) -> None:
        rt = self.rt
        frame = rt.frame
        routes = {r["id"]: [tuple(c) for c in (r.get("path") or [])[:12]] for r in frame["robots"]}
        defects = [{"id": p["id"], "kind": p.get("kind", "pothole"), "at": cname(p["cell"]), "zone": zone_name(tuple(p["cell"])),
                    "size_mm": p["depth"], "one_lane": tuple(p["cell"]) in W.sections,
                    "on_routes_of": [rid for rid, rt_ in routes.items() if tuple(p["cell"]) in rt_]} for p in todo]
        try:
            ans, source, tokens = await structured(rt.settings, I_ROADS, {"defects": defects, "free_crews": free,
                                                                          "busy_crews": {k: v["feature"] for k, v in self.busy.items()}},
                                                   ROADS, "roads", max_tokens=200, timeout=45)
        except Exception as exc:
            log.debug("road crew AI failed: %s", exc)
            return
        fid, crew = str(ans.get("repair") or ""), ans.get("crew")
        f = next((p for p in todo if p["id"] == fid), None)
        if f is None or crew not in free:
            return
        cell = tuple(f["cell"])
        route = service.walk_route(service.TOOL_CRIB, service.beside(frame, cell), frame)
        now = time.time()
        drive = max(3.0, (len(route) - 1) / service.UTE_CELLS_PER_S)
        work = WORK_S.get(f.get("kind", "pothole"), 20)
        kind = f.get("kind", "pothole")
        job = {"feature": fid, "cell": list(cell), "kind": kind, "route": route, "depart_at": now, "arrive_at": now + drive,
               "repair_until": now + drive + work, "reason": str(ans.get("reason") or "")[:200],
               "task": {"sand": "clearing the sand drift", "bump": "re-grading the bumps", "pothole": "filling the pothole"}[kind]}
        self.busy[crew] = job
        self.claimed.add(fid)
        d = {"kind": "roads", "phase": "done", "by": "ai", "source": source, "tokens": tokens, "crew": CREWS[crew][0],
             "feature": fid, "defect": kind, "cell": list(cell), "reason": job["reason"], "eta_s": int(drive + work)}
        rt.decide(d)
        rt.hub.publish("crews", self.jobs())
        async with rt.pool.acquire() as c:
            await log_event(c, "ai.roads", d, run_id=rt.run_id, tick=rt.last_tick)
        asyncio.create_task(self.finish(crew, job))

    async def finish(self, crew: str, job: dict) -> None:
        await asyncio.sleep(max(1.0, job["repair_until"] - time.time()))
        try:
            await self.rt.sim.road(job["feature"], crew=CREWS[crew][0], by="ai:road-crew")
        except SimNodeError as exc:
            log.debug("repair not delivered: %s", exc)
        await asyncio.sleep(4)
        self.busy.pop(crew, None)
        self.claimed.discard(job["feature"])
        self.rt.hub.publish("crews", self.jobs())


# ---------------------------------------------------------------- log + error analysts

class ErrorTap(logging.Handler):
    """Keeps the control plane's own warnings and errors for the log analysts."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: deque[str] = deque(maxlen=3000)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(f"{time.strftime('%H:%M:%S', time.gmtime(record.created))} {record.levelname} {record.name}: "
                              f"{record.getMessage()[:400]}")
        except Exception:
            pass


ERRORS = ErrorTap()
if not logging.getLogger().handlers:   # keep warnings in the journal as well
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger().addHandler(ERRORS)

I_LOGS = """You are the operations log analyst for an autonomous open-pit mine. The whole system runs on Vultr: the control
plane and Postgres on VM A, the fleet simulator and replay workers on VM B, every AI agent on Vultr Serverless Inference.
You get a window of the event log (sim events, AI decisions and their audits, dispatches, right-of-way rulings, road
changes, incidents, replays, approvals), a stretch of the raw 10 Hz fleet telemetry (per tick, every truck as
id:x_m,y_m,speed_mm_per_tick,status[,road defect it is in]) and the control plane's warning and error log. Read all of
it and report:
summary (2 sentences with numbers), incidents (what happened, the event ids or ticks that show it), anomalies (patterns
that look wrong: trucks holding repeatedly, slow AI calls, repeated right-of-way fights, road defects nobody repairs),
errors (component, message, count, likely cause) and recommendations (concrete, short)."""
LOGS = obj({"summary": {"type": "string"},
            "incidents": {"type": "array", "items": obj({"what": {"type": "string"}, "evidence": {"type": "string"}})},
            "anomalies": {"type": "array", "items": obj({"what": {"type": "string"}, "severity": {"type": "string"}})},
            "errors": {"type": "array", "items": obj({"component": {"type": "string"}, "message": {"type": "string"},
                                                      "count": {"type": "integer"}, "cause": {"type": "string"}})},
            "recommendations": {"type": "array", "items": {"type": "string"}}})


class LogAnalysts:
    """Many analysts reading different slices of the event and error logs. Before BURST_USD is spent they run at
    high concurrency (backing off if the endpoint pushes back); afterwards one analysis every LOG_STEADY_S."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.inflight = 0
        self.limit = min(32, LOG_CONCURRENCY)
        self.ok = self.failed = 0
        self.latest: deque[dict] = deque(maxlen=12)
        self.last_steady = 0.0
        self.per_model: dict[str, int] = {m: 0 for m in LOG_MODELS}
        self.cool: dict[str, float] = {}
        self._win: str | None = None
        self._win_at = 0.0
        self._building: asyncio.Task | None = None

    @property
    def bursting(self) -> bool:
        return BURST_USD > 0 and USAGE.usd_total() < BURST_USD

    async def loop(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(.25)
            n += 1
            rt = self.rt
            if not rt.settings.inference_enabled:
                continue
            if n % 40 == 0 and self.bursting:          # every 10 s: additive increase, multiplicative decrease
                if self.failed > max(2, self.ok // 10):
                    self.limit = max(4, self.limit // 2)
                elif self.inflight >= self.limit - 1:
                    self.limit = min(LOG_CONCURRENCY, self.limit + 8)
                self.ok = self.failed = 0
            if self.bursting:
                for m in LOG_MODELS:
                    while self.per_model[m] < PER_MODEL and self.inflight < self.limit and time.monotonic() > self.cool.get(m, 0):
                        self.inflight += 1
                        self.per_model[m] += 1
                        asyncio.create_task(self.analyse(m))
            elif time.monotonic() - self.last_steady > LOG_STEADY_S and self.inflight == 0:
                self.last_steady = time.monotonic()
                self.inflight += 1
                m = LOG_MODELS[int(time.time()) % len(LOG_MODELS)]
                self.per_model[m] += 1
                asyncio.create_task(self.analyse(m))

    async def window(self) -> str:
        """One big slice of the logs, rebuilt every 20 s off the event loop and shared by every analyst; each call
        reads it from its own random starting point so no two analyses see quite the same window."""
        now = time.monotonic()
        if self._win is None or now - self._win_at > 60:
            if self._building is None:
                self._building = asyncio.create_task(self._build())
            if self._win is None:
                await self._building
        text = self._win or ""
        cut = random.randint(0, max(0, len(text) // 5))
        return f"Window {random.randint(1, 10 ** 9)} (log from offset {cut}):\n" + text[cut:]

    async def _build(self) -> None:
        rt = self.rt
        try:
            async with rt.pool.acquire() as c:
                rows = await c.fetch("SELECT id, to_char(ts, 'HH24:MI:SS') AS ts, tick, robot_id, type, payload::text AS p "
                                     "FROM events ORDER BY id DESC LIMIT 6000")
                fails = await c.fetch("SELECT id, type, robot_id, tick, status, note FROM failures ORDER BY id DESC LIMIT 40")
                ticks = await c.fetch("SELECT tick, frame::text AS f FROM ticks WHERE run_id = $1 ORDER BY tick DESC LIMIT 700",
                                      rt.run_id) if rt.run_id else []
            errs = list(ERRORS.lines)[-600:]

            def assemble() -> str:
                budget = LOG_CHARS
                parts = ["INCIDENTS\n" + "\n".join(json.dumps(dict(f), default=str) for f in fails),
                         "ERROR LOG\n" + "\n".join(errs)]
                ev = []
                for r in reversed(rows):
                    ev.append(f"{r['id']} {r['ts']} t{r['tick']} {r['robot_id'] or '-'} {r['type']} {r['p']}"[:1200])
                parts.append("EVENT LOG\n" + "\n".join(ev))
                compact, raw = [], []
                for t in reversed(ticks):
                    try:
                        robots = json.loads(t["f"]).get("robots", [])
                    except ValueError:
                        continue
                    compact.append(f"t{t['tick']} " + " ".join(f"{r['id']}:{r['x'] // 1000},{r['y'] // 1000},{r['v']},{r['st'][:5]}"
                                                            + (f",{r['hole']}" if r.get("hole") else "") for r in robots))
                parts.append("TELEMETRY (compact)\n" + "\n".join(compact))
                size = sum(len(p) for p in parts)
                for t in ticks:                     # newest raw frames until the budget is used
                    if size + len(t["f"]) > budget:
                        break
                    raw.append(f"t{t['tick']} {t['f']}")
                    size += len(t["f"])
                parts.append("RAW FRAMES\n" + "\n".join(reversed(raw)))
                return "\n\n".join(parts)[:budget]
            self._win = await asyncio.to_thread(assemble)
            self._win_at = time.monotonic()
        except Exception:
            log.exception("building the log window")
        finally:
            self._building = None

    async def analyse(self, model: str) -> None:
        rt = self.rt
        try:
            data = await self.window()
            t0 = time.monotonic()
            try:
                ans, source, tokens = await structured(rt.settings, I_LOGS, data, LOGS, "log_analyst", max_tokens=2500, timeout=240,
                                                       model=model)
            except ModelError:                  # the call went through (and is metered); the answer was not clean JSON
                self.ok += 1
                return
            self.ok += 1
            rep = {"at": time.time(), "ms": int((time.monotonic() - t0) * 1000), "tokens": tokens, "source": source,
                   "window": {"chars": len(data)}, "summary": str(ans.get("summary") or "")[:400],
                   "anomalies": (ans.get("anomalies") or [])[:5], "errors": (ans.get("errors") or [])[:5],
                   "recommendations": [str(x)[:200] for x in (ans.get("recommendations") or [])[:4]]}
            self.latest.appendleft(rep)
            rt.hub.publish("logs", rep)
            async with rt.pool.acquire() as c:
                await log_event(c, "ai.logs", rep, run_id=rt.run_id, tick=rt.last_tick)
        except Exception as exc:
            self.failed += 1
            self.cool[model] = time.monotonic() + 20      # that model's pool is busy: give it a moment
            if self.failed in (1, 5, 50) or self.failed % 200 == 0:   # HTTP errors and timeouts: the AIMD backs off
                log.warning("log analyst failed (%s: %s)", type(exc).__name__, str(exc)[:200])
            await asyncio.sleep(2)
        finally:
            self.inflight -= 1
            self.per_model[model] -= 1


# ---------------------------------------------------------------- shift supervisor

I_SUPER = """You are the shift supervisor of an open-pit mine run by autonomous haul trucks, excavators and AI agents.
Read the live state of the pit and write the supervisor's call for the next minutes: headline (one sentence on how the
shift is going, with a number), risks (up to 3 short items, the most pressing first) and actions (up to 3 short, concrete
orders to the agents or the crew). Be specific: truck ids, excavators, road segments, numbers."""
SUPER = obj({"headline": {"type": "string"}, "risks": {"type": "array", "items": {"type": "string"}},
             "actions": {"type": "array", "items": {"type": "string"}}})


# ---------------------------------------------------------------- hazard drills + autopilot

I_HAZARD = """You plan the safety drills of an autonomous mine. Every few minutes one hazard is injected into the live pit
to prove the fleet's rules and the incident pipeline (record, replay, investigate, fix, prove). Pick the next one from
the list: favour hazards whose failures are not yet fixed, vary them, and avoid piling a new one onto a pipeline that
is still busy with the same kind. Answer scenario (one of the list) and reason (at most 20 words)."""
HAZARD = obj({"scenario": {"type": "string", "enum": list(SCENARIOS)}, "reason": {"type": "string"}})

I_APPROVE = """You are the mine's safety officer reviewing a fleet rule change proposed by the incident investigator. It has
already passed: an exact replay of the incident with the fix (avoided), stress tests across variants of the incident,
and the regression suite of every earlier incident. Approve it unless the evidence shows a real problem (weak robustness,
a regression, a throughput cost out of proportion). Answer approve (true or false) and reason (at most 25 words)."""
APPROVE = obj({"approve": {"type": "boolean"}, "reason": {"type": "string"}})


class AIOps:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.drivers = Drivers(rt, self)
        self.auditor = Auditor(rt)
        self.crew = RoadCrew(rt)
        self.logs = LogAnalysts(rt)
        self.supervisor: dict | None = None
        self.next_hazard = time.time() + 90
        self.recent_hazards: deque[str] = deque(maxlen=3)
        self.reviewed: set[int] = set()
        self._rules: list[str] = []
        self._rules_at = 0.0

    def rules(self) -> list[str]:
        return self._rules

    def tasks(self) -> list[asyncio.Task]:
        return [asyncio.create_task(x) for x in (self.drivers.loop(), self.auditor.loop(), self.crew.loop(), self.logs.loop(), self.prove(),
                                                 self.supervise(), self.hazards(), self.autopilot(), self.govern(),
                                                 USAGE.loop())]

    def status(self) -> dict:
        return {"drivers": {"interval_s": round(self.drivers.interval, 2), "inflight": self.drivers.inflight,
                            "latest": list(self.drivers.latest.values())},
                "audit": self.auditor.stats, "crews": self.crew.jobs(), "supervisor": self.supervisor,
                "autopilot": AUTOPILOT, "next_hazard_s": max(0, int(self.next_hazard - time.time())),
                "logs": {"bursting": self.logs.bursting, "concurrency": self.logs.limit, "inflight": self.logs.inflight,
                         "latest": list(self.logs.latest)[:5]},
                "usage": USAGE.snapshot()}

    async def refresh_rules(self) -> None:
        if time.monotonic() - self._rules_at < 10:
            return
        from . import policies
        async with self.rt.pool.acquire() as c:
            self._rules = list((await policies.current(c))["rules"])
        self._rules_at = time.monotonic()

    async def govern(self) -> None:
        """Steer the AI drivers' cadence so the Vultr inference spend tracks the target $/hour."""
        while True:
            await asyncio.sleep(10)
            try:
                await self.refresh_rules()
            except Exception:
                pass
            # while the log analysts burn the burst budget, the pit's own agents still pace to the target
            rate = USAGE.usd_per_hour(180, exclude=("log_analyst",) if self.logs.bursting else ())
            if rate <= 0 or self.drivers.inflight >= MAX_DRIVERS_INFLIGHT:
                continue
            k = max(.7, min(1.4, (rate / USAGE.target) ** .5))
            trucks = max(1, len([r for r in self.rt.frame["robots"] if r.get("svc") != "standby"])) if self.rt.frame else 14
            floor = 5.0 / trucks                 # each truck's AI driver at most every 5 s
            self.drivers.interval = max(floor, min(20.0, self.drivers.interval * k))

    async def supervise(self) -> None:
        while True:
            await asyncio.sleep(30)
            rt = self.rt
            if not rt.sim_online or not rt.settings.inference_enabled:
                continue
            try:
                from . import enterprise
                frame = rt.frame
                counts: dict[str, int] = {}
                for r in frame["robots"]:
                    counts[r["st"]] = counts.get(r["st"], 0) + 1
                ctx = {"kpis": await enterprise.kpis(rt), "truck_status": counts, "faces": rt.faces,
                       "road_defects": [{"kind": p.get("kind", "pothole"), "at": cname(p["cell"]), "mapped": bool(p.get("known"))}
                                        for p in frame.get("potholes", [])],
                       "crews": {k: v["feature"] for k, v in self.crew.busy.items()},
                       "speed_plans": {rid: p["kmh"] for rid, p in self.drivers.latest.items()},
                       "recent_decisions": [{k: d.get(k) for k in ("kind", "robot", "face", "first", "yield", "reason", "review")
                                             if d.get(k)} for d in list(rt.decisions)[-15:]],
                       "audit": self.auditor.stats, "fleet_rules": self.rules()}
                ans, source, tokens = await structured(rt.settings, I_SUPER, ctx, SUPER, "supervisor", max_tokens=500, timeout=60)
                self.supervisor = {"headline": str(ans.get("headline") or "")[:240],
                                   "risks": [str(x)[:160] for x in (ans.get("risks") or [])][:3],
                                   "actions": [str(x)[:160] for x in (ans.get("actions") or [])][:3],
                                   "at": time.time(), "source": source}
                rt.hub.publish("supervisor", self.supervisor)
                rt.decide({"kind": "supervisor", "phase": "done", "by": "ai", "source": source, "tokens": tokens,
                           **{k: self.supervisor[k] for k in ("headline", "risks", "actions")}})
            except Exception as exc:
                log.debug("supervisor failed: %s", exc)

    async def hazards(self) -> None:
        """Keep incidents flowing: a planner model picks the next hazard drill every few minutes."""
        while True:
            await asyncio.sleep(5)
            rt = self.rt
            if time.time() < self.next_hazard or not rt.sim_online or HAZARD_EVERY_S <= 0:
                continue
            self.next_hazard = time.time() + HAZARD_EVERY_S
            try:
                async with rt.pool.acquire() as c:
                    rows = await c.fetch("SELECT type, status, count(*) AS n FROM failures WHERE created_at > now() - interval '6 hours' "
                                         "GROUP BY 1, 2")
                history = [{"failure": r["type"], "status": r["status"], "count": int(r["n"])} for r in rows]
                allowed = [x for x in SCENARIOS if x not in self.recent_hazards]   # vary the drills
                scenario, reason, source = random.choice(allowed), "rotation", "rules"
                if rt.settings.inference_enabled:
                    try:
                        ans, source, _ = await structured(rt.settings, I_HAZARD, {"scenarios": allowed, "recent_incidents": history,
                                                                                  "fleet_rules": self.rules()},
                                                          HAZARD, "hazard_planner", max_tokens=150, timeout=15)
                        if ans.get("scenario") in allowed:
                            scenario, reason = ans["scenario"], str(ans.get("reason") or "")[:200]
                    except Exception as exc:
                        log.debug("hazard planner failed: %s", exc)
                self.recent_hazards.append(scenario)
                res = await rt.sim.chaos(scenario, wait_ticks=1200)   # up to 2 min for a truck to be in position
                async with rt.pool.acquire() as c:
                    await log_event(c, "chaos.requested", {"scenario": scenario, "by": f"ai:{source}", "reason": reason, **res},
                                    run_id=rt.run_id, tick=rt.last_tick)
                rt.decide({"kind": "hazard", "phase": "done", "by": "ai" if source != "rules" else "rules", "source": source,
                           "scenario": scenario, "reason": reason})
            except Exception as exc:
                log.warning("hazard drill failed: %s", str(exc)[:160])

    async def prove(self) -> None:
        """VM B's replay workers never idle: whenever the queue is empty, every approved fix's incident is replayed
        again against the current fleet rules (the regression suite, on the workers' CPUs)."""
        while True:
            await asyncio.sleep(20)
            rt = self.rt
            if rt.orchestrator is None:
                continue
            try:
                async with rt.pool.acquire() as c:
                    busy = await c.fetchval("SELECT count(*) FROM replays WHERE status IN ('queued', 'running')")
                if busy == 0 and await rt.orchestrator.run_suite():
                    rt.orchestrator.kick()
            except Exception:
                log.exception("continuous regression suite")

    async def autopilot(self) -> None:
        """Fixes that passed every gate: the safety-officer model reviews the evidence and approves."""
        while True:
            await asyncio.sleep(15)
            rt = self.rt
            if not AUTOPILOT or rt.orchestrator is None:
                continue
            try:
                async with rt.pool.acquire() as c:
                    rows = await c.fetch("SELECT h.id, h.fix_dsl, h.gate, h.rationale, h.evidence, f.type, f.robot_id FROM hypotheses h "
                                         "JOIN capsules k ON k.id = h.capsule_id JOIN failures f ON f.id = k.failure_id "
                                         "WHERE h.status = 'ready' ORDER BY h.id LIMIT 3")
                for h in rows:
                    if h["id"] in self.reviewed:
                        continue
                    self.reviewed.add(h["id"])
                    gate = h["gate"] or {}
                    evidence = {"fix": h["fix_dsl"], "incident": h["type"], "truck": h["robot_id"], "rationale": h["rationale"],
                                "gate": {k: v for k, v in gate.items() if k not in ("trial_replay",)},
                                "stress_test": h["evidence"], "current_rules": self.rules()}
                    ok, reason, source = True, "passed every gate", "rules"
                    if rt.settings.inference_enabled:
                        try:
                            ans, source, _ = await structured(rt.settings, I_APPROVE, evidence, APPROVE, "autopilot",
                                                              max_tokens=200, timeout=20)
                            ok, reason = bool(ans.get("approve")), str(ans.get("reason") or "")[:200]
                        except Exception as exc:
                            log.debug("autopilot review failed: %s", exc)
                    rt.decide({"kind": "autopilot", "phase": "done", "by": "ai" if source != "rules" else "rules", "source": source,
                               "fix": h["fix_dsl"], "verdict": "approved" if ok else "held for a person", "reason": reason})
                    if ok:
                        try:
                            await rt.orchestrator.approve(h["id"], f"autopilot · {source}")
                        except Exception as exc:
                            log.info("autopilot approve %s: %s", h["id"], exc)
                            self.reviewed.discard(h["id"])   # e.g. policy moved on: re-check later
            except Exception:
                log.exception("autopilot")


__all__ = ["AIOps", "SCENARIOS", "driver_context"]
