"""Static warehouse world: grid, zones, rack slots, docks, robot homes.

Built once from LAYOUT at import time and never mutated. Only the mutable sim
state (see state.py) is snapshotted and hashed; the world is identified by
MAP_HASH so a capsule can refuse to replay against a different map.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# Fixed timestep and physical constants. All sim math is integer millimetres and ticks.
TICK_HZ = 10                 # 100 ms per tick
CELL = 1000                  # mm per grid cell
HALF = CELL // 2
ROBOT_HALF = 300             # robot footprint 600 x 600 mm
PALLET_HALF = 400            # dropped pallet 800 x 800 mm
SENSOR_RANGE = 800           # forward obstacle sensing, robot front to obstacle edge (mm)
ACCEL = 10                   # mm/tick^2 (1 m/s^2), used for both speeding up and braking
MAX_SPEED = 120              # mm/tick (1.2 m/s)
DEFAULT_CLEARANCE = 100      # mm kept to a sensed obstacle
LOOKAHEAD_CELLS = 2          # cells a moving robot reserves ahead of itself

SNAPSHOT_EVERY = 50          # ticks between full-state snapshots (5 s)
STALL_TICKS = 100            # 10 s without progress on a move counts as a stall
YIELD_TICKS = 20             # wait this long on another robot before re-routing
ESTOP_TICKS = 30             # e-stop duration after a collision
IDLE_HOME_TICKS = 20         # idle robots head home after this long
JOB_DEADLINE_TICKS = 1200    # 120 s per job, from creation

SOFT_AVOID_COST = 40         # A* penalty per policy-avoided cell (reroute_avoid)
PARKED_COST = 30             # A* penalty per cell held by a stationary robot
DOCK_COST = 20               # A* penalty for driving through a dock that isn't the goal
TURN_COST = 3                # A* penalty per turn (robots stop to turn)

# '#' wall, A-F rack rows (impassable, picked from the aisle below), '1'-'3' docks DK1-DK3,
# 'H' robot homes, '.' floor.
LAYOUT: tuple[str, ...] = (
    "########################",
    "#......................#",
    "#.AAAAAAAA..BBBBBBBB...#",
    "#.....................1#",
    "#.CCCCCCCC..DDDDDDDD...#",
    "#.....................2#",
    "#.EEEEEEEE..FFFFFFFF...#",
    "#.....................3#",
    "#......................#",
    "#......................#",
    "#.HHHH.................#",
    "#......................#",
    "########################",
)

RACK_CLASSES: dict[str, str] = {
    "A": "boxed", "B": "boxed",
    "C": "loose_small", "D": "loose_small",
    "E": "fragile", "F": "heavy",
}
ITEM_CLASSES: tuple[str, ...] = ("boxed", "fragile", "heavy", "loose_small")

ROBOT_SPECS: tuple[dict, ...] = (
    {"id": "R1", "caps": ["standard"]},
    {"id": "R2", "caps": ["standard"]},
    {"id": "R3", "caps": ["standard"]},
    {"id": "R4", "caps": ["heavy", "standard"]},
)

# Maintenance: garage bays in the south-west corner; the standby spare parks in the first one.
# Kept out of the map hash (like the spare) so capsules recorded before them still verify.
GARAGE: tuple[tuple[int, int], ...] = ((1, 11), (2, 11), (3, 11))
SPARE_SPECS: tuple[dict, ...] = ({"id": "R5", "caps": ["standard"]},)
FAULT_SPEED = {"tire": 15, "sensor": 40}   # mm/tick under remote control: 0.15 m/s on a flat, 0.4 m/s half-blind
FAULT_SENSOR_RANGE = 300                    # mm a degraded lidar still sees
WHEELS = ("FL", "FR", "RL", "RR")

Cell = tuple[int, int]


@dataclass(frozen=True)
class World:
    width: int
    height: int
    blocked: frozenset[Cell]                 # walls and racks
    slots: dict[str, dict]                   # slot id -> {cell, access, cls, sku}
    docks: dict[str, list[int]]              # dock id -> cell
    homes: tuple[tuple[int, int], ...]
    zones: dict[str, list[list[int]]]        # zone name -> sorted cells
    cell_zones: dict[Cell, tuple[str, ...]]  # cell -> sorted zone names
    robot_caps: dict[str, frozenset[str]]
    map_hash: str
    garage: tuple[tuple[int, int], ...] = GARAGE

    def passable(self, c: Cell) -> bool:
        return 0 <= c[0] < self.width and 0 <= c[1] < self.height and c not in self.blocked

    def as_json(self) -> dict:
        """Everything the web app needs to draw the floor."""
        return {
            "width": self.width, "height": self.height, "cell_mm": CELL,
            "layout": list(LAYOUT),
            "slots": self.slots, "docks": self.docks,
            "homes": [list(h) for h in self.homes],
            "zones": self.zones,
            "robots": [{"id": s["id"], "caps": s["caps"]} for s in ROBOT_SPECS],
            "spares": [{"id": s["id"], "caps": s["caps"]} for s in SPARE_SPECS],
            "garage": [list(c) for c in self.garage],
            "map_hash": self.map_hash,
        }


def _build() -> World:
    height, width = len(LAYOUT), len(LAYOUT[0])
    blocked: set[Cell] = set()
    slots: dict[str, dict] = {}
    docks: dict[str, list[int]] = {}
    homes: list[Cell] = []
    rack_counts: dict[str, int] = {}
    for y, row in enumerate(LAYOUT):
        assert len(row) == width, f"row {y} has width {len(row)}"
        for x, ch in enumerate(row):
            if ch == "#":
                blocked.add((x, y))
            elif ch in RACK_CLASSES:
                blocked.add((x, y))
                rack_counts[ch] = rack_counts.get(ch, 0) + 1
                sid = f"{ch}{rack_counts[ch]}"
                slots[sid] = {"cell": [x, y], "access": [x, y + 1],
                              "cls": RACK_CLASSES[ch], "sku": f"SKU-{sid}"}
            elif ch.isdigit():
                docks[f"DK{ch}"] = [x, y]  # not "D1": rack D's slots are D1..D8
            elif ch == "H":
                homes.append((x, y))

    zones: dict[str, set[Cell]] = {"racks": set(), "cross_mid": set(), "dock_area": set(),
                                   "home_area": set(), "south_floor": set()}
    for y in (1, 3, 5, 7):
        for x0, x1, side in ((2, 9, "W"), (12, 19, "E")):
            name = f"aisle_{y}{side}"
            zones[name] = {(x, y) for x in range(x0, x1 + 1)}
            zones["racks"] |= zones[name]
    for y in range(height):
        for x in range(width):
            c = (x, y)
            if c in blocked:
                continue
            if 10 <= x <= 11 and 1 <= y <= 7:
                zones["cross_mid"].add(c)
            if 20 <= x <= 22 and 1 <= y <= 8:
                zones["dock_area"].add(c)
            if 1 <= x <= 6 and 9 <= y <= 11:
                zones["home_area"].add(c)
            if 8 <= y <= 11:
                zones["south_floor"].add(c)

    cell_zones: dict[Cell, list[str]] = {}
    for name, cells in zones.items():
        for c in cells:
            cell_zones.setdefault(c, []).append(name)

    assert len(homes) >= len(ROBOT_SPECS), "not enough robot homes"
    zone_lists = {k: [list(c) for c in sorted(v)] for k, v in sorted(zones.items())}
    digest = hashlib.sha256(json.dumps(
        {"layout": LAYOUT, "zones": zone_lists, "robots": ROBOT_SPECS, "slots": slots, "docks": docks},
        sort_keys=True
    ).encode()).hexdigest()
    return World(
        width=width, height=height, blocked=frozenset(blocked), slots=slots, docks=docks,
        homes=tuple(homes), zones=zone_lists,
        cell_zones={c: tuple(sorted(z)) for c, z in cell_zones.items()},
        robot_caps={s["id"]: frozenset(s["caps"]) for s in ROBOT_SPECS + SPARE_SPECS},
        map_hash=digest,
    )


W = _build()
MAP_HASH = W.map_hash


def center(c: list[int] | Cell) -> tuple[int, int]:
    """Millimetre coordinates of a cell's centre."""
    return c[0] * CELL + HALF, c[1] * CELL + HALF


def cell_at(x: int, y: int) -> list[int]:
    """Cell containing a millimetre point."""
    return [x // CELL, y // CELL]
