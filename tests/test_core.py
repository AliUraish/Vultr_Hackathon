"""Nav, dispatch and detector rules."""
from replay_core.detector import detect
from replay_core.dispatch import choose_robot, make_job
from replay_core.engine import make_frame
from replay_core.nav import astar
from replay_core.state import initial_state
from replay_core.world import STALL_TICKS, W


def test_astar_path_is_contiguous_and_avoids_racks():
    home, dock = W.homes[0], tuple(W.docks["DK1"])
    path = astar(home, dock, set(), {})
    assert path[0] == list(home) and path[-1] == list(dock)
    for a, b in zip(path, path[1:]):
        assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
        assert (b[0], b[1]) not in W.blocked
    assert astar(home, dock, set(), {}) == path  # deterministic


def test_astar_soft_cost_routes_around_an_aisle():
    start, goal = (1, 3), (10, 3)  # straight through aisle_3W is shortest
    direct = astar(start, goal, set(), {})
    avoid = {(c[0], c[1]): 40 for c in W.zones["aisle_3W"]}
    around = astar(start, goal, set(), avoid)
    assert any(tuple(c) in avoid for c in direct)
    assert not any(tuple(c) in avoid for c in around)


def test_dispatch_keeps_heavy_robot_for_heavy_jobs():
    frame = make_frame(initial_state(1), [], [])
    standard = make_job("J1", ["A1"], "DK1", 0)
    heavy = make_job("J2", ["F3"], "DK2", 0)
    assert choose_robot(frame, heavy) == "R4"
    assert choose_robot(frame, standard) != "R4"
    assert choose_robot(frame, heavy, exclude={"R4"}) is None


def test_detector_rules_fire_once_on_crossings():
    frame = {
        "t": 500,
        "robots": [{"id": "R1", "blk": STALL_TICKS, "st": "waiting", "job": "J1"},
                   {"id": "R2", "blk": STALL_TICKS + 1, "st": "waiting", "job": None}],
        "jobs": [{"id": "J1", "robot": "R1", "deadline": 500}, {"id": "J2", "robot": "R2", "deadline": 900}],
        "ev": [
            {"type": "contact", "robot": "R3", "with": "P1", "v": 80, "cell": [5, 3]},
            {"type": "dock_scan", "robot": "R4", "dock": "DK1", "job": "J7", "expected": ["A"], "actual": ["B"], "ok": False},
            {"type": "dock_scan", "robot": "R4", "dock": "DK1", "job": "J8", "expected": ["A"], "actual": ["A"], "ok": True},
            {"type": "zone_enter", "robot": "R2", "zone": "aisle_3W", "restricted": True},
        ],
    }
    got = sorted((f["type"], f["robot"]) for f in detect(frame))
    assert got == [("collision", "R3"), ("stall", "R1"), ("task_overdue", "R1"),
                   ("wrong_item", "R4"), ("zone_breach", "R2")]
