"""The deterministic fleet step.

`step(state, inputs)` applies one tick's recorded inputs and advances the mine by
one fixed timestep. It is pure integer math over a JSON-pure dict and never reads
a clock, the network or unseeded randomness, so the same state plus the same
inputs always yields the same next state.

Motion model: haul trucks follow A* routes from road segment to road segment.
They drive along the route and take bends at corner speed, accelerate and brake
at ACCEL, reserve segments ahead so two trucks never share one, and only learn
about fallen rocks and potholes through their lidar. A truck that cannot brake
within the gap to a rock hits it; a known pothole is crossed at crawl speed or
avoided when the road allows.

Right of way: when two trucks meet head-on, the traffic AI on the control plane
rules who yields (a recorded `traffic` input, so replays stay exact); if it has
not ruled after AI_TRAFFIC_TICKS, the fallback rule decides.
"""
from __future__ import annotations

from typing import Any

from . import world as wd
from .nav import astar
from .policy import PolicyError, make_policy
from .rng import _next, randint
from .world import W, cell_at, center

State = dict[str, Any]
Event = dict[str, Any]
Cell = tuple[int, int]

_DIR_NAMES: dict[tuple[int, int], str] = {(1, 0): "E", (-1, 0): "W", (0, 1): "S", (0, -1): "N"}
_DIR_VEC: dict[str, tuple[int, int]] = {v: k for k, v in _DIR_NAMES.items()}
_WORK_STATUS = {"pick": "loading", "scan": "grade_check", "drop": "dumping"}


def _t(c: list[int]) -> Cell:
    return (c[0], c[1])


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


def _at(r: dict, c: list[int]) -> bool:
    cx, cy = center(c)
    return r["x"] == cx and r["y"] == cy


def _cell(r: dict) -> list[int]:
    return cell_at(r["x"], r["y"])


# ---------------------------------------------------------------- public API

def step(state: State, inputs: list[dict]) -> list[Event]:
    """Advance one tick. Returns the events emitted during it."""
    events: list[Event] = []
    for inp in inputs:
        _apply_input(state, inp, events)
    _expire_restrictions(state, events)
    occ = _occupancy(state)
    for rid in sorted(state["robots"]):
        _robot_tick(state, state["robots"][rid], occ, events)
    _weather(state, events)
    _lidar(state, events)
    _holes(state, events)
    _contacts(state, events)
    _zones(state, events)
    state["tick"] += 1
    return events


def set_policy(state: State, policy: dict, events: list[Event]) -> None:
    """Swap the fleet policy. Trucks re-route only if the avoid set changed."""
    old_avoid = state["policy"]["c"]["avoid"]
    state["policy"] = policy
    if policy["c"]["avoid"] != old_avoid:
        for rid in sorted(state["robots"]):
            r = state["robots"][rid]
            if r["step"] is not None and r["step"]["op"] == "goto":
                r["replan"] = True
    events.append({"type": "policy_applied", "version": policy["version"], "hash": policy["hash"]})


def make_frame(state: State, events: list[Event], inputs: list[dict]) -> dict:
    """Compact per-tick view: what telemetry streams, the UI draws and the detector reads."""
    robots = []
    for rid in sorted(state["robots"]):
        r = state["robots"][rid]
        s = r["step"]
        free = (
            r["estop"] == 0
            and not r.get("fault") and not r.get("svc")
            and (s is None or s.get("internal", False))
            and all(q.get("internal", False) for q in r["queue"])
        )
        robots.append({
            "id": rid, "x": r["x"], "y": r["y"], "v": r["v"], "dir": r["dir"],
            "st": r["status"], "job": r["job"], "blk": r["blk"], "free": free,
            "carry": [i["sku"] for i in r["carry"]],
            "path": r["path"][r["k"]:r["k"] + 16] if r["path"] else [],
            "res": [list(c) for c in r["res"]], "wait_on": r["wait_on"], "odo": r["odo"],
            "op": s["op"] if s else None, "goal": (s.get("cell") or (W.slots[s["slot"]]["access"] if s.get("slot") in W.slots
                                                 else W.docks.get(s.get("dock")))) if s else None,
            "fault": r.get("fault"), "svc": r.get("svc"), "health": _health(state, r), "hole": r.get("hole", ""),
            "adv": r["adv"] if r.get("adv") and state["tick"] < r.get("adv_until", 0) else 0,
        })
    known = {_t(c) for c in state["holes_known"]}
    return {
        "t": state["tick"],
        "robots": robots,
        "pallets": [dict(p) for p in state["pallets"]],
        "potholes": [{**p, "known": _t(p["cell"]) in known} for p in state["potholes"]],
        "restricted": [dict(z) for z in state["restricted"]],
        "jobs": [{"id": j["id"], "robot": j["robot"], "deadline": j["deadline"]}
                 for _, j in sorted(state["jobs"].items())],
        "ev": events,
        "in": inputs,
    }


def _jitter(rid: str, tick: int, k: int, amp: float) -> float:
    """Deterministic sensor noise: a pure function of truck, time and channel (no RNG state consumed)."""
    h = (sum(ord(ch) * 131 for ch in rid) * (k + 17) + (tick // 10) * 2654435761 + k * 97) % 10007
    return (h / 10007 - 0.5) * amp


def _health(st: State, r: dict) -> dict:
    """On-board health telemetry: tyre pressures (psi), wheel slip (%), drive current imbalance (%),
    chassis vibration (g), lidar return rate (%) and usable range (mm). Faults show up here first."""
    t, rid = st["tick"], r["id"]
    tires = {w: round(105 + _jitter(rid, t, i, 3.0), 1) for i, w in enumerate(wd.WHEELS)}
    moving = r["v"] > 0
    rough = bool(r.get("hole"))
    slip = round(1.2 + abs(_jitter(rid, t, 7, 1.6)) + (0.8 if moving else 0), 1)
    imbalance = round(2 + abs(_jitter(rid, t, 8, 2.5)), 1)
    vib = round(0.08 + abs(_jitter(rid, t, 9, 0.05)) + (0.06 if moving else 0) + (0.35 if rough else 0), 2)
    lidar = round(98.4 + _jitter(rid, t, 10, 1.8), 1)
    rng = wd.SENSOR_RANGE
    f = r.get("fault")
    if f:
        age = t - f.get("since", t)
        if f["type"] == "tire":
            w = f.get("wheel") or "FL"
            tires[w] = round(max(18.0, 105 - age * 2.6 + _jitter(rid, t, 11, 1.0)), 1)
            slip = round(21 + abs(_jitter(rid, t, 12, 6)) + (9 if moving else 0), 1)
            imbalance = round(34 + abs(_jitter(rid, t, 13, 8)), 1)
            vib = round(0.62 + abs(_jitter(rid, t, 14, 0.3)) + (0.5 if moving else 0), 2)
        elif f["type"] == "sensor":
            lidar = round(27 + _jitter(rid, t, 15, 8), 1)
            rng = wd.FAULT_SENSOR_RANGE
    return {"tires": tires, "slip": slip, "imbalance": imbalance, "vib": vib, "lidar": lidar, "range": rng}


# ---------------------------------------------------------------- inputs

def _apply_input(st: State, inp: dict, ev: list[Event]) -> None:
    kind = inp.get("kind")
    if kind == "cmd":
        _apply_cmd(st, inp, ev)
    elif kind == "cancel":
        r = st["robots"].get(inp.get("robot"))
        if r is not None and r["job"]:
            _abandon_job(st, r, ev, r["job"], "cancelled")
    elif kind == "chaos":
        _apply_chaos(st, inp, ev)
    elif kind == "service":
        _apply_service(st, inp, ev)
    elif kind == "traffic":
        _apply_traffic(st, inp, ev)
    elif kind == "advice":
        _apply_advice(st, inp, ev)
    elif kind == "road":
        _apply_road(st, inp, ev)
    elif kind == "policy":
        try:
            pol = make_policy(inp["rules"], inp["version"])
        except (PolicyError, KeyError) as exc:
            ev.append({"type": "policy_rejected", "version": inp.get("version"), "error": str(exc)})
            return
        set_policy(st, pol, ev)
    else:
        ev.append({"type": "input_ignored", "kind": kind})


def _apply_cmd(st: State, inp: dict, ev: list[Event]) -> None:
    r = st["robots"].get(inp.get("robot"))
    if r is None:
        ev.append({"type": "input_ignored", "kind": "cmd", "robot": inp.get("robot")})
        return
    steps = [dict(s) for s in inp.get("steps", [])]
    job = inp.get("job")
    jid = None
    if job:
        jid = job["id"]
        st["jobs"][jid] = {
            "id": jid, "robot": r["id"], "kind": job.get("kind", "single"),
            "skus": sorted(job["skus"]), "dock": job["dock"],
            "deadline": int(job["deadline"]), "status": "active",
        }
        for s in steps:
            s["job"] = jid
        steps = _reorder(st, r, steps, st["jobs"][jid]["kind"])
    if r["step"] is not None and r["step"].get("internal"):
        r["step"] = None  # new work preempts the trip to the park; motion state is kept
    r["queue"] = [q for q in r["queue"] if not q.get("internal")] + steps
    ev.append({"type": "cmd", "robot": r["id"], "job": jid, "ops": [s["op"] for s in steps],
               **({"by": inp["by"]} if inp.get("by") else {})})


def _reorder(st: State, r: dict, steps: list[dict], kind: str) -> list[dict]:
    """Apply reorder_steps to the leading (goto, pick) pairs of a job."""
    rules = st["policy"]["c"]["reorder"]
    strategy = rules.get(kind) or rules.get("*") or "as_given"
    groups: list[list[dict]] = []
    i = 0
    while i + 1 < len(steps) and steps[i]["op"] == "goto" and steps[i + 1]["op"] == "pick":
        groups.append(steps[i:i + 2])
        i += 2
    if strategy == "as_given" or len(groups) < 2:
        return steps
    ordered: list[list[dict]] = []
    here = _cell(r)
    remaining = list(range(len(groups)))
    while remaining:
        def dist(gi: int, here: list[int] = here) -> int:
            c = groups[gi][0]["cell"]
            return abs(c[0] - here[0]) + abs(c[1] - here[1])
        sign = 1 if strategy == "nearest_first" else -1
        pick = min(remaining, key=lambda gi: (sign * dist(gi), gi))
        remaining.remove(pick)
        ordered.append(groups[pick])
        here = groups[pick][0]["cell"]
    return [s for g in ordered for s in g] + steps[i:]


def _apply_chaos(st: State, inp: dict, ev: list[Event]) -> None:
    typ = inp.get("type")
    if typ == "spawn_rock":
        st["pallet_seq"] += 1
        pid = f"K{st['pallet_seq']}"
        cell = [inp["cell"][0], inp["cell"][1]]
        st["pallets"].append({"id": pid, "cell": cell})
        ev.append({"type": "rock_fell", "id": pid, "cell": list(cell)})
        px, py = center(cell)
        reach = wd.ROBOT_HALF + wd.PALLET_HALF
        for rid in sorted(st["robots"]):
            r = st["robots"][rid]
            if abs(r["x"] - px) < reach and abs(r["y"] - py) < reach:
                r["contact"].append(pid)  # already touching: no "contact" (collision) event
                r["estop"], r["v"] = wd.ESTOP_TICKS, 0
                ev.append({"type": "struck", "robot": rid, "by": pid})
                _learn_obstacle(st, cell)
    elif typ == "clear_rocks":
        ids = set(inp.get("ids") or [p["id"] for p in st["pallets"]])
        gone = [p["cell"] for p in st["pallets"] if p["id"] in ids]
        st["pallets"] = [p for p in st["pallets"] if p["id"] not in ids]
        st["known"] = [c for c in st["known"] if c not in gone]
        ev.append({"type": "rocks_cleared", "ids": sorted(ids)})
    elif typ == "spawn_pothole":
        cell = [inp["cell"][0], inp["cell"][1]]
        if not W.passable(_t(cell)) or any(p["cell"] == cell for p in st["potholes"]):
            ev.append({"type": "input_ignored", "kind": "chaos", "chaos": typ})
            return
        st["hole_seq"] += 1
        hid = f"H{st['hole_seq']}"
        st["potholes"].append({"id": hid, "cell": cell, "depth": int(inp.get("depth", 700)), "fresh": True})
        ev.append({"type": "pothole_formed", "id": hid, "cell": list(cell), "depth": int(inp.get("depth", 700))})
    elif typ == "fill_potholes":
        gone = [p["cell"] for p in st["potholes"] if p.get("fresh")]
        st["potholes"] = [p for p in st["potholes"] if not p.get("fresh")]
        st["holes_known"] = [c for c in st["holes_known"] if c not in gone]
        ev.append({"type": "potholes_filled", "cells": gone})
    elif typ == "mislabel":
        slot = inp["slot"]
        st["inventory"][slot]["sku"] = inp["sku"]
        ev.append({"type": "grade_mislabeled", "slot": slot, "sku": inp["sku"]})
    elif typ == "relabel":
        slots = inp.get("slots") or sorted(st["inventory"])
        for s in slots:
            st["inventory"][s]["sku"] = W.slots[s]["sku"]
        ev.append({"type": "grades_relabeled", "slots": slots})
    elif typ == "restrict_zone":
        zone = inp["zone"]
        until = st["tick"] + int(inp.get("ticks", 450))
        st["restricted"] = [z for z in st["restricted"] if z["zone"] != zone]
        st["restricted"].append({"zone": zone, "until": until})
        ev.append({"type": "zone_restricted", "zone": zone, "until": until})
        for rid in sorted(st["robots"]):
            r = st["robots"][rid]
            if r["step"] is not None and r["step"]["op"] == "goto" and r["path"]:
                closed = _closed_cells(st, r)
                if closed and any(_t(c) in closed for c in r["path"][r["k"]:]):
                    r["replan"] = True
    elif typ == "lift_zones":
        st["restricted"] = []
        ev.append({"type": "zones_lifted"})
    elif typ == "robot_fault":
        _apply_fault(st, inp, ev)
    else:
        ev.append({"type": "input_ignored", "kind": "chaos", "chaos": typ})


_FAULT_CODE = {"tire": "E-DRV-217 traction anomaly", "sensor": "E-PER-104 perception degraded"}


def _apply_fault(st: State, inp: dict, ev: list[Event]) -> None:
    """A hardware fault: the truck safety-stops, releases its load ticket to the fleet and raises an alarm.
    The alarm code says what the truck noticed, not why; diagnosis reads the health telemetry."""
    r = st["robots"].get(inp.get("robot"))
    kind = inp.get("fault")
    if r is None or r.get("fault") or r.get("svc") or kind not in wd.FAULT_SPEED:
        ev.append({"type": "input_ignored", "kind": "fault", "robot": inp.get("robot")})
        return
    r["fault"] = {"type": kind, "since": st["tick"], **({"wheel": inp.get("wheel") or "FL"} if kind == "tire" else {})}
    r["svc"] = "fault"
    jid = r["job"]
    if jid:
        st["jobs"].pop(jid, None)
        ev.append({"type": "job_released", "robot": r["id"], "job": jid, "held": [i["sku"] for i in r["carry"]]})
    r.update(v=0, step=None, queue=[], job=None, path=[], k=0, replan=False, wait=0, wait_on="", blk=0,
             status="fault", phase="", timer=0)
    r["res"] = _overlap_cells(r)
    ev.append({"type": "fault_alarm", "robot": r["id"], "cell": _cell(r), "code": _FAULT_CODE[kind], "job": jid})


def _apply_service(st: State, inp: dict, ev: list[Event]) -> None:
    """Service commands from the control plane: pull over / drive to the workshop (remote control),
    park as standby, deploy a standby truck, or mark a repair done."""
    r = st["robots"].get(inp.get("robot"))
    op = inp.get("op")
    if r is None or op not in ("move", "standby", "deploy", "repair"):
        ev.append({"type": "input_ignored", "kind": "service", "robot": inp.get("robot"), "op": op})
        return
    if op == "move":
        cell = inp.get("cell")
        if not (isinstance(cell, list) and len(cell) == 2 and W.passable((cell[0], cell[1]))):
            ev.append({"type": "input_ignored", "kind": "service", "robot": r["id"], "op": op})
            return
        r["step"] = {"op": "goto", "cell": [cell[0], cell[1]], "svc": True}
        r["queue"], r["replan"], r["held"] = [], True, False
        r["svc"] = "remote"
        ev.append({"type": "service_move", "robot": r["id"], "cell": [cell[0], cell[1]], "by": inp.get("by", "")})
    elif op == "standby":
        r.update(svc="standby", status="standby", v=0, step=None, queue=[], job=None, path=[], k=0)
        r["home"] = _cell(r)
        r["res"] = _overlap_cells(r)
        ev.append({"type": "standby", "robot": r["id"], "cell": _cell(r)})
    elif op == "deploy":
        if r.get("fault"):
            ev.append({"type": "input_ignored", "kind": "service", "robot": r["id"], "op": op})
            return
        r.pop("svc", None)
        r["status"], r["idle"] = "idle", 0
        ev.append({"type": "deployed", "robot": r["id"], "cell": _cell(r)})
    else:  # repair
        f = r.pop("fault", None)
        held = [i["sku"] for i in r["carry"]]
        r["carry"] = []  # a load on board was tipped at the workshop during the repair
        if r.get("svc") in ("fault", "remote"):
            r.pop("svc", None)
            r["status"], r["step"], r["path"], r["k"] = "idle", None, [], 0
        ev.append({"type": "repaired", "robot": r["id"], "fault": (f or {}).get("type"), "by": inp.get("by", ""),
                   "returned": held})


def _apply_traffic(st: State, inp: dict, ev: list[Event]) -> None:
    """A right-of-way ruling from the traffic AI: `robot` yields to `to` (re-routes, or pulls aside)."""
    r = st["robots"].get(inp.get("robot"))
    other = st["robots"].get(inp.get("to"))
    if r is None or other is None or r is other or inp.get("op") != "yield":
        ev.append({"type": "input_ignored", "kind": "traffic", "robot": inp.get("robot")})
        return
    if r["wait_on"] != other["id"] or r["v"] != 0 or r["step"] is None or r["step"]["op"] != "goto":
        ev.append({"type": "traffic_stale", "robot": r["id"], "to": other["id"], "by": inp.get("by", "")})
        return
    r["avoid_tmp"] = [list(c) for c in other["res"]]
    r["yield_from"] = other["id"]
    r["replan"] = True
    ev.append({"type": "yield", "robot": r["id"], "to": other["id"], "rule": "ai", "waited": r["wait"],
               "cell": _cell(r), "reason": str(inp.get("reason", ""))[:240], "by": inp.get("by", "")})


def _apply_advice(st: State, inp: dict, ev: list[Event]) -> None:
    """The truck's AI driver sets its speed for the next few seconds. Advice can only slow a truck (the physics
    and the fleet rules still cap it), never below ADVICE_MIN, and lapses unless the driver renews it."""
    r = st["robots"].get(inp.get("robot"))
    if r is None or r.get("svc") == "standby":
        ev.append({"type": "input_ignored", "kind": "advice", "robot": inp.get("robot")})
        return
    cap = max(wd.ADVICE_MIN, min(wd.MAX_SPEED, int(inp.get("cap", wd.MAX_SPEED))))
    r["adv"] = cap
    r["adv_until"] = st["tick"] + max(10, min(wd.ADVICE_TTL, int(inp.get("ttl", wd.ADVICE_TTL))))
    ev.append({"type": "advice", "robot": r["id"], "cap": cap, "reason": str(inp.get("reason", ""))[:160],
               "by": str(inp.get("by", ""))[:80]})


def _apply_road(st: State, inp: dict, ev: list[Event]) -> None:
    """The road crew finished a repair the road-crew AI ordered: that pothole, bump or drift is gone."""
    fid = inp.get("id")
    p = next((q for q in st["potholes"] if q["id"] == fid), None)
    if inp.get("op") != "repair" or p is None or any(r.get("hole") == fid for r in st["robots"].values()):
        ev.append({"type": "input_ignored", "kind": "road", "id": fid})
        return
    st["potholes"] = [q for q in st["potholes"] if q["id"] != fid]
    st["holes_known"] = [c for c in st["holes_known"] if c != p["cell"]]
    ev.append({"type": "road_changed", "op": "repaired", "id": fid, "cell": list(p["cell"]),
               "kind": p.get("kind", "pothole"), "by": str(inp.get("by", ""))[:80], "crew": str(inp.get("crew", ""))[:40]})


def _expire_restrictions(st: State, ev: list[Event]) -> None:
    keep = []
    for z in st["restricted"]:
        if z["until"] <= st["tick"]:
            ev.append({"type": "zone_lifted", "zone": z["zone"]})
        else:
            keep.append(z)
    st["restricted"] = keep


# ---------------------------------------------------------------- trucks

def _occupancy(st: State) -> dict[Cell, str]:
    occ: dict[Cell, str] = {}
    for rid in sorted(st["robots"]):
        for c in st["robots"][rid]["res"]:
            occ[_t(c)] = rid
    return occ


def _robot_tick(st: State, r: dict, occ: dict[Cell, str], ev: list[Event]) -> None:
    if r["estop"] > 0:
        r["estop"] -= 1
        r["v"] = 0
        r["status"] = "estop"
        if r["estop"] == 0:
            r["replan"] = True
        return
    svc = r.get("svc")
    if svc == "standby":  # parked in the workshop, out of service
        r["v"], r["status"], r["blk"] = 0, "standby", 0
        return
    if svc is not None and r["step"] is None:  # faulted: stay put until the control plane moves it
        if svc == "remote":
            r["svc"] = "fault"
            ev.append({"type": "service_arrived", "robot": r["id"], "cell": _cell(r)})
        r["v"], r["status"], r["blk"] = 0, "fault", 0
        return
    if r["step"] is None:
        _start_next(st, r, ev)
    s = r["step"]
    if s is None:
        r["v"] = 0
        r["status"] = "idle"
        r["blk"] = 0
        if r["path"]:  # left over from an abandoned move: give back segments ahead
            keep = _overlap_cells(r)
            keep_t = {_t(c) for c in keep}
            for c in r["res"]:
                if _t(c) not in keep_t and occ.get(_t(c)) == r["id"]:
                    del occ[_t(c)]
            r.update(res=keep, path=[], k=0, replan=False)
        if _cell(r) == r["home"]:
            r["idle"] = 0
        else:
            r["idle"] += 1
            if r["idle"] >= wd.IDLE_HOME_TICKS:
                r["queue"].append({"op": "goto", "cell": list(r["home"]), "internal": True})
                r["idle"] = 0
        return
    r["idle"] = 0
    if s["op"] == "goto":
        _drive(st, r, occ, ev)
    else:
        _work(st, r, ev)


def _start_next(st: State, r: dict, ev: list[Event]) -> None:
    while r["queue"]:
        s = r["queue"].pop(0)
        op = s.get("op")
        need = None
        if op in ("pick", "scan"):
            need = W.slots[s["slot"]]["access"]
        elif op == "drop":
            need = W.docks[s["dock"]]
        if need is not None and not _at(r, need):
            r["queue"].insert(0, s)
            s = {"op": "goto", "cell": list(need), "job": s.get("job")}
            op = "goto"
        if op not in ("goto", "pick", "scan", "drop"):
            ev.append({"type": "step_rejected", "robot": r["id"], "op": op})
            continue
        r["step"] = s
        if s.get("job"):
            r["job"] = s["job"]
        if op == "goto":
            r["replan"] = True
            r["held"] = False
        elif op == "pick":
            scan = st["policy"]["c"]["scan"]
            if "*" in scan or s.get("cls") in scan:
                r["phase"], r["timer"] = "scan", randint(st, 25, 35)
            else:
                r["phase"], r["timer"] = "pick", randint(st, 70, 100)
        elif op == "drop":
            r["phase"], r["timer"] = "drop", randint(st, 45, 65)
        else:
            r["phase"], r["timer"] = "scan", randint(st, 25, 35)
        return


def _work(st: State, r: dict, ev: list[Event]) -> None:
    s = r["step"]
    r["v"] = 0
    r["blk"] = 0
    r["status"] = _WORK_STATUS[r["phase"]]
    r["timer"] -= 1
    if r["timer"] > 0:
        return
    op = s["op"]
    if op == "pick" and r["phase"] == "scan":
        actual = st["inventory"][s["slot"]]["sku"]
        ok = actual == s.get("sku")
        ev.append({"type": "scan", "robot": r["id"], "slot": s["slot"],
                   "expected": s.get("sku"), "actual": actual, "ok": ok})
        if not ok:
            ev.append({"type": "scan_mismatch", "robot": r["id"], "slot": s["slot"], "job": s.get("job")})
            _abandon_job(st, r, ev, s.get("job"), "grade_mismatch")
            return
        r["phase"], r["timer"] = "pick", randint(st, 70, 100)
        return
    if op == "pick":
        item = st["inventory"][s["slot"]]
        r["carry"].append({"sku": item["sku"], "cls": item["cls"], "job": s.get("job")})
        ev.append({"type": "pick", "robot": r["id"], "slot": s["slot"], "sku": item["sku"], "job": s.get("job")})
    elif op == "drop":
        jid = s.get("job")
        job = st["jobs"].pop(jid, None) if jid else None
        expected = job["skus"] if job else []
        actual = sorted(i["sku"] for i in r["carry"])
        ok = actual == expected
        ev.append({"type": "dock_scan", "robot": r["id"], "dock": s["dock"], "job": jid,
                   "expected": expected, "actual": actual, "ok": ok})
        ev.append({"type": "job_done", "robot": r["id"], "job": jid, "ok": ok})
        r["carry"] = []
        r["job"] = None
    else:
        actual = st["inventory"][s["slot"]]["sku"]
        ev.append({"type": "scan", "robot": r["id"], "slot": s["slot"],
                   "expected": s.get("sku"), "actual": actual, "ok": actual == s.get("sku")})
    r["step"] = None
    r["phase"] = ""


def _abandon_job(st: State, r: dict, ev: list[Event], jid: str | None, reason: str) -> None:
    """Drop a load ticket's remaining steps. It leaves the fleet as an exception for a person."""
    if jid is None:  # a manual step with no ticket: only the current step is dropped
        r["step"], r["phase"] = None, ""
        return
    r["queue"] = [q for q in r["queue"] if q.get("job") != jid]
    if r["step"] is not None and r["step"].get("job") == jid:
        r["step"] = None
        r["phase"] = ""
    r["carry"] = [i for i in r["carry"] if i.get("job") != jid]
    st["jobs"].pop(jid, None)
    ev.append({"type": "job_exception", "robot": r["id"], "job": jid, "reason": reason})
    r["job"] = None
    r["idle"] = wd.IDLE_HOME_TICKS  # clear the road right away instead of idling on it


# ---------------------------------------------------------------- motion

def _brake(v: int, vt: int) -> int:
    """Distance covered while braking from v down to vt."""
    d = 0
    while v > vt:
        v = max(v - wd.ACCEL, vt)
        d += v
    return d


def _choose_speed(v: int, vcap: int, limits: list[tuple[int, int]]) -> int:
    """Fastest reachable speed from which every (distance, max speed) limit can still be met."""
    cands: list[int] = []
    if v + wd.ACCEL <= vcap:
        cands.append(v + wd.ACCEL)
    elif v < vcap:
        cands.append(vcap)
    if v <= vcap:
        cands.append(v)
    cands.append(max(v - wd.ACCEL, 0))
    for c in cands:
        if all(c <= vmax or dist - c >= _brake(c, vmax) for dist, vmax in limits):
            return c
    return max(v - wd.ACCEL, 0)


def _cap(st: State, c: list[int] | Cell) -> int:
    caps = st["policy"]["c"]["caps"]
    out = wd.MAX_SPEED
    for z in W.cell_zones.get(_t(c), ()):
        if z in caps and caps[z] < out:
            out = caps[z]
    return out


def _sense(st: State, r: dict, pts: list[tuple[int, int]]) -> tuple[int, dict] | None:
    """Nearest rock on the route ahead within lidar range: (gap in mm along the route, rock).
    `pts` is the route as millimetre points starting at the truck."""
    best: tuple[int, dict] | None = None
    reach = wd.ROBOT_HALF + wd.PALLET_HALF
    sensor = wd.FAULT_SENSOR_RANGE if (r.get("fault") or {}).get("type") == "sensor" else wd.SENSOR_RANGE
    for p in st["pallets"]:
        px, py = center(p["cell"])
        base = 0
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            dx, dy = _sign(bx - ax), _sign(by - ay)
            seg = abs(bx - ax) + abs(by - ay)
            along = (px - ax) * dx + (py - ay) * dy
            lateral = abs(py - ay) if dx else abs(px - ax)
            if lateral < reach and -reach < along <= seg + reach and base + along > 0:
                gap = base + along - reach
                if gap <= sensor and (best is None or gap < best[0]):
                    best = (gap, p)
                break
            base += seg
            if base > sensor + reach:
                break
    return best


def _overlap_cells(r: dict) -> list[list[int]]:
    xs = sorted({(r["x"] - wd.ROBOT_HALF) // wd.CELL, (r["x"] + wd.ROBOT_HALF - 1) // wd.CELL})
    ys = sorted({(r["y"] - wd.ROBOT_HALF) // wd.CELL, (r["y"] + wd.ROBOT_HALF - 1) // wd.CELL})
    return [[x, y] for y in ys for x in xs]


def _restricted_cells(st: State) -> set[Cell]:
    out: set[Cell] = set()
    for z in st["restricted"]:
        out |= {_t(c) for c in W.zones.get(z["zone"], [])}
    return out


_CLOSED = "#closed"  # _reserve's marker: the way ahead is a zone closed under respect_closures


def _closed_cells(st: State, r: dict) -> set[Cell]:
    """Closed-zone cells this truck must not enter (respect_closures); zones it is already in excepted."""
    rules = st["policy"]["c"].get("closures")
    if not rules or not st["restricted"]:
        return set()
    out: set[Cell] = set()
    for z in st["restricted"]:
        cells = {_t(c) for c in W.zones.get(z["zone"], [])}
        if z["zone"] in r["zones"]:
            continue
        if "*" in rules or z["zone"] in rules or any(cells <= {_t(c) for c in W.zones.get(n, [])} for n in rules):
            out |= cells
    return out


def _learn_obstacle(st: State, cell: list[int]) -> None:
    """Fleet-wide obstacle map: once any truck senses a rock, every truck routes around it."""
    if cell in st["known"]:
        return
    st["known"].append([cell[0], cell[1]])
    for rid in sorted(st["robots"]):
        r = st["robots"][rid]
        if r["step"] is not None and r["step"]["op"] == "goto" and cell in r["path"][r["k"]:]:
            r["replan"] = True


def _stationary(r: dict) -> bool:
    """Not going anywhere soon: parked, working, queued, e-stopped, or stuck without a route."""
    return r["v"] == 0 and (
        r["step"] is None or r["step"]["op"] != "goto" or r["status"] in ("held", "blocked", "estop", "queued")
    )


def _soft_costs(st: State, r: dict, goal: Cell) -> dict[Cell, int]:
    soft: dict[Cell, int] = {}
    mine = W.pockets.get(goal)
    for c, owner in W.pockets.items():  # don't cut through another face's loading pocket or a dump's tipping pocket
        if owner != mine:
            soft[c] = wd.DOCK_COST
    for c in st["policy"]["c"]["avoid"]:
        soft[_t(c)] = soft.get(_t(c), 0) + wd.SOFT_AVOID_COST
    if st["holes_known"]:
        cost = {_t(p["cell"]): _kind(p)["cost"] for p in st["potholes"]}
        for c in st["holes_known"]:
            if _t(c) != goal:
                soft[_t(c)] = soft.get(_t(c), 0) + cost.get(_t(c), wd.POTHOLE_COST)
    for oid in sorted(st["robots"]):
        o = st["robots"][oid]
        if oid != r["id"] and _stationary(o):
            for c in o["res"]:
                soft[_t(c)] = soft.get(_t(c), 0) + wd.PARKED_COST
    return soft


def _plan(st: State, r: dict, occ: dict[Cell, str], ev: list[Event]) -> bool:
    goal = _t(r["step"]["cell"])
    hard = {_t(c) for c in st["known"]}
    tmp = {_t(c) for c in r["avoid_tmp"]}
    restricted = _restricted_cells(st)
    moving = r["v"] > 0 and r["path"] and r["k"] < len(r["path"])
    if moving:
        # Re-route on the move: keep to the current route for as far as it takes to brake to corner speed,
        # and plan onward from there, so a truck that spots a pothole or gets new work swings onto its new
        # route without stopping, and never meets a bend it can no longer slow down for.
        j = r["k"]
        cx, cy = center(r["path"][j])
        d = abs(cx - r["x"]) + abs(cy - r["y"])
        need = max(0, r["v"] * r["v"] - wd.CORNER_SPEED * wd.CORNER_SPEED) // (2 * wd.ACCEL) + r["v"]
        while d < need and j + 1 < len(r["path"]):
            j += 1
            d += wd.CELL
        start = _t(r["path"][j])
        if start in hard or start in restricted:
            r["replan"] = "rest"
            return False
        heading = (_sign(r["path"][j][0] - r["path"][j - 1][0]), _sign(r["path"][j][1] - r["path"][j - 1][1])) \
            if j > r["k"] else _DIR_VEC.get(r["dir"])
        path = astar(start, goal, (hard | restricted) - {start}, _soft_costs(st, r, goal), heading,
                     no_turnaround=True)
        if path is None:
            r["replan"] = "rest"
            return False
        r["path"] = r["path"][:j] + path
        r["replan"], r["held"] = False, False
        return True
    start = _t(_cell(r))
    yielding = bool(tmp)
    r["avoid_tmp"] = []
    soft = _soft_costs(st, r, goal)
    path = astar(start, goal, (hard | restricted | tmp) - {start}, soft, _DIR_VEC.get(r["dir"]))
    if path is None:
        if yielding:
            r["replan"] = False  # no way around (often: our goal is their segment), so pull aside
            _step_aside(st, r, occ)
            return False
        r["held"] = astar(start, goal, (hard | tmp) - {start}, soft) is not None
        if not r["held"] and astar(start, goal, set(), {}) is not None:
            # Only a known obstacle (a rock) cuts us off. Waiting would just block the road
            # for everyone, so hand the load to a person as an exception.
            _abandon_job(st, r, ev, r["step"].get("job"), "unreachable")
        return False
    k0 = 0
    if len(path) >= 2 and not _at(r, path[0]):  # stopped between two segment centres: carry straight on
        (ax, ay), (bx, by) = center(path[0]), center(path[1])
        if (ay == by == r["y"] and min(ax, bx) <= r["x"] <= max(ax, bx)) or \
                (ax == bx == r["x"] and min(ay, by) <= r["y"] <= max(ay, by)):
            k0 = 1
    r["path"], r["k"], r["replan"], r["held"] = path, k0, False, False
    keep = _overlap_cells(r)
    keep_t = {_t(c) for c in keep}
    for c in r["res"]:
        if _t(c) not in keep_t and occ.get(_t(c)) == r["id"]:
            del occ[_t(c)]
    r["res"] = keep
    for c in keep:
        occ.setdefault(_t(c), r["id"])
    return True


def _step_aside(st: State, r: dict, occ: dict[Cell, str]) -> None:
    """Move to the nearest free segment off the other truck's remaining route, then resume."""
    other = st["robots"].get(r["yield_from"])
    if other is None:
        return
    avoid = {_t(c) for c in other["res"]}
    theirs = {_t(c) for c in other["path"][other["k"]:]}
    hard = W.blocked | {_t(c) for c in st["known"]} | _restricted_cells(st)
    start = _t(_cell(r))
    seen = {start}
    frontier = [start]
    while frontier:
        nxt: list[Cell] = []
        for c in frontier:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (c[0] + dx, c[1] + dy)
                if n in seen or n in hard or n in avoid or not W.passable(n):
                    continue
                seen.add(n)
                owner = occ.get(n)
                if owner is not None and owner != r["id"]:
                    continue
                if n not in theirs:
                    r["queue"].insert(0, r["step"])
                    r["step"] = {"op": "goto", "cell": [n[0], n[1]], "job": r["step"].get("job"), "aside": True}
                    r["replan"] = True
                    return
                nxt.append(n)
        frontier = nxt


def _section_run(path: list[list[int]], idx: int) -> list[Cell]:
    """The one-lane cut the route enters at path[idx]: every cell of it the route crosses, in order."""
    sec = W.sections.get(_t(path[idx]))
    if sec is None:
        return []
    out: list[Cell] = []
    for c in path[idx:]:
        if W.sections.get(_t(c)) != sec:
            break
        out.append(_t(c))
    return out


def _trim_res(r: dict, occ: dict[Cell, str]) -> None:
    """After passing a segment centre, release every segment behind it."""
    path, k = r["path"], r["k"]
    here = _t(path[k - 1])
    keep = {here} | {_t(c) for c in path[k:k + wd.LOOKAHEAD_CELLS + 1]}
    if k < len(path):
        keep |= set(_section_run(path, k))  # a cut stays ours until we are out of it
    new: list[list[int]] = []
    for c in r["res"]:
        tc = _t(c)
        if tc in keep:
            new.append(c)
        elif occ.get(tc) == r["id"]:
            del occ[tc]
    if all(_t(c) != here for c in new):
        new.insert(0, [here[0], here[1]])
        occ[here] = r["id"]
    r["res"] = new


def _reserve(st: State, r: dict, occ: dict[Cell, str], closed: set[Cell]) -> str:
    """Reserve segments ahead along the route. Returns the id of a blocking truck, _CLOSED, or ""."""
    path, k = r["path"], r["k"]
    mine = {_t(c) for c in r["res"]}
    known = {_t(c) for c in st["known"]}
    for idx in range(k, min(len(path), k + wd.LOOKAHEAD_CELLS)):
        c = _t(path[idx])
        if c in closed:
            return _CLOSED
        if c in mine:
            continue
        run = _section_run(path, idx) or [c]   # a one-lane cut is taken whole, or not at all
        for cc in run:
            owner = occ.get(cc)
            if owner is not None and owner != r["id"]:
                return owner
        if any(cc in known for cc in run):
            return ""
        for cc in run:
            if cc not in mine:
                occ[cc] = r["id"]
                r["res"].append([cc[0], cc[1]])
                mine.add(cc)
    return ""


def _arrive(r: dict, occ: dict[Cell, str]) -> None:
    here = _cell(r)
    for c in r["res"]:
        if _t(c) != _t(here) and occ.get(_t(c)) == r["id"]:
            del occ[_t(c)]
    r["res"] = [here]
    occ[_t(here)] = r["id"]
    r.update(v=0, path=[], k=0, step=None, blk=0, status="idle",
             replan=False, wait=0, wait_on="")


def _kind(p: dict) -> dict[str, int]:
    return wd.ROAD_KINDS[p.get("kind", "pothole")]


def _in_hole(st: State, r: dict) -> dict | None:
    for p in st["potholes"]:
        px, py = center(p["cell"])
        if abs(r["x"] - px) < wd.POTHOLE_HALF and abs(r["y"] - py) < wd.POTHOLE_HALF:
            return p
    return None


def _drive(st: State, r: dict, occ: dict[Cell, str], ev: list[Event]) -> None:
    if r["replan"] is True and r["v"] > 0 and r["path"]:
        _plan(st, r, occ, ev)
    if not r["path"] or (r["replan"] and r["v"] == 0):
        planned = _plan(st, r, occ, ev)
        if r["step"] is None:  # the move was handed off as an exception
            r["v"] = 0
            return
        if not planned and not r["path"]:
            r["v"] = 0
            r["status"] = "held" if r["held"] else "blocked"
            if not r["held"]:
                r["blk"] += 1
            return

    path = r["path"]
    while r["k"] < len(path) and _at(r, path[r["k"]]):
        r["k"] += 1
        if r["k"] > 0:
            _trim_res(r, occ)
    if r["k"] >= len(path):
        _arrive(r, occ)
        return
    k = r["k"]
    cx, cy = center(path[k])
    if r["v"] == 0:  # at rest a truck can swing onto any heading before it pulls away
        r["dir"] = _DIR_NAMES[(_sign(cx - r["x"]), _sign(cy - r["y"]))]

    closed = _closed_cells(st, r)
    blocked_by = _reserve(st, r, occ, closed)
    closed_ahead = blocked_by == _CLOSED
    if closed_ahead:
        blocked_by = ""
    mine = {_t(c) for c in r["res"]}

    # Distances along the route to each segment centre ahead, and the route as points.
    horizon = min(len(path), k + 6)
    first = abs(cx - r["x"]) + abs(cy - r["y"])
    dist = [first + (i - k) * wd.CELL for i in range(k, horizon)]
    pts = [(r["x"], r["y"])] + [center(path[i]) for i in range(k, horizon)]

    # Hard stops (speed 0): the end of the route, the end of our reservations, a closed zone,
    # and (when a re-plan is pending) the first centre the truck can comfortably stop at.
    fr = k - 1
    for idx in range(k, horizon):
        if _t(path[idx]) in closed:  # a zone closed after we reserved into it: stop short
            closed_ahead = True
            break
        if _t(path[idx]) not in mine:
            break
        fr = idx
    hard = dist[fr - k] if fr >= k else 0
    if horizon == len(path):
        hard = min(hard, dist[-1])
    if r["replan"]:
        need = _brake(r["v"], 0)
        for idx in range(k, fr + 1):
            if dist[idx - k] >= need:
                hard = min(hard, dist[idx - k])
                break
    limits: list[tuple[int, int]] = [(hard, 0)]

    # Bends: slow to corner speed; a turnaround needs a stop.
    for idx in range(k, horizon - 1):
        a = pts[idx - k]
        b, c = pts[idx - k + 1], pts[idx - k + 2]
        din = (_sign(b[0] - a[0]), _sign(b[1] - a[1]))
        dout = (_sign(c[0] - b[0]), _sign(c[1] - b[1]))
        if din != dout:
            limits.append((dist[idx - k], 0 if din == (-dout[0], -dout[1]) else wd.CORNER_SPEED))

    vcap = min(_cap(st, _cell(r)), wd.MAX_SPEED_LOADED if r["carry"] else wd.MAX_SPEED)
    if r.get("adv") and st["tick"] < r.get("adv_until", 0):   # the AI driver's current speed advice
        vcap = min(vcap, r["adv"])
    if r.get("fault"):  # limping under remote control
        vcap = min(vcap, wd.FAULT_SPEED[r["fault"]["type"]])
    for idx in range(k, min(horizon, k + 4)):
        cap = _cap(st, path[idx])
        boundary = dist[idx - k] - wd.HALF
        if cap < vcap and boundary >= 0:
            limits.append((boundary, cap))

    # Mapped road features: ease into a pothole at crawl speed, over a bump or through a sand drift a little
    # faster, then speed up again.
    known = {_t(c) for c in st["holes_known"]}
    if known:
        speed = {_t(p["cell"]): _kind(p)["speed"] for p in st["potholes"]}
        inside = _in_hole(st, r)
        if inside is not None and _t(inside["cell"]) in known:
            vcap = min(vcap, _kind(inside)["speed"])
        for idx in range(k, horizon):
            c = _t(path[idx])
            if c in known and c in speed:
                edge = dist[idx - k] - wd.POTHOLE_HALF
                if edge >= 0:
                    limits.append((edge, speed[c]))

    # Sensed rocks are soft: the truck brakes but is not teleported to a stop.
    sensed = _sense(st, r, pts)
    obstacle_limited = False
    if sensed is not None:
        gap, pallet = sensed
        obs = gap - st["policy"]["c"]["clearance"]
        limits.append((obs, 0))
        obstacle_limited = obs <= hard
        _learn_obstacle(st, pallet["cell"])

    v = _choose_speed(r["v"], vcap, limits)
    if v == 0 and 0 < hard < wd.ACCEL and not obstacle_limited:
        v = hard   # braking in whole ACCEL steps can leave a few mm to the stop point: creep them
    move = min(v, max(hard, 0))
    left = move
    while left > 0 and r["k"] < len(path):  # drive along the route, through bends
        tx, ty = center(path[r["k"]])
        d = abs(tx - r["x"]) + abs(ty - r["y"])
        sx, sy = _sign(tx - r["x"]), _sign(ty - r["y"])
        adv = min(left, d)
        r["x"] += sx * adv
        r["y"] += sy * adv
        if (sx, sy) != (0, 0):
            r["dir"] = _DIR_NAMES[(sx, sy)]
        left -= adv
        if adv == d:
            r["k"] += 1
            _trim_res(r, occ)
    r["v"] = move if move < v else v
    r["odo"] += move

    if move > 0:
        if r["wait"]:
            ev.append({"type": "resume", "robot": r["id"], "after": r["wait"], "from": r["wait_on"]})
        r["blk"], r["wait"], r["wait_on"] = 0, 0, ""
        r["status"] = "moving"
        return
    if closed_ahead:  # waiting for a closed road to reopen is by design, not a stall
        r["status"], r["held"] = "held", True
        return
    if blocked_by:
        other = st["robots"][blocked_by]
        goal = _t(r["step"]["cell"])
        queued = goal in {_t(c) for c in other["res"]} or (
            other["status"] == "queued" and other["step"] is not None and _t(other["step"].get("cell", [-1, -1])) == goal)
        if queued and other["wait_on"] == r["id"] and other["wait"] > 0 and not r["avoid_tmp"]:
            # The truck at the spot is loaded and we stand in its way out: pull aside for it.
            r["avoid_tmp"] = [list(c) for c in other["res"]] + [list(c) for c in other["path"][other["k"]:other["k"] + 3]]
            r["yield_from"] = other["id"]
            r["replan"] = True
            ev.append({"type": "yield", "robot": r["id"], "to": other["id"], "rule": "make_way", "waited": r["wait"],
                       "cell": _cell(r)})
            r["status"] = "queued"
            return
        if queued:  # the truck ahead is being loaded (or waits for the same spot): queue behind it
            r["status"] = "queued"
            if r["wait_on"] == blocked_by:
                r["wait"] += 1
            else:
                r["wait_on"], r["wait"] = blocked_by, 1
                ev.append({"type": "queue", "robot": r["id"], "behind": blocked_by, "cell": list(goal)})
            return
    # A stall is being stuck, not queueing: behind a truck that is still moving, don't count.
    if not (blocked_by and st["robots"][blocked_by]["v"] > 0):
        r["blk"] += 1
    if blocked_by:
        r["status"] = "waiting"
        other = st["robots"][blocked_by]
        if r["wait_on"] == blocked_by:
            r["wait"] += 1
        else:
            r["wait_on"], r["wait"] = blocked_by, 1
            # Coordination is visible as events (they never feed back into the state):
            # the segment we asked for is held by `blocked_by`, so we hold short of it.
            want = next((path[i] for i in range(k, min(len(path), k + wd.LOOKAHEAD_CELLS))
                         if occ.get(_t(path[i])) == blocked_by), path[k])
            ev.append({"type": "wait", "robot": r["id"], "on": blocked_by, "cell": list(want),
                       "other_moving": other["v"] > 0})
            if other["wait_on"] == r["id"] and other["wait"] > 0:
                ev.append({"type": "standoff", "robot": r["id"], "with": blocked_by, "cell": _cell(r),
                           "loaded": bool(r["carry"]), "other_loaded": bool(other["carry"])})
        # Fallback right of way, if the traffic AI has not ruled: anyone blocked by a parked truck
        # re-routes; in a head-on standoff the higher id yields, then the lower id if that failed;
        # in a longer chain, anyone who has waited long enough. Retries every YIELD_TICKS.
        mutual = other["wait_on"] == r["id"] and other["wait"] > 0
        if _stationary(other):
            patience, rule = wd.YIELD_TICKS, "parked"
        elif mutual and r["id"] > other["id"]:
            patience, rule = wd.AI_TRAFFIC_TICKS, "head_on"
        elif mutual:
            patience, rule = wd.AI_TRAFFIC_TICKS + 4 * wd.YIELD_TICKS, "head_on_retry"
        else:
            patience, rule = 3 * wd.YIELD_TICKS, "queue"
        if r["wait"] >= patience and (r["wait"] - patience) % wd.YIELD_TICKS == 0:
            r["avoid_tmp"] = [list(c) for c in other["res"]]
            r["yield_from"] = other["id"]
            r["replan"] = True
            ev.append({"type": "yield", "robot": r["id"], "to": other["id"], "rule": rule, "waited": r["wait"],
                       "cell": _cell(r)})
    else:
        r["status"] = "blocked" if obstacle_limited else "waiting"


# ---------------------------------------------------------------- sensing and physics checks

def _wx(st: State, lo: int, hi: int) -> int:
    st["wx"], z = _next(st["wx"])
    return lo + z % (hi - lo + 1)


def _weather(st: State, ev: list[Event]) -> None:
    """Halfway between lidar surveys the roads change: a sand drift blows in, a bump builds up or a pothole
    opens somewhere no truck is about to drive over; now and then an old drift or bump is gone again.
    Drawn from the state's own weather stream, so a replay sees exactly the same roads."""
    if st["tick"] % wd.SURVEY_TICKS != wd.SURVEY_TICKS // 2 or "wx" not in st:
        return
    feats = st["potholes"]
    roll = _wx(st, 0, 99)
    if roll < 60 and len(feats) < wd.MAX_ROAD_FEATURES:
        busy: set[Cell] = set()
        for rid in sorted(st["robots"]):
            r = st["robots"][rid]
            cx, cy = _cell(r)
            busy |= {(cx + dx, cy + dy) for dx in range(-2, 3) for dy in range(-2, 3) if abs(dx) + abs(dy) <= 2}
            busy |= {_t(c) for c in r["path"][r["k"]:r["k"] + 5]}
        taken = {_t(p["cell"]) for p in feats}
        pool = [c for c in W.weather_cells if c not in busy and c not in taken]
        if pool:
            c = pool[_wx(st, 0, len(pool) - 1)]
            kr = _wx(st, 0, 99)
            kind = "sand" if kr < 45 else "bump" if kr < 75 else "pothole"
            depth = _wx(st, *{"sand": (300, 700), "bump": (200, 450), "pothole": (400, 800)}[kind])
            st["hole_seq"] += 1
            hid = f"H{st['hole_seq']}"
            feats.append({"id": hid, "cell": [c[0], c[1]], "depth": depth, "kind": kind, "fresh": True, "w": True})
            ev.append({"type": "road_changed", "op": "formed", "id": hid, "cell": [c[0], c[1]], "kind": kind,
                       "depth": depth})
    elif roll >= 80:
        inside = {r.get("hole", "") for r in st["robots"].values()}
        gone = [p for p in feats if p.get("w") and p.get("kind") in ("sand", "bump") and p["id"] not in inside]
        if gone:
            p = gone[_wx(st, 0, len(gone) - 1)]
            st["potholes"] = [q for q in feats if q["id"] != p["id"]]
            st["holes_known"] = [c for c in st["holes_known"] if c != p["cell"]]
            ev.append({"type": "road_changed", "op": "cleared", "id": p["id"], "cell": list(p["cell"]),
                       "kind": p.get("kind", "pothole")})


def _lidar(st: State, ev: list[Event]) -> None:
    """Every SURVEY_TICKS each truck's lidar does a full 360-degree survey: every road feature within range goes
    on the fleet's shared road map. Between surveys it keeps watching the road it is about to drive on."""
    if not st["potholes"]:
        return
    survey = st["tick"] % wd.SURVEY_TICKS == 0
    known = {_t(c) for c in st["holes_known"]}
    for rid in sorted(st["robots"]):
        r = st["robots"][rid]
        if r.get("svc") == "standby":
            continue
        rng = wd.FAULT_SENSOR_RANGE if (r.get("fault") or {}).get("type") == "sensor" else wd.SENSOR_RANGE
        ahead = None if survey else {_t(c) for c in r["path"][r["k"]:r["k"] + 4]}
        for p in st["potholes"]:
            c = _t(p["cell"])
            if c in known or (ahead is not None and c not in ahead):
                continue
            px, py = center(p["cell"])
            d2 = (px - r["x"]) ** 2 + (py - r["y"]) ** 2
            if d2 <= rng * rng:
                known.add(c)
                st["holes_known"].append([c[0], c[1]])
                ev.append({"type": "pothole_detected", "robot": rid, "id": p["id"], "cell": list(p["cell"]),
                           "depth": p["depth"], "dist": int(d2 ** 0.5), "kind": p.get("kind", "pothole"),
                           "survey": survey})
                for oid in sorted(st["robots"]):
                    o = st["robots"][oid]
                    if o["step"] is not None and o["step"]["op"] == "goto" and p["cell"] in o["path"][o["k"]:]:
                        o["replan"] = True


def _holes(st: State, ev: list[Event]) -> None:
    """Entering and climbing out of potholes; hitting one fast is a strike."""
    for rid in sorted(st["robots"]):
        r = st["robots"][rid]
        p = _in_hole(st, r)
        now = p["id"] if p else ""
        was = r.get("hole", "")
        if now == was:
            continue
        if was:
            ev.append({"type": "pothole_exit", "robot": rid, "id": was})
        if p:
            kind = p.get("kind", "pothole")
            ev.append({"type": "pothole_enter", "robot": rid, "id": p["id"], "cell": list(p["cell"]), "v": r["v"],
                       "depth": p["depth"], "kind": kind})
            if r["v"] > _kind(p)["strike"]:
                ev.append({"type": "pothole_strike" if kind == "pothole" else "bump_jump", "robot": rid,
                           "id": p["id"], "v": r["v"], "kind": kind})
        r["hole"] = now


def _contacts(st: State, ev: list[Event]) -> None:
    rh, ph = wd.ROBOT_HALF, wd.PALLET_HALF
    robots = st["robots"]
    for rid in sorted(robots):
        r = robots[rid]
        now: list[str] = []
        hit_cells: list[list[int]] = []
        for p in st["pallets"]:
            px, py = center(p["cell"])
            if abs(px - r["x"]) < rh + ph and abs(py - r["y"]) < rh + ph:
                now.append(p["id"])
                hit_cells.append(p["cell"])
        for oid in sorted(robots):
            o = robots[oid]
            if oid != rid and abs(o["x"] - r["x"]) < 2 * rh and abs(o["y"] - r["y"]) < 2 * rh:
                now.append(oid)
        new = [x for x in now if x not in r["contact"]]
        for x in new:
            ev.append({"type": "contact", "robot": rid, "with": x, "v": r["v"],
                       "x": r["x"], "y": r["y"], "cell": _cell(r)})
        if new:
            r["estop"] = wd.ESTOP_TICKS
            r["v"] = 0
            for c in hit_cells:
                _learn_obstacle(st, c)
        r["contact"] = now


def _zones(st: State, ev: list[Event]) -> None:
    restricted = {z["zone"] for z in st["restricted"]}
    for rid in sorted(st["robots"]):
        r = st["robots"][rid]
        zones = list(W.cell_zones.get(_t(_cell(r)), ()))
        for z in zones:
            if z not in r["zones"] and z in restricted:
                ev.append({"type": "zone_enter", "robot": rid, "zone": z, "restricted": True})
        r["zones"] = zones
