"""Static open-pit mine: haul roads, benches, dig faces, dump points, truck park, workshop.

Built once from LAYOUT at import time and never mutated. Only the mutable sim
state (see state.py) is snapshotted and hashed; the world is identified by
MAP_HASH so a capsule can refuse to replay against a different map.

One grid cell is a 20 m road segment. All sim math is integer millimetres and
ticks, in real units: speeds are mm per tick (100 ms), so 1 200 mm/tick is 12 m/s
(43 km/h).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

TICK_HZ = 10                 # 100 ms per tick
CELL = 20_000                # mm per grid cell: one 20 m road segment
HALF = CELL // 2
ROBOT_HALF = 5_000           # haul truck footprint for contact checks, 10 x 10 m
PALLET_HALF = 3_000          # a fallen rock (or spill) blocking the road, 6 x 6 m
SENSOR_RANGE = 60_000        # lidar: obstacles and potholes are seen 60 m out
ACCEL = 20                   # mm/tick^2 (2 m/s^2), used for both speeding up and braking
MAX_SPEED = 1_200            # mm/tick (12 m/s, 43 km/h) empty
MAX_SPEED_LOADED = 900       # mm/tick (9 m/s, 32 km/h) carrying a load
CORNER_SPEED = 500           # mm/tick (5 m/s, 18 km/h) through a 90 degree bend
POTHOLE_SPEED = 200          # mm/tick (2 m/s, 7 km/h) through a pothole
POTHOLE_HALF = 5_000         # the slow zone: 5 m either side of the pothole's centre
POTHOLE_STRIKE = 450         # entering a pothole faster than this (16 km/h) is a strike
# The haul roads change under the fleet: sand drifts in, bumps (washboard, spilled rock ridges) build up and
# potholes open. Each kind has a crossing speed, the speed above which hitting it is a strike (or, for a bump,
# a jump), and an A* cost for routing round it once the lidar has mapped it.
ROAD_KINDS: dict[str, dict[str, int]] = {
    "pothole": {"speed": POTHOLE_SPEED, "strike": POTHOLE_STRIKE, "cost": 70},
    "bump":    {"speed": 390, "strike": 700, "cost": 25},      # 14 km/h over a ridge, a jump above 25 km/h
    "sand":    {"speed": 500, "strike": 10 ** 9, "cost": 30},  # 18 km/h through a drift
}
ADVICE_MIN = 330             # mm/tick (12 km/h): an AI driver's speed advice never slows a truck below this
ADVICE_TTL = 150             # ticks: advice lapses after 15 s unless the AI driver renews it
SURVEY_TICKS = 30            # every 3 s each truck's lidar does a full 360-degree survey of the environment around it
MAX_ROAD_FEATURES = 16       # the weather never leaves more than this many features on the roads
DEFAULT_CLEARANCE = 3_000    # mm kept to a sensed obstacle
LOOKAHEAD_CELLS = 2          # cells a moving truck reserves ahead of itself

SNAPSHOT_EVERY = 50          # ticks between full-state snapshots (5 s)
STALL_TICKS = 150            # 15 s without progress on a move counts as a stall
YIELD_TICKS = 20             # wait this long on a parked truck before re-routing
AI_TRAFFIC_TICKS = 80        # head-on: the traffic AI has 8 s to rule before the fallback rule does
ESTOP_TICKS = 50             # e-stop duration after a collision
IDLE_HOME_TICKS = 50         # idle trucks head to the park after this long
JOB_DEADLINE_TICKS = 3_000   # 5 min per load, from the ticket

SOFT_AVOID_COST = 40         # A* penalty per policy-avoided cell (reroute_avoid)
PARKED_COST = 30             # A* penalty per cell held by a stationary truck
DOCK_COST = 20               # A* penalty for driving through a dump point that isn't the goal
TURN_COST = 3                # A* penalty per bend (trucks slow down for them)
LANE_COST = 12               # A* penalty per cell driven against a two-lane road's direction (keep left)
REVERSE_COST = 60            # A* penalty for turning around where the truck stands
POTHOLE_COST = 70            # A* penalty for a known pothole: swerve round it on a two-lane road, crawl through on a one-lane one

# '#' rock, A-F excavators (they dig the rock behind them), a-f their loading spots,
# 1-4 dump points, P truck park, G workshop bays, '.' haul road. Loading spots and dump
# points sit in 3-wide pockets, so a loaded truck can always pull out past the next one.
LAYOUT: tuple[str, ...] = (
    "##################################",
    "####.3.####.4.#######.2.####.1.###",
    "#................................#",
    "#................................#",
    "#..##############.#############..#",
    "#..####A####E####.########B####..#",
    "#..###.a.##.e.###.#######.b.###..#",
    "#................................#",
    "#..#######.#############.######..#",
    "#..#######.#############.######..#",
    "#................................#",
    "#................................#",
    "#..##############.#############..#",
    "#..##############.#############..#",
    "#................................#",
    "#..###.c.####.##.f.##.####.d.##..#",
    "#..####C#####.###F###.#####D###..#",
    "#..##########.#######.#########..#",
    "#................................#",
    "#................................#",
    "###PPPPPPPPPPPPPPPP#####GGG#######",
    "##################################",
)

# What each dig face produces, and where that material goes.
FACE_CLASSES: dict[str, str] = {"A": "ore", "B": "lowgrade", "C": "waste", "D": "sand", "E": "ore", "F": "waste"}
ITEM_CLASSES: tuple[str, ...] = ("ore", "lowgrade", "waste", "sand")
DESTINATION: dict[str, str] = {"ore": "DK1", "lowgrade": "DK2", "waste": "DK3", "sand": "DK4"}

ROBOT_SPECS: tuple[dict, ...] = tuple({"id": f"T{i:02d}", "caps": ["standard"]} for i in range(1, 15))

# Maintenance: workshop bays on the pit floor; the standby spare parks in the first one.
# Kept out of the map hash (like the spare) so the map hash only covers the roads.
GARAGE: tuple[tuple[int, int], ...] = ((24, 20), (25, 20), (26, 20))
SPARE_SPECS: tuple[dict, ...] = ({"id": "T15", "caps": ["standard"]},)
FAULT_SPEED = {"tire": 150, "sensor": 400}  # mm/tick under remote control: 5 km/h on a flat, 14 km/h half-blind
FAULT_SENSOR_RANGE = 15_000                  # mm a degraded lidar still sees
WHEELS = ("FL", "FR", "RL1", "RL2", "RR1", "RR2")   # dual rear tyres

# Potholes the road already has when a run starts (trucks still have to find them with lidar).
INITIAL_POTHOLES: tuple[tuple[int, int, int], ...] = (   # (x, y, depth in mm)
    (14, 10, 700), (1, 13, 600), (20, 18, 800), (9, 3, 500), (12, 7, 600), (27, 14, 500),
)
INITIAL_ROAD: tuple[tuple[str, int, int, int], ...] = (   # other features at the start: (kind, x, y, height/depth mm)
    ("bump", 5, 11, 350), ("sand", 22, 2, 500), ("sand", 31, 8, 600), ("bump", 26, 19, 300),
)

Cell = tuple[int, int]

# Visual elevation (metres) of the road rows: the crest at 0, then one bench every few rows down to the pit floor.
RIM_M = 8.0


def _level(y: int) -> float:
    """Road elevation at row y: 3 m per row down from the crest, flat on the two-lane roads."""
    for rows, lv in (((2, 3), 0.0), ((10, 11), -24.0), ((18, 19, 20), -48.0)):
        if y in rows:
            return lv
    return max(-48.0, min(0.0, -3.0 * (y - 2.5)))


@dataclass(frozen=True)
class World:
    width: int
    height: int
    blocked: frozenset[Cell]                 # rock and excavators
    slots: dict[str, dict]                   # dig face id -> {cell (excavator), access (loading spot), cls, sku}
    pockets: dict[Cell, str]                 # loading / tipping pocket cell -> the face or dump point it serves
    docks: dict[str, list[int]]              # dump point id -> cell
    homes: tuple[tuple[int, int], ...]
    zones: dict[str, list[list[int]]]        # zone name -> sorted cells
    cell_zones: dict[Cell, tuple[str, ...]]  # cell -> sorted zone names
    lanes: dict[Cell, tuple[int, int]]       # two-lane road cell -> the direction its lane runs (keep left)
    sections: dict[Cell, int]                # one-lane cut cell -> its section: a truck takes a whole cut at once
    elev: tuple[tuple[float, ...], ...]      # visual elevation per cell (m)
    robot_caps: dict[str, frozenset[str]]
    map_hash: str
    garage: tuple[tuple[int, int], ...] = GARAGE
    weather_cells: tuple[Cell, ...] = ()     # road cells where sand, bumps and potholes can form

    def passable(self, c: Cell) -> bool:
        return 0 <= c[0] < self.width and 0 <= c[1] < self.height and c not in self.blocked

    def as_json(self) -> dict:
        """Everything the web app needs to draw the pit."""
        return {
            "width": self.width, "height": self.height, "cell_mm": CELL,
            "layout": list(LAYOUT),
            "slots": self.slots, "docks": self.docks,
            "pockets": [[c[0], c[1], o] for c, o in sorted(self.pockets.items())],
            "homes": [list(h) for h in self.homes],
            "zones": self.zones,
            "lanes": [[c[0], c[1], d[0], d[1]] for c, d in sorted(self.lanes.items())],
            "elev": [list(r) for r in self.elev],
            "rim_m": RIM_M,
            "robots": [{"id": s["id"], "caps": s["caps"]} for s in ROBOT_SPECS],
            "spares": [{"id": s["id"], "caps": s["caps"]} for s in SPARE_SPECS],
            "garage": [list(c) for c in self.garage],
            "destination": DESTINATION,
            "physics": {"max_kmh": MAX_SPEED * TICK_HZ * 3.6 / 1000, "max_loaded_kmh": MAX_SPEED_LOADED * TICK_HZ * 3.6 / 1000,
                        "corner_kmh": CORNER_SPEED * TICK_HZ * 3.6 / 1000, "pothole_kmh": POTHOLE_SPEED * TICK_HZ * 3.6 / 1000,
                        "lidar_m": SENSOR_RANGE / 1000, "accel": ACCEL, "robot_half": ROBOT_HALF,
                        "rock_half": PALLET_HALF, "clearance_m": DEFAULT_CLEARANCE / 1000, "pothole_half": POTHOLE_HALF,
                        "survey_ticks": SURVEY_TICKS,
                        "road_kmh": {k: v["speed"] * TICK_HZ * 3.6 / 1000 for k, v in ROAD_KINDS.items()}},
            "map_hash": self.map_hash,
        }


def _rect(x0: int, x1: int, y0: int, y1: int) -> set[Cell]:
    return {(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)}


def _build() -> World:
    height, width = len(LAYOUT), len(LAYOUT[0])
    blocked: set[Cell] = set()
    slots: dict[str, dict] = {}
    docks: dict[str, list[int]] = {}
    homes: list[Cell] = []
    spots: dict[str, Cell] = {}
    for y, row in enumerate(LAYOUT):
        assert len(row) == width, f"row {y} has width {len(row)}"
        for x, ch in enumerate(row):
            if ch == "#":
                blocked.add((x, y))
            elif ch in FACE_CLASSES:
                blocked.add((x, y))
                slots[ch] = {"cell": [x, y], "cls": FACE_CLASSES[ch], "sku": f"MAT-{ch}"}
            elif ch.islower():
                spots[ch.upper()] = (x, y)
            elif ch.isdigit():
                docks[f"DK{ch}"] = [x, y]
            elif ch == "P":
                homes.append((x, y))
    for sid, s in slots.items():
        s["access"] = list(spots[sid])
    slots = dict(sorted(slots.items()))
    docks = dict(sorted(docks.items()))

    passable = {(x, y) for y in range(height) for x in range(width) if (x, y) not in blocked}
    pockets: dict[Cell, str] = {}
    for sid, (x, y) in spots.items():
        for dx in (-1, 0, 1):
            pockets[(x + dx, y)] = sid
    for did, (x, y) in docks.items():
        for dx in (-1, 0, 1):
            pockets[(x + dx, y)] = did
    bays = {c for c, o in pockets.items() if o in spots}
    cut_list = [_rect(17, 17, 4, 6), _rect(10, 10, 8, 9), _rect(24, 24, 8, 9), _rect(17, 17, 12, 13),
                _rect(13, 13, 15, 17), _rect(21, 21, 15, 17)]
    cuts = set().union(*cut_list)
    sections = {c: i for i, cells in enumerate(cut_list) for c in cells}
    zones: dict[str, set[Cell]] = {
        "crest_road": _rect(1, 32, 2, 3),
        "middle_road": _rect(1, 32, 10, 11),
        "pit_floor": _rect(1, 32, 18, 19),
        "ramp_west": _rect(1, 2, 4, 17) - _rect(1, 2, 10, 11),
        "ramp_east": _rect(31, 32, 4, 17) - _rect(31, 32, 10, 11),
        "bench_upper_w": _rect(3, 16, 7, 7) | {c for c, o in pockets.items() if o in ("A", "E")},
        "bench_upper_e": _rect(18, 30, 7, 7) | {c for c, o in pockets.items() if o == "B"},
        "bench_lower_w": _rect(3, 16, 14, 14) | {c for c, o in pockets.items() if o == "C"},
        "bench_lower_e": _rect(18, 30, 14, 14) | {c for c, o in pockets.items() if o in ("D", "F")},
        "cuts": cuts,
        "park": set(homes),
        "workshop": set(GARAGE),
        "dump_area": {c for c, o in pockets.items() if o in docks} | {(c[0] + dx, y) for c in docks.values()
                                                                     for dx in (-1, 0, 1) for y in (2, 3)},
    }
    zones["haul_roads"] = zones["crest_road"] | zones["middle_road"] | zones["pit_floor"]
    zones["ramps"] = zones["ramp_west"] | zones["ramp_east"]
    zones["bench_upper"] = zones["bench_upper_w"] | zones["bench_upper_e"] | {(17, 7)}
    zones["bench_lower"] = zones["bench_lower_w"] | zones["bench_lower_e"] | {(17, 14)}
    zones["benches"] = zones["bench_upper"] | zones["bench_lower"] | cuts   # roads under a highwall
    for name, cells in zones.items():
        assert cells <= passable, f"zone {name} has a non-road cell"
    covered = set().union(*zones.values())
    assert passable <= covered, f"roads outside every zone: {sorted(passable - covered)[:5]}"

    cell_zones: dict[Cell, list[str]] = {}
    for name, cells in zones.items():
        for c in cells:
            cell_zones.setdefault(c, []).append(name)

    lanes: dict[Cell, tuple[int, int]] = {}
    for top in (2, 10, 18):                      # east-west roads: the north lane runs east (keep left)
        for x in range(3, 31):
            lanes[(x, top)], lanes[(x, top + 1)] = (1, 0), (-1, 0)
    for west in (1, 31):                         # ramps: the west lane runs north
        for y in range(4, 18):
            if y not in (10, 11):
                lanes[(west, y)], lanes[(west + 1, y)] = (0, -1), (0, 1)

    elev: list[list[float]] = []
    for y in range(height):
        row: list[float] = []
        for x in range(width):
            c = (x, y)
            face = LAYOUT[y][x].upper()
            if face in FACE_CLASSES or pockets.get(c) in FACE_CLASSES:   # excavators and their bays sit flat on their bench
                owner = face if face in FACE_CLASSES else pockets[c]
                row.append(_level(7) if owner in "ABE" else _level(14))
            elif pockets.get(c) in docks:                      # tipping pockets on the crest
                row.append(0.0)
            elif x in (0, width - 1) or y in (0, height - 1) or (y == 1 and c not in passable) or (y == 20 and c not in passable):
                row.append(RIM_M)                               # the pit wall rises to the rim
            elif c in passable:
                row.append(_level(y))
            else:                                               # a bench: its top is the road above it
                up = next((yy for yy in range(y - 1, -1, -1) if (x, yy) in passable and (x, yy) not in bays), None)
                row.append(_level(up) if up is not None else RIM_M)
        elev.append(tuple(round(v, 2) for v in row))

    assert len(homes) >= len(ROBOT_SPECS), "not enough park bays"
    weather = sorted(passable - set(pockets) - set(homes) - set(GARAGE) - cuts - zones["dump_area"])
    zone_lists = {k: [list(c) for c in sorted(v)] for k, v in sorted(zones.items())}
    digest = hashlib.sha256(json.dumps(
        {"layout": LAYOUT, "zones": zone_lists, "robots": ROBOT_SPECS, "slots": slots, "docks": docks,
         "lanes": sorted([list(c) + list(d) for c, d in lanes.items()])},
        sort_keys=True
    ).encode()).hexdigest()
    return World(
        width=width, height=height, blocked=frozenset(blocked), slots=slots, pockets=pockets, docks=docks,
        homes=tuple(homes), zones=zone_lists,
        cell_zones={c: tuple(sorted(z)) for c, z in cell_zones.items()},
        lanes=lanes, sections=sections, elev=tuple(elev), weather_cells=tuple(weather),
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


def kmh(v_mm_per_tick: float) -> float:
    return v_mm_per_tick * TICK_HZ * 3.6 / 1000
