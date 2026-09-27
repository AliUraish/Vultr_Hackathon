"""Hazard scenarios for the mine.

A scenario request is abstract ("a rock falls in front of a fast truck"). The live
sim resolves it against the current state into a concrete input ("rock at segment
7,7"), and only the concrete input is recorded. Replays feed that recorded input
back, so a forked replay with a different policy still gets the rock in the same
place at the same tick, no matter where its trucks are.

`resolve` returns None while the pit isn't in a suitable state yet; the live loop
keeps retrying until the request expires.

Hints narrow a scenario so "Re-inject live" recreates the original situation
(same bench and gap, same material, same road closed) instead of a new one.
"""
from __future__ import annotations

from typing import Any

from .world import ACCEL, CELL, ITEM_CLASSES, PALLET_HALF, ROBOT_HALF, W, center

SCENARIOS: dict[str, str] = {
    "rockfall": "A rock falls from the highwall onto a bench road just ahead of a fast truck (collision).",
    "grade_mixup": "A dig face's grade tag is wrong: the truck on its way there loads the wrong material (wrong material at the dump).",
    "blast_closure": "The blast crew closes a bench road that a truck is already routed through (exclusion-zone breach).",
    "road_damage": "A pothole opens up on a road ahead of a working truck (it has to see it and slow down or go around).",
    "tire_fault": "A working truck blows a tyre: it can only crawl (service case: technician repair).",
    "sensor_fault": "A working truck's lidar degrades: it can still drive slowly (service case: workshop + spare).",
    "clear_roads": "Clear fallen rocks, fix grade tags, reopen closed roads, grade fresh potholes.",
}

_DIRS = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}
BENCH_SEGMENTS = tuple(sorted(z for z in W.zones if z.startswith("bench_") and z.endswith(("_w", "_e"))))
_SPOTS = {tuple(s["access"]) for s in W.slots.values()}
_DOCKS = {tuple(c) for c in W.docks.values()}


def _chaos(scenario: str, target: str | None, **kw: object) -> dict:
    return {"kind": "chaos", "scenario": scenario, "target": target, **kw}


def _brake(v: int) -> int:
    """Braking distance from v (mm/tick) to a stop."""
    d = 0
    while v > 0:
        v = max(v - ACCEL, 0)
        d += v
    return d


def _cell_of(r: dict) -> tuple[int, int]:
    return (r["x"] // CELL, r["y"] // CELL)


def _rockfall(state: dict, hints: dict[str, Any]) -> list[dict] | None:
    robots = state["robots"]
    zone = hints.get("zone", "benches")
    min_v = max(1, int(hints.get("min_v", 800)))
    fast = []
    for rid in sorted(robots):
        r = robots[rid]
        s = r["step"]
        if s and s["op"] == "goto" and r["path"] and r["dir"] and r["v"] >= min_v \
                and zone in W.cell_zones.get(_cell_of(r), ()):
            fast.append((-r["v"], rid))
    taken = {tuple(p["cell"]) for p in state["pallets"]}
    reach = ROBOT_HALF + PALLET_HALF
    for _, rid in sorted(fast):
        r = robots[rid]
        if "gap_mm" in hints:  # re-inject / stress variant: a chosen gap between truck and rock
            lo, hi = max(3_000, int(hints["gap_mm"]) - 1_500), min(40_000, int(hints["gap_mm"]) + 1_500)
        else:                  # inside this truck's braking distance: it cannot stop in time
            stop = _brake(r["v"])
            lo, hi = stop * 45 // 100, stop * 80 // 100
        dx, dy = _DIRS[r["dir"]]
        path, k = r["path"], r["k"]
        if k >= len(path):  # arriving this tick: nothing ahead
            continue
        j = k
        while j + 1 < len(path) and path[j + 1][0] - path[j][0] == dx and path[j + 1][1] - path[j][1] == dy:
            j += 1
        for idx in range(k, j):   # never the last segment before a bend: the truck brakes for that anyway
            c = path[idx]
            px, py = center(c)
            gap = (px - r["x"]) * dx + (py - r["y"]) * dy - reach
            if not lo <= gap <= hi or tuple(c) in taken or tuple(c) in _SPOTS or c == path[-1] \
                    or "benches" not in W.cell_zones.get(tuple(c), ()):
                continue  # rocks come off a highwall onto the bench roads and cuts, never the open haul roads
            if any(abs(o["x"] - px) < reach and abs(o["y"] - py) < reach for o in robots.values()):
                continue
            return [_chaos("rockfall", rid, type="spawn_rock", cell=list(c), gap_mm=gap, v=r["v"])]
    return None


def _grade_mixup(state: dict, hints: dict[str, Any]) -> list[dict] | None:
    want_cls = hints.get("cls")
    cands = []
    for rid in sorted(state["robots"]):
        r = state["robots"][rid]
        s = r["step"]
        if s and s["op"] == "goto" and r["queue"] and r["queue"][0]["op"] in ("pick", "scan"):
            slot = r["queue"][0]["slot"]
            if want_cls and W.slots[slot]["cls"] != want_cls:
                continue
            if state["inventory"][slot]["sku"] == W.slots[slot]["sku"]:
                cands.append((W.slots[slot]["cls"] != "ore", rid, slot))   # ore faces first: ore -> crusher
    if not cands:
        return None
    _, rid, slot = sorted(cands)[0]
    cls = W.slots[slot]["cls"]
    other = next((s for s, v in sorted(W.slots.items()) if v["cls"] == ("waste" if cls != "waste" else "lowgrade")), slot)
    return [_chaos("grade_mixup", rid, type="mislabel", slot=slot, sku=W.slots[other]["sku"])]


def _blast_closure(state: dict, hints: dict[str, Any]) -> list[dict] | None:
    closed = {z["zone"] for z in state["restricted"]}
    only = hints.get("zone")
    if only and hints.get("now"):  # re-inject: the blast crew closes the same road right away
        return None if only in closed else [_chaos("blast_closure", None, type="restrict_zone", zone=only, ticks=450)]
    # Target a truck already committed to its route: moving, and the bench road is 2-4 segments
    # ahead, so it will not re-plan before it gets there (its dig face may be on that bench).
    for rid in sorted(state["robots"]):
        r = state["robots"][rid]
        s = r["step"]
        if not (s and s["op"] == "goto" and r["path"] and r["v"] > 0):
            continue
        here = W.cell_zones.get(_cell_of(r), ())
        ahead = r["path"][r["k"]:]
        for c in ahead[2:5]:
            for z in W.cell_zones.get(tuple(c), ()):
                if z in BENCH_SEGMENTS and z not in here and z not in closed \
                        and (only is None or z == only):
                    return [_chaos("blast_closure", rid, type="restrict_zone", zone=z, ticks=450)]
    return None


def _road_damage(state: dict, hints: dict[str, Any]) -> list[dict] | None:
    """A fresh pothole a few segments ahead of a moving truck, on a road nobody is standing on."""
    holes = {tuple(p["cell"]) for p in state["potholes"]}
    occupied = {tuple(c) for r in state["robots"].values() for c in r["res"]}
    for rid in sorted(state["robots"]):
        r = state["robots"][rid]
        s = r["step"]
        if not (s and s["op"] == "goto" and r["path"] and r["v"] > 0):
            continue
        for c in r["path"][r["k"] + 3:r["k"] + 7]:
            t = tuple(c)
            if t in holes or t in occupied or t in _SPOTS or t in _DOCKS:
                continue
            return [_chaos("road_damage", rid, type="spawn_pothole", cell=list(c),
                           depth=600 + (state["tick"] % 5) * 100)]
    return None


def _robot_fault(kind: str):
    def resolve_fault(state: dict, hints: dict[str, Any]) -> list[dict] | None:
        """Pick a truck that is out working (moving, on a load, not already in service)."""
        want = hints.get("robot")
        for rid in sorted(state["robots"]):
            r = state["robots"][rid]
            if (want and rid != want) or r.get("fault") or r.get("svc") or not r["job"]:
                continue
            if r["v"] > 0 and r["step"] and r["step"]["op"] == "goto":
                wheels = ("FL", "FR", "RL1", "RL2", "RR1", "RR2")
                wheel = wheels[(state["tick"] + len(rid)) % len(wheels)]
                return [_chaos(f"{kind}_fault", rid, type="robot_fault", robot=rid, fault=kind,
                               **({"wheel": wheel} if kind == "tire" else {}))]
        return None
    return resolve_fault


def _clear_roads(state: dict, hints: dict[str, Any]) -> list[dict]:
    return [
        _chaos("clear_roads", None, type="clear_rocks"),
        _chaos("clear_roads", None, type="relabel"),
        _chaos("clear_roads", None, type="lift_zones"),
        _chaos("clear_roads", None, type="fill_potholes"),
    ]


_RESOLVERS = {
    "rockfall": _rockfall,
    "grade_mixup": _grade_mixup,
    "blast_closure": _blast_closure,
    "road_damage": _road_damage,
    "tire_fault": _robot_fault("tire"),
    "sensor_fault": _robot_fault("sensor"),
    "clear_roads": _clear_roads,
}


def resolve(state: dict, scenario: str, hints: dict[str, Any] | None = None) -> list[dict] | None:
    if scenario not in _RESOLVERS:
        raise ValueError(f"unknown scenario {scenario!r}; known: {', '.join(SCENARIOS)}")
    return _RESOLVERS[scenario](state, hints or {})


def check_hints(hints: dict[str, Any]) -> dict[str, Any]:
    """Validate hints arriving over the network; raises ValueError."""
    rules = {
        "zone": lambda v: isinstance(v, str) and v in W.zones,
        "gap_mm": lambda v: isinstance(v, int) and 3_000 <= v <= 40_000,
        "min_v": lambda v: isinstance(v, int) and 0 <= v <= 1_200,
        "cls": lambda v: isinstance(v, str) and v in ITEM_CLASSES,
        "now": lambda v: isinstance(v, bool),
        "fallback_zone": lambda v: isinstance(v, str) and v in W.zones,
        "fallback_after": lambda v: isinstance(v, int) and 0 <= v <= 1200,
        "robot": lambda v: isinstance(v, str) and v in W.robot_caps,
    }
    for k, v in hints.items():
        if k not in rules or not rules[k](v):
            raise ValueError(f"bad hint {k}={v!r}")
    return hints


def reinject_hints(original: dict) -> dict[str, Any]:
    """Hints that recreate the situation of a recorded hazard input."""
    typ = original.get("type")
    if typ == "spawn_rock":
        zones = W.cell_zones.get(tuple(original["cell"]), ())
        hints: dict[str, Any] = {"min_v": 1}  # any moving truck: after a speed cap nobody is "fast"
        seg = next((z for z in zones if z in BENCH_SEGMENTS or z == "cuts"), None)
        if seg:  # the same bench road if a truck comes through soon, else any bench road
            hints.update(zone=seg, fallback_zone="benches", fallback_after=450)
        if isinstance(original.get("gap_mm"), int):
            hints["gap_mm"] = original["gap_mm"]
        return hints
    if typ == "mislabel" and original.get("slot") in W.slots:
        return {"cls": W.slots[original["slot"]]["cls"]}
    if typ == "restrict_zone":
        return {"zone": original["zone"], "now": True}
    return {}
