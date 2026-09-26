"""The deterministic fleet step.

`step(state, inputs)` applies one tick's recorded inputs and advances the world
by one fixed timestep. It is pure integer math over a JSON-pure dict and never
reads a clock, the network or unseeded randomness, so the same state plus the
same inputs always yields the same next state.

Motion model: robots follow A* paths cell-centre to cell-centre, stopping to
turn. They accelerate/brake at ACCEL, reserve cells ahead so robots never
share a cell, and only learn about dropped pallets through a forward sensor.
A robot that cannot brake within its sensed gap hits the pallet.
"""
from __future__ import annotations

from typing import Any

from . import world as wd
from .nav import astar
from .policy import PolicyError, make_policy
from .rng import randint
from .world import W, cell_at, center

State = dict[str, Any]
Event = dict[str, Any]
Cell = tuple[int, int]

_DIR_NAMES: dict[tuple[int, int], str] = {(1, 0): "E", (-1, 0): "W", (0, 1): "S", (0, -1): "N"}
_WORK_STATUS = {"pick": "picking", "scan": "scanning", "drop": "dropping"}


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
    _contacts(state, events)
    _zones(state, events)
    state["tick"] += 1
    return events


def set_policy(state: State, policy: dict, events: list[Event]) -> None:
    """Swap the fleet policy. Robots re-route only if the avoid set changed."""
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
            and (s is None or s.get("internal", False))
            and all(q.get("internal", False) for q in r["queue"])
        )
        robots.append({
            "id": rid, "x": r["x"], "y": r["y"], "v": r["v"], "dir": r["dir"],
            "st": r["status"], "job": r["job"], "blk": r["blk"], "free": free,
            "carry": [i["sku"] for i in r["carry"]],
            "path": r["path"][r["k"]:r["k"] + 16] if r["path"] else [],
        })
    return {
        "t": state["tick"],
        "robots": robots,
        "pallets": [dict(p) for p in state["pallets"]],
        "restricted": [dict(z) for z in state["restricted"]],
        "jobs": [{"id": j["id"], "robot": j["robot"], "deadline": j["deadline"]}
                 for _, j in sorted(state["jobs"].items())],
        "ev": events,
        "in": inputs,
    }


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
        r["step"] = None  # new work preempts the trip home; motion state is kept
    r["queue"] = [q for q in r["queue"] if not q.get("internal")] + steps
    ev.append({"type": "cmd", "robot": r["id"], "job": jid, "ops": [s["op"] for s in steps]})


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
    if typ == "spawn_pallet":
        st["pallet_seq"] += 1
        pid = f"P{st['pallet_seq']}"
        cell = [inp["cell"][0], inp["cell"][1]]
        st["pallets"].append({"id": pid, "cell": cell})
        ev.append({"type": "pallet_dropped", "id": pid, "cell": list(cell)})
        px, py = center(cell)
        reach = wd.ROBOT_HALF + wd.PALLET_HALF
        for rid in sorted(st["robots"]):
            r = st["robots"][rid]
            if abs(r["x"] - px) < reach and abs(r["y"] - py) < reach:
                r["contact"].append(pid)  # already touching: no "contact" (collision) event
                r["estop"], r["v"] = wd.ESTOP_TICKS, 0
                ev.append({"type": "struck", "robot": rid, "by": pid})
                _learn_obstacle(st, cell)
    elif typ == "clear_pallets":
        ids = set(inp.get("ids") or [p["id"] for p in st["pallets"]])
        gone = [p["cell"] for p in st["pallets"] if p["id"] in ids]
        st["pallets"] = [p for p in st["pallets"] if p["id"] not in ids]
        st["known"] = [c for c in st["known"] if c not in gone]
        ev.append({"type": "pallets_cleared", "ids": sorted(ids)})
    elif typ == "mislabel":
        slot = inp["slot"]
        st["inventory"][slot]["sku"] = inp["sku"]
        ev.append({"type": "bin_mislabeled", "slot": slot, "sku": inp["sku"]})
    elif typ == "relabel":
        slots = inp.get("slots") or sorted(st["inventory"])
        for s in slots:
            st["inventory"][s]["sku"] = W.slots[s]["sku"]
        ev.append({"type": "bins_relabeled", "slots": slots})
    elif typ == "restrict_zone":
        zone = inp["zone"]
        until = st["tick"] + int(inp.get("ticks", 200))
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
    else:
        ev.append({"type": "input_ignored", "kind": "chaos", "chaos": typ})


def _expire_restrictions(st: State, ev: list[Event]) -> None:
    keep = []
    for z in st["restricted"]:
        if z["until"] <= st["tick"]:
            ev.append({"type": "zone_lifted", "zone": z["zone"]})
        else:
            keep.append(z)
    st["restricted"] = keep


# ---------------------------------------------------------------- robots

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
    if r["step"] is None:
        _start_next(st, r, ev)
    s = r["step"]
    if s is None:
        r["v"] = 0
        r["status"] = "idle"
        r["blk"] = 0
        if r["path"]:  # left over from an abandoned move: give back cells ahead
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
                r["phase"], r["timer"] = "scan", randint(st, 4, 6)
            else:
                r["phase"], r["timer"] = "pick", randint(st, 8, 14)
        elif op == "drop":
            r["phase"], r["timer"] = "drop", randint(st, 6, 10)
        else:
            r["phase"], r["timer"] = "scan", randint(st, 4, 6)
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
            _abandon_job(st, r, ev, s.get("job"), "scan_mismatch")
            return
        r["phase"], r["timer"] = "pick", randint(st, 8, 14)
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
    """Drop a job's remaining steps. The job leaves the fleet as an exception for a human."""
    if jid is None:  # a manual step with no job: only the current step is dropped
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
    r["idle"] = wd.IDLE_HOME_TICKS  # clear the aisle right away instead of idling in it


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


def _cap(st: State, c: list[int]) -> int:
    caps = st["policy"]["c"]["caps"]
    out = wd.MAX_SPEED
    for z in W.cell_zones.get(_t(c), ()):
        if z in caps and caps[z] < out:
            out = caps[z]
    return out


def _sense(st: State, r: dict, dx: int, dy: int) -> tuple[int, dict] | None:
    """Nearest pallet ahead within sensor range: (gap in mm, pallet)."""
    best: tuple[int, dict] | None = None
    reach = wd.ROBOT_HALF + wd.PALLET_HALF
    for p in st["pallets"]:
        px, py = center(p["cell"])
        along = (px - r["x"]) * dx + (py - r["y"]) * dy
        lateral = abs(py - r["y"]) if dx else abs(px - r["x"])
        if along <= 0 or lateral >= reach:
            continue
        gap = along - reach
        if gap > wd.SENSOR_RANGE:
            continue
        if best is None or gap < best[0]:
            best = (gap, p)
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
    """Closed-zone cells this robot must not enter (respect_closures); zones it is already in excepted."""
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
    """Fleet-wide obstacle map: once any robot senses a pallet, every robot routes around it."""
    if cell in st["known"]:
        return
    st["known"].append([cell[0], cell[1]])
    for rid in sorted(st["robots"]):
        r = st["robots"][rid]
        if r["step"] is not None and r["step"]["op"] == "goto" and cell in r["path"][r["k"]:]:
            r["replan"] = True


def _stationary(r: dict) -> bool:
    """Not going anywhere soon: parked, working, e-stopped, or stuck without a route."""
    return r["v"] == 0 and (
        r["step"] is None or r["step"]["op"] != "goto" or r["status"] in ("held", "blocked", "estop")
    )


def _plan(st: State, r: dict, occ: dict[Cell, str], ev: list[Event]) -> bool:
    start = _t(_cell(r))
    goal = _t(r["step"]["cell"])
    hard = {_t(c) for c in st["known"]}
    tmp = {_t(c) for c in r["avoid_tmp"]}
    yielding = bool(tmp)
    r["avoid_tmp"] = []
    soft: dict[Cell, int] = {}
    for c in W.docks.values():  # don't drive through someone else's dock
        if _t(c) != goal:
            soft[_t(c)] = wd.DOCK_COST
    for c in st["policy"]["c"]["avoid"]:
        soft[_t(c)] = soft.get(_t(c), 0) + wd.SOFT_AVOID_COST
    for oid in sorted(st["robots"]):
        o = st["robots"][oid]
        if oid != r["id"] and _stationary(o):
            for c in o["res"]:
                soft[_t(c)] = soft.get(_t(c), 0) + wd.PARKED_COST
    restricted = _restricted_cells(st)
    path = astar(start, goal, (hard | restricted | tmp) - {start}, soft)
    if path is None:
        if yielding:
            r["replan"] = False  # no way around (often: our goal is their cell), so step aside
            _step_aside(st, r, occ)
            return False
        r["held"] = astar(start, goal, (hard | tmp) - {start}, soft) is not None
        if not r["held"] and astar(start, goal, set(), {}) is not None:
            # Only a known obstacle (a pallet) cuts us off. Waiting would just block the
            # aisle for everyone, so hand the job to a human as an exception.
            _abandon_job(st, r, ev, r["step"].get("job"), "unreachable")
        return False
    r["path"], r["k"], r["replan"], r["held"] = path, 0, False, False
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
    """Move to the nearest free cell off the blocking robot's remaining path, then resume."""
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


def _trim_res(r: dict, occ: dict[Cell, str]) -> None:
    """After passing a waypoint centre, release every cell behind it."""
    path, k = r["path"], r["k"]
    here = _t(path[k - 1])
    keep = {here} | {_t(c) for c in path[k:k + wd.LOOKAHEAD_CELLS + 1]}
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
    """Reserve cells ahead along the path. Returns the id of a blocking robot, _CLOSED, or ""."""
    path, k = r["path"], r["k"]
    mine = {_t(c) for c in r["res"]}
    known = {_t(c) for c in st["known"]}
    for idx in range(k, min(len(path), k + wd.LOOKAHEAD_CELLS)):
        c = _t(path[idx])
        if c in closed:
            return _CLOSED
        if c in mine:
            continue
        owner = occ.get(c)
        if owner is not None and owner != r["id"]:
            return owner
        if c in known:
            return ""
        occ[c] = r["id"]
        r["res"].append([c[0], c[1]])
        mine.add(c)
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


def _drive(st: State, r: dict, occ: dict[Cell, str], ev: list[Event]) -> None:
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
    dx, dy = _sign(cx - r["x"]), _sign(cy - r["y"])
    d = _DIR_NAMES[(dx, dy)]
    turning = d != r["dir"]
    if turning:
        r["v"] = 0  # robots turn in place; the planner always brings them to rest first
        r["dir"] = d

    j = k  # end of the straight segment
    while j + 1 < len(path) and path[j + 1][0] - path[j][0] == dx and path[j + 1][1] - path[j][1] == dy:
        j += 1

    closed = _closed_cells(st, r)
    blocked_by = _reserve(st, r, occ, closed)
    closed_ahead = blocked_by == _CLOSED
    if closed_ahead:
        blocked_by = ""
    mine = {_t(c) for c in r["res"]}
    fr = k - 1
    for idx in range(k, j + 1):
        if _t(path[idx]) in closed:  # an aisle closed after we reserved into it: stop short
            closed_ahead = True
            break
        if _t(path[idx]) not in mine:
            break
        fr = idx

    def along(c: list[int]) -> int:
        px, py = center(c)
        return (px - r["x"]) * dx + (py - r["y"]) * dy

    # Hard stops are known in advance and exact: end of segment, end of reservations,
    # and (when a re-plan is pending) the first centre the robot can comfortably stop at.
    hard = along(path[j])
    hard = min(hard, along(path[fr]) if fr >= k else 0)
    if r["replan"]:
        need = _brake(r["v"], 0)
        for idx in range(k, fr + 1):
            if along(path[idx]) >= need:
                hard = min(hard, along(path[idx]))
                break
    limits: list[tuple[int, int]] = [(hard, 0)]

    vcap = _cap(st, _cell(r))
    for idx in range(k, min(j, k + 3) + 1):
        cap = _cap(st, path[idx])
        boundary = along(path[idx]) - wd.HALF
        if cap < vcap and boundary >= 0:
            limits.append((boundary, cap))

    # Sensed obstacles are soft: the robot brakes but is not teleported to a stop.
    sensed = _sense(st, r, dx, dy)
    obstacle_limited = False
    if sensed is not None:
        gap, pallet = sensed
        obs = gap - st["policy"]["c"]["clearance"]
        limits.append((obs, 0))
        obstacle_limited = obs <= hard
        _learn_obstacle(st, pallet["cell"])

    v = _choose_speed(r["v"], vcap, limits)
    move = min(v, max(hard, 0))
    r["x"] += dx * move
    r["y"] += dy * move
    r["v"] = move if move < v else v
    r["odo"] += move
    # Moving at speed rarely lands exactly on a centre: count every centre passed on this run.
    while r["k"] <= j and along(path[r["k"]]) <= 0:
        r["k"] += 1
        _trim_res(r, occ)

    if move > 0:
        r["blk"], r["wait"], r["wait_on"] = 0, 0, ""
        r["status"] = "moving"
        return
    if closed_ahead:  # waiting for a closed aisle to reopen is by design, not a stall
        r["status"], r["held"] = "held", True
        return
    # A stall is being stuck, not queueing: behind a robot that is still moving, don't count.
    if not turning and not (blocked_by and st["robots"][blocked_by]["v"] > 0):
        r["blk"] += 1
    if blocked_by:
        r["status"] = "waiting"
        if r["wait_on"] == blocked_by:
            r["wait"] += 1
        else:
            r["wait_on"], r["wait"] = blocked_by, 1
        # Re-route around the blocker. Who goes first: anyone blocked by a parked robot;
        # in a head-on standoff the higher id, then the lower id if that failed; in a
        # longer chain, anyone who has waited long enough. Retries every YIELD_TICKS.
        other = st["robots"][blocked_by]
        mutual = other["wait_on"] == r["id"] and other["wait"] > 0
        if _stationary(other) or (mutual and r["id"] > other["id"]):
            patience = wd.YIELD_TICKS
        elif mutual:
            patience = 2 * wd.YIELD_TICKS
        else:
            patience = 3 * wd.YIELD_TICKS
        if r["wait"] >= patience and (r["wait"] - patience) % wd.YIELD_TICKS == 0:
            r["avoid_tmp"] = [list(c) for c in other["res"]]
            r["yield_from"] = other["id"]
            r["replan"] = True
    else:
        r["status"] = "blocked" if obstacle_limited else "waiting"


# ---------------------------------------------------------------- physics checks

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
