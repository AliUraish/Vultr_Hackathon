"""Mutable sim state: creation and hashing.

The state is a JSON-pure dict (ints, strings, lists, dicts; cells are [x, y]
lists, never tuples). That is what makes "snapshot -> store -> reload -> step"
produce the same bytes as stepping the original.
"""
from __future__ import annotations

from typing import Any

from .hashing import sha256
from .policy import make_policy
from .rng import seed_state
from .world import GARAGE, INITIAL_POTHOLES, INITIAL_ROAD, MAP_HASH, ROBOT_SPECS, SPARE_SPECS, W, center

STATE_FORMAT = 2
# Bump whenever step() behaves differently: capsules recorded by another engine can't replay exactly.
ENGINE_VERSION = 5

State = dict[str, Any]


def _robot(spec: dict, home: tuple[int, int]) -> dict:
    x, y = center(home)
    return {
        "id": spec["id"], "caps": list(spec["caps"]), "home": [home[0], home[1]],
        "x": x, "y": y, "v": 0, "dir": "N", "odo": 0,
        "status": "idle", "step": None, "queue": [], "phase": "", "timer": 0, "job": None,
        "path": [], "k": 0, "res": [[home[0], home[1]]], "replan": False, "held": False,
        "avoid_tmp": [], "yield_from": "",
        "blk": 0, "wait_on": "", "wait": 0, "idle": 0, "estop": 0,
        "contact": [], "zones": list(W.cell_zones.get(home, ())),
        "carry": [], "hole": "",
    }


def _spare(spec: dict, bay: tuple[int, int]) -> dict:
    """A standby truck parked in the workshop: out of service until the control plane deploys it."""
    return {**_robot(spec, bay), "status": "standby", "svc": "standby"}


def initial_state(seed: int, policy: dict | None = None) -> State:
    return {
        "fmt": STATE_FORMAT,
        "engine": ENGINE_VERSION,
        "map": MAP_HASH,
        "tick": 0,
        "seed": seed,
        "rng": seed_state(seed),
        "policy": policy or make_policy([], 1),
        "robots": {**{s["id"]: _robot(s, W.homes[i]) for i, s in enumerate(ROBOT_SPECS)},
                   **{s["id"]: _spare(s, GARAGE[i]) for i, s in enumerate(SPARE_SPECS)}},
        "pallets": [],        # fallen rocks on the road
        "pallet_seq": 0,
        "known": [],          # rock cells the fleet has sensed (shared map)
        "potholes": [{"id": f"H{i + 1}", "cell": [x, y], "depth": d, "kind": k}
                     for i, (k, x, y, d) in enumerate([("pothole", *h) for h in INITIAL_POTHOLES] + list(INITIAL_ROAD))],
        "hole_seq": len(INITIAL_POTHOLES) + len(INITIAL_ROAD),
        "holes_known": [],    # road feature cells (potholes, bumps, sand) some truck's lidar has seen (shared map)
        "wx": seed_state(seed * 7919 + 17),   # the road weather's own random stream
        "restricted": [],
        "inventory": {sid: {"sku": s["sku"], "cls": s["cls"]} for sid, s in sorted(W.slots.items())},
        "jobs": {},
    }


def state_hash(state: State) -> str:
    return sha256(state)
