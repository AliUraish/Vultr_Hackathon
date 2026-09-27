"""Nav, dispatch and detector rules."""
from replay_core.detector import detect
from replay_core.dispatch import choose_robot, make_job
from replay_core.engine import make_frame
from replay_core.nav import astar
from replay_core.state import initial_state
from replay_core.world import DESTINATION, POTHOLE_COST, STALL_TICKS, W


def test_astar_path_is_contiguous_and_stays_on_the_roads():
    park, crusher = W.homes[0], tuple(W.docks["DK1"])
    path = astar(park, crusher, set(), {})
    assert path[0] == list(park) and path[-1] == list(crusher)
    for a, b in zip(path, path[1:]):
        assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
        assert (b[0], b[1]) not in W.blocked
    assert astar(park, crusher, set(), {}) == path  # deterministic


def test_trucks_keep_left_on_two_lane_roads():
    east = astar((3, 10), (30, 10), set(), {})     # the middle road: north lane eastbound
    west = astar((30, 11), (3, 11), set(), {})
    assert all(c[1] == 10 for c in east) and all(c[1] == 11 for c in west)
    against = astar((30, 10), (3, 10), set(), {})  # starting in the wrong lane: it moves over
    assert sum(1 for c in against if c[1] == 11) > len(against) // 2


def test_astar_goes_around_a_known_pothole_when_the_road_allows():
    start, goal = (3, 10), (30, 10)
    hole = (14, 10)
    around = astar(start, goal, set(), {hole: POTHOLE_COST})
    assert list(hole) not in around and around[-1] == [30, 10]


def test_every_face_and_dump_is_reachable_from_the_park():
    for s in W.slots.values():
        assert astar(W.homes[0], tuple(s["access"]), set(), {}) is not None
    for d in W.docks.values():
        assert astar(W.homes[0], tuple(d), set(), {}) is not None
    assert set(DESTINATION.values()) == set(W.docks)


def test_dispatch_sends_the_nearest_free_truck():
    frame = make_frame(initial_state(1), [], [])
    job = make_job("J1", ["C"], DESTINATION["waste"], 0)
    first = choose_robot(frame, job)
    assert first is not None and first != "T15"                  # the standby spare is not free
    assert choose_robot(frame, job, exclude={first}) not in (None, first)


def test_detector_rules_fire_once_on_crossings():
    frame = {
        "t": 500,
        "robots": [{"id": "T01", "blk": STALL_TICKS, "st": "waiting", "job": "J1"},
                   {"id": "T02", "blk": STALL_TICKS + 1, "st": "waiting", "job": None}],
        "jobs": [{"id": "J1", "robot": "T01", "deadline": 500}, {"id": "J2", "robot": "T02", "deadline": 900}],
        "ev": [
            {"type": "contact", "robot": "T03", "with": "K1", "v": 800, "cell": [5, 7]},
            {"type": "dock_scan", "robot": "T04", "dock": "DK1", "job": "J7", "expected": ["A"], "actual": ["B"], "ok": False},
            {"type": "dock_scan", "robot": "T04", "dock": "DK1", "job": "J8", "expected": ["A"], "actual": ["A"], "ok": True},
            {"type": "zone_enter", "robot": "T02", "zone": "bench_upper_w", "restricted": True},
        ],
    }
    got = sorted((f["type"], f["robot"]) for f in detect(frame))
    assert got == [("collision", "T03"), ("stall", "T01"), ("task_overdue", "T01"),
                   ("wrong_item", "T04"), ("zone_breach", "T02")]
