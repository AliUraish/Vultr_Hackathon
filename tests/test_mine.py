"""How haul trucks drive the pit: lidar and potholes, bends, loads, one-lane cuts, queues, right-of-way rulings."""
import pytest

from replay_core.world import CORNER_SPEED, MAX_SPEED, MAX_SPEED_LOADED, ROAD_KINDS, STALL_TICKS, SURVEY_TICKS, W
from tests.driver import Driver


@pytest.fixture(scope="module")
def shift() -> Driver:
    d = Driver(seed=5, job_seed=5)
    d.run(6000)   # ten minutes of hauling
    return d


def test_the_fleet_hauls(shift):
    done = [e for e in shift.events if e["type"] == "job_done"]
    assert len(done) >= 15 and all(e["ok"] for e in done)
    assert not [f for f in shift.failures if f["type"] in ("collision", "wrong_item", "zone_breach")]


def test_potholes_are_seen_by_lidar_first_and_crossed_at_crawl_speed(shift):
    seen: dict[str, int] = {}
    entered = 0
    for e in shift.events:
        if e["type"] == "pothole_detected":
            seen.setdefault(e["id"], e["t"])
        elif e["type"] == "pothole_enter":
            entered += 1
            assert e["id"] in seen and seen[e["id"]] < e["t"], "entered a pothole the lidar had not reported"
            assert e["v"] <= ROAD_KINDS[e["kind"]]["speed"]
    assert entered > 0 and not [e for e in shift.events if e["type"] in ("pothole_strike", "bump_jump")]


def test_the_roads_change_and_the_lidar_survey_maps_them(shift):
    """Sand drifts, bumps and potholes form (and clear) between surveys; the 5 s survey maps new ones."""
    changed = [e for e in shift.events if e["type"] == "road_changed"]
    formed = [e for e in changed if e["op"] == "formed"]
    assert len(formed) >= 10 and {e["kind"] for e in formed} >= {"sand", "bump"}
    surveyed = [e for e in shift.events if e["type"] == "pothole_detected" and e["survey"]]
    weather_at = {e["t"] % SURVEY_TICKS for e in changed}
    survey_at = {e["t"] % SURVEY_TICKS for e in surveyed}
    assert len(weather_at) == 1 and len(survey_at) == 1        # on a fixed beat...
    assert (weather_at.pop() - survey_at.pop()) % SURVEY_TICKS == SURVEY_TICKS // 2   # ...halfway between surveys
    assert len(shift.records[-1]["frame"]["potholes"]) <= 16


def test_trucks_take_bends_without_stopping_and_respect_speed_limits(shift):
    bends = 0
    prev: dict[str, dict] = {}
    for rec in shift.records:
        for r in rec["frame"]["robots"]:
            p = prev.get(r["id"])
            if p and p["v"] > 0 and r["v"] > 0 and r["dir"] != p["dir"]:
                bends += 1
                assert min(p["v"], r["v"]) <= CORNER_SPEED + 20
            assert r["v"] <= (MAX_SPEED_LOADED if r["carry"] else MAX_SPEED)
            prev[r["id"]] = r
    assert bends > 20


def test_a_one_lane_cut_holds_one_truck_at_a_time(shift):
    for rec in shift.records:
        inside: dict[int, set] = {}
        for r in rec["frame"]["robots"]:
            sec = W.sections.get((r["x"] // 20_000, r["y"] // 20_000))
            if sec is not None:
                inside.setdefault(sec, set()).add(r["id"])
        assert all(len(v) == 1 for v in inside.values()), f"two trucks in one cut at t{rec['tick']}: {inside}"


def test_queueing_under_an_excavator_is_not_a_stall(shift):
    queued = [(rec["tick"], r) for rec in shift.records for r in rec["frame"]["robots"] if r["st"] == "queued"]
    assert all(r["blk"] < STALL_TICKS for _, r in queued)


def test_a_ruling_decides_who_yields():
    d = Driver(seed=1, job_seed=1)
    standoff = None
    for _ in range(900):
        d.run(10)
        so = [e for e in d.events[-60:] if e["type"] == "standoff"]
        if so:
            standoff = so[-1]
            break
    assert standoff, "no head-on meeting in 2.5 h of hauling"
    a, b = sorted((standoff["robot"], standoff["with"]))
    # the rules would make the higher number yield; the ruling says the lower one does
    d.sim.submit({"kind": "traffic", "robot": a, "to": b, "op": "yield", "reason": "b is loaded", "by": "ai:test"})
    d.run(2)
    y = [e for e in d.events if e["type"] == "yield" and e.get("rule") == "ai"]
    assert y and y[-1]["robot"] == a and y[-1]["to"] == b and y[-1]["reason"] == "b is loaded"
    d.run(300)
    moved = {r["id"] for rec in d.records[-200:] for r in rec["frame"]["robots"] if r["v"] > 0}
    assert {a, b} <= moved


def test_a_stale_ruling_is_ignored():
    d = Driver(seed=2, job_seed=2)
    d.run(100)
    d.sim.submit({"kind": "traffic", "robot": "T01", "to": "T02", "op": "yield", "by": "ai:test"})
    d.run(2)
    assert any(e["type"] == "traffic_stale" for e in d.events)
    assert not any(e["type"] == "yield" and e.get("rule") == "ai" for e in d.events)


def test_road_damage_makes_a_fresh_pothole_that_trucks_find():
    d = Driver(seed=3, job_seed=3)
    d.run(300)
    d.sim.request_chaos("road_damage", wait_ticks=600)
    d.run(1500)
    formed = [e for e in d.events if e["type"] == "pothole_formed"]
    assert formed
    assert any(e["type"] == "pothole_detected" and e["id"] == formed[0]["id"] for e in d.events)
