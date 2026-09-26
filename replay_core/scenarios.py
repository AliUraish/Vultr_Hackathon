"""Chaos scenarios for the demo.

A scenario request is abstract ("drop a pallet in front of a fast robot"). The
live sim resolves it against the current state into a concrete input ("pallet at
cell 7,3"), and only the concrete input is recorded. Replays feed that recorded
input back, so a forked replay with a different policy still gets the pallet in
the same place at the same tick, no matter where its robots are.

`resolve` returns None while the floor isn't in a suitable state yet; the live
loop keeps retrying until the request expires.

Hints narrow a scenario so "Re-inject live" recreates the original situation
(same aisle and gap, same item class, same aisle closed) instead of a new one.
"""
from __future__ import annotations

from typing import Any

from .world import ITEM_CLASSES, PALLET_HALF, ROBOT_HALF, W, center

SCENARIOS: dict[str, str] = {
    "pallet_drop": "A pallet falls into a rack aisle just ahead of a fast robot (collision).",
    "mislabel_bin": "A bin gets the wrong item; a robot on its way there picks it (wrong item at dock).",
    "worker_in_aisle": "A worker closes an aisle that a robot is already routed through (zone breach).",
    "clear_floor": "Remove pallets, fix bin labels, reopen aisles.",
}

_DIRS = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}


def _chaos(scenario: str, target: str | None, **kw: object) -> dict:
    return {"kind": "chaos", "scenario": scenario, "target": target, **kw}


def _pallet_drop(state: dict, hints: dict[str, Any]) -> list[dict] | None:
    robots = state["robots"]
    zone = hints.get("zone", "racks")
    min_v = max(1, int(hints.get("min_v", 100)))
    lo, hi = 250, 420
    if "gap_mm" in hints:  # re-inject: the same gap the original pallet appeared at
        lo, hi = max(lo, int(hints["gap_mm"]) - 30), min(hi, int(hints["gap_mm"]) + 30)
    fast = []
    for rid in sorted(robots):
        r = robots[rid]
        s = r["step"]
        cell = (r["x"] // 1000, r["y"] // 1000)
        if s and s["op"] == "goto" and r["path"] and r["dir"] and r["v"] >= min_v \
                and zone in W.cell_zones.get(cell, ()):
            fast.append((-r["v"], rid))
    taken = {tuple(p["cell"]) for p in state["pallets"]}
    reach = ROBOT_HALF + PALLET_HALF
    for _, rid in sorted(fast):
        r = robots[rid]
        dx, dy = _DIRS[r["dir"]]
        path, k = r["path"], r["k"]
        j = k
        while j + 1 < len(path) and path[j + 1][0] - path[j][0] == dx and path[j + 1][1] - path[j][1] == dy:
            j += 1
        for idx in range(k, j + 1):
            c = path[idx]
            px, py = center(c)
            gap = (px - r["x"]) * dx + (py - r["y"]) * dy - reach
            if not lo <= gap <= hi or tuple(c) in taken or "racks" not in W.cell_zones.get(tuple(c), ()):
                continue  # pallets fall from racks into rack aisles, never onto the cross-aisles
            if any(abs(o["x"] - px) < reach and abs(o["y"] - py) < reach for o in robots.values()):
                continue
            return [_chaos("pallet_drop", rid, type="spawn_pallet", cell=list(c), gap_mm=gap, v=r["v"])]
    return None


def _mislabel_bin(state: dict, hints: dict[str, Any]) -> list[dict] | None:
    want_cls = hints.get("cls")
    cands = []
    for rid in sorted(state["robots"]):
        r = state["robots"][rid]
        s = r["step"]
        if s and s["op"] == "goto" and r["queue"] and r["queue"][0]["op"] == "pick":
            slot = r["queue"][0]["slot"]
            if want_cls and W.slots[slot]["cls"] != want_cls:
                continue
            if state["inventory"][slot]["sku"] == W.slots[slot]["sku"]:
                cands.append((W.slots[slot]["cls"] != "loose_small", rid, slot))
    if not cands:
        return None
    _, rid, slot = sorted(cands)[0]
    rack, n = slot[0], int(slot[1:])
    other = f"{rack}{n + 1}" if f"{rack}{n + 1}" in W.slots else f"{rack}{n - 1}"
    return [_chaos("mislabel_bin", rid, type="mislabel", slot=slot, sku=W.slots[other]["sku"])]


def _worker_in_aisle(state: dict, hints: dict[str, Any]) -> list[dict] | None:
    closed = {z["zone"] for z in state["restricted"]}
    only = hints.get("zone")
    if only and hints.get("now"):  # re-inject: the worker walks into the same aisle right away
        return None if only in closed else [_chaos("worker_in_aisle", None, type="restrict_zone",
                                                   zone=only, ticks=300)]
    # Target a robot already committed to its route: moving, and the aisle is 2-4 cells
    # ahead, so it will not re-plan before it gets there.
    for rid in sorted(state["robots"]):
        r = state["robots"][rid]
        s = r["step"]
        if not (s and s["op"] == "goto" and r["path"] and r["v"] > 0):
            continue
        here = W.cell_zones.get((r["x"] // 1000, r["y"] // 1000), ())
        goal = W.cell_zones.get(tuple(r["path"][-1]), ())
        ahead = r["path"][r["k"]:]
        for c in ahead[2:5]:
            for z in W.cell_zones.get(tuple(c), ()):
                if z.startswith("aisle_") and z not in here and z not in goal and z not in closed \
                        and (only is None or z == only):
                    return [_chaos("worker_in_aisle", rid, type="restrict_zone", zone=z, ticks=300)]
    return None


def _clear_floor(state: dict, hints: dict[str, Any]) -> list[dict]:
    return [
        _chaos("clear_floor", None, type="clear_pallets"),
        _chaos("clear_floor", None, type="relabel"),
        _chaos("clear_floor", None, type="lift_zones"),
    ]


_RESOLVERS = {
    "pallet_drop": _pallet_drop,
    "mislabel_bin": _mislabel_bin,
    "worker_in_aisle": _worker_in_aisle,
    "clear_floor": _clear_floor,
}


def resolve(state: dict, scenario: str, hints: dict[str, Any] | None = None) -> list[dict] | None:
    if scenario not in _RESOLVERS:
        raise ValueError(f"unknown scenario {scenario!r}; known: {', '.join(SCENARIOS)}")
    return _RESOLVERS[scenario](state, hints or {})


def check_hints(hints: dict[str, Any]) -> dict[str, Any]:
    """Validate hints arriving over the network; raises ValueError."""
    rules = {
        "zone": lambda v: isinstance(v, str) and v in W.zones,
        "gap_mm": lambda v: isinstance(v, int) and 100 <= v <= 800,
        "min_v": lambda v: isinstance(v, int) and 0 <= v <= 200,
        "cls": lambda v: isinstance(v, str) and v in ITEM_CLASSES,
        "now": lambda v: isinstance(v, bool),
        "fallback_zone": lambda v: isinstance(v, str) and v in W.zones,
        "fallback_after": lambda v: isinstance(v, int) and 0 <= v <= 1200,
    }
    for k, v in hints.items():
        if k not in rules or not rules[k](v):
            raise ValueError(f"bad hint {k}={v!r}")
    return hints


def reinject_hints(original: dict) -> dict[str, Any]:
    """Hints that recreate the situation of a recorded chaos input."""
    typ = original.get("type")
    if typ == "spawn_pallet":
        zones = W.cell_zones.get(tuple(original["cell"]), ())
        hints: dict[str, Any] = {"min_v": 1}  # any moving robot: after a speed cap nobody is "fast"
        aisle = next((z for z in zones if z.startswith("aisle_")), None)
        if aisle:  # the same aisle if a robot comes through soon, else any rack aisle
            hints.update(zone=aisle, fallback_zone="racks", fallback_after=300)
        if isinstance(original.get("gap_mm"), int):
            hints["gap_mm"] = original["gap_mm"]
        return hints
    if typ == "mislabel" and original.get("slot") in W.slots:
        return {"cls": W.slots[original["slot"]]["cls"]}
    if typ == "restrict_zone":
        return {"zone": original["zone"], "now": True}
    return {}
