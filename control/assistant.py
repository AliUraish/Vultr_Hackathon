"""Operations copilot: answers a controller's question about one haul truck or the whole pit.

The answer is grounded in live data only: the latest frame from the fleet, the truck's recent
events from the log, its sensors (derived from the frame exactly as the sim senses: lidar,
potholes, stopping distance), its load ticket and material, the asset register, the excavators,
open incidents, recent AI decisions and the fleet rules. It explains; it never acts. Every
question and answer is written to the event log.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from typing import Any

import httpx

from replay_core.world import (CELL, DEFAULT_CLEARANCE, MAX_SPEED, MAX_SPEED_LOADED, PALLET_HALF, ROBOT_HALF,
                               SENSOR_RANGE, TICK_HZ, W, kmh)

from . import enterprise, policies
from .db import log_event
from .diagnosis import _pick_model, _stopping_m
from .runtime import Runtime
from .usage import USAGE

log = logging.getLogger("replay.assistant")

MAX_PER_MINUTE = 12
_recent: dict[str, deque] = defaultdict(deque)

RULES = (f"How the fleet drives (deterministic sim): trucks plan the cheapest road route (A*: bends, driving against "
         f"a two-lane road's direction (keep left), known potholes and parked trucks cost extra), reserve up to 2 "
         f"road segments (20 m each) ahead and never enter a segment another truck holds; a one-lane cut is taken "
         f"whole. A truck that meets a held segment stops and waits; behind a truck being loaded it queues. When two "
         f"trucks meet head-on (a standoff) the traffic AI rules who goes first (loaded trucks have right of way); "
         f"if it has not ruled after 8 s the fallback rule does (the higher truck number yields). Speeds: up to "
         f"{kmh(MAX_SPEED):.0f} km/h empty, {kmh(MAX_SPEED_LOADED):.0f} km/h loaded, 18 km/h through bends, 7 km/h "
         f"through a pothole. Lidar sees {SENSOR_RANGE / 1000:.0f} m; trucks brake at 2 m/s^2 and keep "
         f"{DEFAULT_CLEARANCE / 1000:.0f} m clearance. Loads are dispatched by the dispatch AI (rules as fallback).")


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
    """What the truck senses and must respect right now (same geometry as the sim)."""
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
                nearest = (gap, f"fallen rock at c{p['cell'][0]}_{p['cell'][1]}")
    route = [tuple(c) for c in r.get("path", [])[:4]]
    holes = [{"at": f"c{p['cell'][0]}_{p['cell'][1]}", "depth_m": round(p["depth"] / 1000, 1), "on_route": tuple(p["cell"]) in route,
              "distance_m": round(((p["cell"][0] * CELL + CELL // 2 - r["x"]) ** 2 + (p["cell"][1] * CELL + CELL // 2 - r["y"]) ** 2) ** 0.5 / 1000)}
             for p in frame.get("potholes", []) if p.get("known")]
    holes = sorted((h for h in holes if h["distance_m"] <= SENSOR_RANGE / 1000 * 2), key=lambda h: h["distance_m"])[:4]
    for o in frame["robots"]:
        if o["id"] == rid:
            continue
        ahead, side = (o["x"] - r["x"]) * dx + (o["y"] - r["y"]) * dy, abs((o["x"] - r["x"]) * dy - (o["y"] - r["y"]) * dx)
        if ahead > 0 and side < 2 * ROBOT_HALF:
            gap = ahead - 2 * ROBOT_HALF
            if nearest is None or gap < nearest[0]:
                nearest = (gap, f"truck {o['id']}")
    caps = []
    for rule in rules:
        if rule.startswith("speed_cap("):
            zone, v = [a.strip() for a in rule[len("speed_cap("):-1].split(",")]
            if zone in zones:
                caps.append(float(v))
    stop = _stopping_m(r["v"]) + DEFAULT_CLEARANCE / 1000
    return {
        "cell": f"c{cell[0]}_{cell[1]}", "zones": [z for z in zones if z not in ("haul_roads", "ramps", "benches")] or ["road"],
        "heading": r["dir"], "speed_kmh": round(kmh(r["v"]), 1), "status": r["st"], "loaded": bool(r.get("carry")),
        "forward_obstacle": ({"what": nearest[1], "gap_m": round(max(0, nearest[0]) / 1000, 1),
                              "within_lidar_range": nearest[0] <= SENSOR_RANGE} if nearest and nearest[0] < 150_000 else None),
        "potholes_nearby": holes, "in_pothole": r.get("hole") or None,
        "stopping_distance_m": round(stop, 1), "lidar_range_m": ((r.get("health") or {}).get("range") or SENSOR_RANGE) / 1000,
        "can_stop_within_lidar_range": stop <= SENSOR_RANGE / 1000,
        "speed_limit_kmh": min(caps) if caps else round(kmh(MAX_SPEED_LOADED if r.get("carry") else MAX_SPEED)),
        "reserved_cells": [f"c{c[0]}_{c[1]}" for c in r.get("res", [])],
        "waiting_on": r.get("wait_on") or None,
        "route_next_cells": [f"c{c[0]}_{c[1]}" for c in r.get("path", [])[:8]],
        "current_step": r.get("op"), "goal_cell": f"c{r['goal'][0]}_{r['goal'][1]}" if r.get("goal") else None,
        "odometer_km": round(r.get("odo", 0) / 1_000_000, 2),
        "carrying": r.get("carry", []),
    }


def _brief(payload: dict) -> dict:
    keep = ("robot", "on", "to", "with", "rule", "waited", "after", "cell", "slot", "sku", "job", "dock", "ok",
            "expected", "actual", "zone", "reason", "v", "type", "scenario", "behind", "depth", "id", "by", "face", "road_m")
    return {k: v for k, v in (payload or {}).items() if k in keep}


# ---------------------------------------------------------------- context

async def context(rt: Runtime, scope: str, rid: str | None) -> dict:
    site = rt.site or {}
    frame = rt.frame or {"robots": [], "pallets": [], "restricted": []}
    catalog = site.get("catalog", {})
    exc = {e["slot"]: e for e in site.get("excavators", [])}
    dest = {c["dock"]: c["carrier"] for c in site.get("carriers", [])}

    def item(sku: str) -> str:
        it = catalog.get(sku.replace("MAT-", ""))
        return f"{it['name']} ({it['sku']}, face {it['slot']} / {exc.get(it['slot'], {}).get('id', '')})" if it else sku

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
            "('sim.wait', 'sim.standoff', 'sim.yield', 'sim.queue', 'input.chaos', 'dispatch', 'ai.dispatch', 'ai.traffic', "
            "'traffic.rule', 'sim.pothole_detected') ORDER BY id DESC LIMIT 30", rt.run_id)]
        mine = [dict(r) for r in await c.fetch(
            "SELECT tick, type, payload FROM events WHERE run_id = $1 AND robot_id = $2 "
            "ORDER BY id DESC LIMIT 40", rt.run_id, rid)] if rid else []

    def activity(r: dict, job: dict | None) -> str:
        """What the truck is doing, in words, from the same state the 3D view reads."""
        goal = r.get("goal")
        slot = next((s for s, v in W.slots.items() if goal and list(v["access"]) == list(goal)), None)
        dock = next((d for d, c in W.docks.items() if goal and list(c) == list(goal)), None)
        st = r["st"]
        if st in ("loading", "grade_check"):
            what = "being loaded by" if st == "loading" else "grade check at"
            return f"{what} {exc.get(slot, {}).get('id', 'the excavator')} (face {slot}): {item(W.slots[slot]['sku']) if slot else ''}"
        if st == "dumping":
            return f"tipping its load at {dest.get(dock, dock or 'the dump')}"
        if st == "queued":
            return f"queued behind {r.get('wait_on')} for the excavator (normal, not a fault)"
        if st == "waiting":
            return f"holding: {r.get('wait_on') or 'another truck'} has the next road segment (traffic rule, not a fault)"
        if st == "held":
            return "holding outside a road closed for blasting until it reopens"
        if st == "blocked":
            return "stopped: a fallen rock blocks its route"
        if st == "estop":
            return "emergency stop"
        if st == "fault":
            return f"broken down ({(r.get('fault') or {}).get('type', 'fault')}); a service case is open"
        if st == "moving" and r.get("hole"):
            return "crawling through a pothole at 7 km/h"
        if st == "moving" and slot:
            return f"driving empty to {exc.get(slot, {}).get('id', 'face ' + slot)} to load {item(W.slots[slot]['sku'])}"
        if st == "moving" and dock:
            return f"hauling {', '.join(item(x) for x in r.get('carry', []))} to {dest.get(dock, dock)}"
        if st == "moving":
            return "driving to the truck park" if goal and tuple(goal) in set(map(tuple, W.homes)) else "repositioning"
        if st == "standby":
            return "standby spare, parked in the workshop"
        return "idle, waiting for a load" if not job else f"about to start {job['id']}"

    def robot_view(r: dict) -> dict:
        job = jobs.get(r.get("job") or "")
        asset = next((a for a in site.get("robots", []) if a["id"] == r["id"]), {})
        return {
            "id": r["id"], "model": asset.get("model"), "serial": asset.get("serial"), "firmware": asset.get("firmware"),
            "payload_t": asset.get("payload_t"), "status": r["st"], "activity": activity(r, job),
            **sensors(frame, r["id"], list(pol["rules"])),
            "carrying": [item(s) for s in r.get("carry", [])],
            "load_ticket": ({"id": job["id"], "face": [item(line["sku"]) for line in job["lines"]],
                             "destination": dest.get(job["dock"], job["dock"]), "ticket": job["order_id"],
                             "for": job["customer"], "value_usd": float(job["value"]) if job["value"] is not None else None,
                             "priority": job["priority"]} if job else None),
        }

    t = rt.last_tick
    out: dict[str, Any] = {
        "now": {"tick": t, "sim_seconds": round(t / TICK_HZ, 1)},
        "site": {"company": site.get("facility", {}).get("company"), "mine": site.get("name"),
                 "code": site.get("code"), "location": site.get("facility", {}).get("location"),
                 "commodity": site.get("facility", {}).get("commodity"), "destinations": site.get("carriers")},
        "excavators": [{"id": exc.get(s, {}).get("id", s), "face": s, "material": catalog.get(s, {}).get("name"),
                        "trucks_coming": (rt.faces.get(s) or {}).get("trucks", []), "alone_s": (rt.faces.get(s) or {}).get("alone_s", 0)}
                       for s in sorted(W.slots)],
        "recent_ai_decisions": [{k: d.get(k) for k in ("kind", "robot", "first", "yield", "reason", "by")}
                                for d in list(rt.decisions)[-12:] if d.get("phase") == "done"],
        "kpis": {k: v for k, v in kpi.items() if k != "last_safety_incident"},
        "policy": {"version": pol["version"], "rules": pol["rules"], "signed": bool(pol.get("signature"))},
        "traffic_rules": RULES,
        "open_incidents": [i for i in incidents if i["status"] not in ("fixed", "dismissed", "lost")],
        "recent_incidents": incidents,
        "rocks_on_roads": [f"c{p['cell'][0]}_{p['cell'][1]}" for p in frame.get("pallets", [])],
        "known_potholes": [f"c{p['cell'][0]}_{p['cell'][1]}" for p in frame.get("potholes", []) if p.get("known")],
        "roads_closed_for_blasting": [z["zone"] for z in frame.get("restricted", [])],
    }
    if scope == "robot" and rid:
        r = next((x for x in frame["robots"] if x["id"] == rid), None)
        if r is None:
            raise AskError(f"no live truck {rid}")
        out["robot"] = robot_view(r)
        out["truck_recent_events"] = [{"t": e["tick"], "type": e["type"], **_brief(e["payload"])} for e in reversed(mine)]
        out["other_trucks"] = [{"id": o["id"], "status": o["st"], "cell": f"c{o['x'] // CELL}_{o['y'] // CELL}"}
                               for o in frame["robots"] if o["id"] != rid]
    else:
        out["fleet"] = [robot_view(r) for r in frame["robots"]]
    out["fleet_coordination_recent"] = [{"t": e["tick"], "robot": e["robot_id"], "type": e["type"], **_brief(e["payload"])}
                                        for e in reversed(radio)]
    return out


INSTRUCTIONS = """You are the operations copilot of an open-pit mine run by autonomous haul trucks. A controller is
watching the live 3D pit and asks about {what}. Answer from the live data provided (JSON): truck telemetry and lidar,
potholes, the event log, load tickets, excavators, incidents, recent AI dispatch and traffic decisions, and the fleet
rules. Be specific and concrete: truck ids, road segments (cX_Y), bench roads, excavators, materials, tonnes, km/h,
seconds. A truck's `activity` field says what it is doing right now; trust it over inferring from raw fields. Explain
traffic decisions with the rules given. Keep it under 110 words unless asked for more; plain sentences, at most a
short list. If the data does not say, say so; never invent readings. You explain; you cannot command trucks."""


# ---------------------------------------------------------------- answering

def _fallback(ctx: dict) -> str:
    r = ctx.get("robot")
    if r:
        bits = [f"{r['id']} is {r['activity']}, at {r['cell']} ({', '.join(r['zones'])}), {r['speed_kmh']} km/h heading {r['heading']}."]
        if r.get("waiting_on"):
            bits.append(f"It is holding for {r['waiting_on']}, which has the next road segment.")
        if r.get("load_ticket"):
            j = r["load_ticket"]
            bits.append(f"Load {j['id']} for {j['for'] or 'the mine'}: {', '.join(j['face'])} to {j['destination']}.")
        if r["carrying"]:
            bits.append(f"Carrying {', '.join(r['carrying'])}.")
        return " ".join(bits) + " (Model not configured: this is a rule-based summary.)"
    k = ctx["kpis"]
    return (f"{k['fleet_busy']} of {k['fleet_size']} trucks hauling, {k['orders_per_hour']} loads/hour, "
            f"{k['open']} loads in progress, {k['incidents_open']} open incidents. (Model not configured.)")


async def answer(rt: Runtime, question: str, scope: str, rid: str | None, history: list[dict],
                 transport: httpx.AsyncBaseTransport | None = None) -> dict:
    ctx = await context(rt, scope, rid)
    s = rt.settings
    if not s.inference_enabled:
        return {"answer": _fallback(ctx), "source": "rules", "tokens": 0}
    what = f"haul truck {rid}" if scope == "robot" else "the whole pit"
    turns = [{"role": h["role"], "content": str(h["text"])[:800]} for h in history[-6:]
             if h.get("role") in ("user", "assistant") and h.get("text")]
    data = "LIVE DATA\n" + json.dumps(ctx, default=str)
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {s.inference_key}"}, timeout=60.0,
                                 transport=transport) as http:
        model = await _pick_model(http, s)
        body = {"model": model, "temperature": 0.2, "max_tokens": 500,
                "messages": [{"role": "system", "content": INSTRUCTIONS.format(what=what)},
                             {"role": "user", "content": data}, *turns, {"role": "user", "content": question}]}
        r = await http.post(f"{s.inference_url}/chat/completions", json=body)
        r.raise_for_status()
        out = r.json()
        text = out["choices"][0]["message"]["content"] or ""
        u = out.get("usage") or {}
        tokens = int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
        USAGE.record("copilot", int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
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
