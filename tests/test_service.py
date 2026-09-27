"""Trailer faults and their recovery: the sim side (fault, limp, service moves, repair, spare) and the
control-plane helpers the service desk validates the copilot's decisions with."""
import asyncio
import json
import random

import httpx

from control import service
from control.enterprise import gtin13
from control.llm import structured
from replay_core.world import FAULT_SPEED, GARAGE, W
from tests.driver import Driver
from tests.test_services import settings


def faulted(kind, seed=5, warm=400):
    d = Driver(seed=seed, job_seed=seed)
    d.run(warm)
    d.sim.request_chaos(f"{kind}_fault", wait_ticks=300)
    d.run(40)
    alarm = next(e for e in d.events if e["type"] == "fault_alarm")
    return d, alarm


def robot(d, rid):
    return next(r for r in d.records[-1]["frame"]["robots"] if r["id"] == rid)


def run_until(d, cond, limit=2000):
    for _ in range(limit):
        d.run(1)
        if cond():
            return True
    return False


def test_a_fault_safety_stops_the_robot_and_releases_its_job():
    d, alarm = faulted("tire")
    rid = alarm["robot"]
    r = robot(d, rid)
    assert r["st"] == "fault" and r["svc"] == "fault" and r["v"] == 0 and not r["free"] and r["job"] is None
    assert alarm["code"].startswith("E-DRV") and any(e["type"] == "job_released" and e["robot"] == rid for e in d.events)
    h = r["health"]
    assert min(h["tires"].values()) < 60 and h["slip"] > 12 and h["range"] == 800
    assert service.read_health(h)["fault"] == "tire" and not service.read_health(h)["movable"]
    others = [x for x in d.records[-1]["frame"]["robots"] if x["id"] not in (rid, "R5")]
    assert all(service.read_health(x["health"])["fault"] == "unknown" for x in others)   # healthy robots read nominal


def test_tire_fault_pulls_over_at_a_crawl_then_repairs_back_into_service():
    d, alarm = faulted("tire")
    rid = alarm["robot"]
    cands = service.safe_cells(d.records[-1]["frame"], rid)
    assert cands and all(W.passable(tuple(c["cell"])) for c in cands)
    assert cands == sorted(cands, key=lambda c: (c["score"], c["id"]))
    target = cands[0]["cell"]
    d.sim.submit({"kind": "service", "robot": rid, "op": "move", "cell": target, "by": "test"})
    speeds = []
    assert run_until(d, lambda: (speeds.append(robot(d, rid)["v"]) or True) and robot(d, rid)["svc"] == "fault"
                     and [robot(d, rid)["x"] // 1000, robot(d, rid)["y"] // 1000] == target and d.records[-1]["tick"] > alarm["t"] + 5)
    assert max(speeds) <= FAULT_SPEED["tire"]
    assert any(e["type"] == "service_arrived" and e["robot"] == rid for e in d.events)
    d.sim.submit({"kind": "service", "robot": rid, "op": "repair", "by": "Tobin Reyes"})
    d.run(5)
    r = robot(d, rid)
    assert r["svc"] is None and r["fault"] is None
    assert r["st"] != "fault" and service.read_health(r["health"])["fault"] == "unknown"
    rep = next(e for e in d.events if e["type"] == "repaired")
    assert rep["by"] == "Tobin Reyes" and rep["fault"] == "tire"
    assert run_until(d, lambda: robot(d, rid)["free"] or robot(d, rid)["job"] is not None, 400)   # rejoins the fleet


def test_sensor_fault_drives_to_the_garage_and_the_spare_takes_over():
    d, alarm = faulted("sensor", seed=6)
    rid = alarm["robot"]
    r = robot(d, rid)
    assert service.read_health(r["health"])["fault"] == "sensor" and r["health"]["range"] < 800
    spare = robot(d, "R5")
    assert spare["st"] == "standby" and not spare["free"] and [spare["x"] // 1000, spare["y"] // 1000] == list(GARAGE[0])
    bay = list(GARAGE[1])
    d.sim.submit({"kind": "service", "robot": rid, "op": "move", "cell": bay, "by": "copilot"})
    d.sim.submit({"kind": "service", "robot": "R5", "op": "deploy", "by": "copilot"})
    d.run(3)
    assert robot(d, "R5")["free"] and robot(d, "R5")["st"] != "standby"
    speeds = []
    assert run_until(d, lambda: (speeds.append(robot(d, rid)["v"]) or True) and robot(d, rid)["svc"] == "fault"
                     and [robot(d, rid)["x"] // 1000, robot(d, rid)["y"] // 1000] == bay, 3000)
    assert 0 < max(speeds) <= FAULT_SPEED["sensor"]
    d.sim.submit({"kind": "service", "robot": rid, "op": "repair", "by": "tech"})
    d.sim.submit({"kind": "service", "robot": rid, "op": "standby", "by": "copilot"})
    d.run(30)
    r = robot(d, rid)
    assert r["st"] == "standby" and r["svc"] == "standby" and not r["free"] and [r["x"] // 1000, r["y"] // 1000] == bay


def test_faults_and_service_replay_exactly():
    def go():
        d, alarm = faulted("sensor", seed=6)
        d.sim.submit({"kind": "service", "robot": alarm["robot"], "op": "move", "cell": list(GARAGE[2]), "by": "x"})
        d.run(200)
        return [r["hash"] for r in d.records]
    assert go() == go()


def test_invalid_service_commands_are_ignored():
    d = Driver(seed=1, job_seed=1)
    d.run(50)
    d.sim.submit({"kind": "service", "robot": "R1", "op": "fly"})
    d.sim.submit({"kind": "service", "robot": "R1", "op": "move", "cell": [2, 2]})   # a rack
    d.run(2)
    assert sum(e["type"] == "input_ignored" and e.get("kind") == "service" for e in d.events) == 2


def test_beside_and_walk_route():
    d, alarm = faulted("tire")
    f = d.records[-1]["frame"]
    rid = alarm["robot"]
    rc = service.cell_of(robot(d, rid))
    spot = service.beside(f, rc)
    assert W.passable(spot) and abs(spot[0] - rc[0]) + abs(spot[1] - rc[1]) <= 1
    route = service.walk_route(service.TOOL_CRIB, spot, f)
    assert route[0] == list(service.TOOL_CRIB) and route[-1] == list(spot)
    assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in zip(route, route[1:]))


def test_gtin13_check_digit():
    for sku in ["CHH-1020", "AB-10001", "X"]:
        code = gtin13(sku)
        assert len(code) == 13 and code.isdigit()
        assert sum(int(dgt) * (3 if i % 2 else 1) for i, dgt in enumerate(code)) % 10 == 0
    assert gtin13("CHH-1020") == gtin13("CHH-1020") != gtin13("CHH-1021")


def test_structured_call_uses_a_strict_schema():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m1"}]})
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"candidate": "c6_2", "reason": "right of the lane"}'}]}],
            "usage": {"input_tokens": 400, "output_tokens": 20}})

    ans, source, tokens = asyncio.run(structured(settings("k", provider="openai", model=""), "pick", {"a": 1},
                                                 service.PULL_OVER, "pull_over", httpx.MockTransport(handler)))
    assert ans == {"candidate": "c6_2", "reason": "right of the lane"} and source == "openai:m1" and tokens == 420
    assert seen["text"]["format"]["strict"] and seen["text"]["format"]["schema"]["required"] == ["candidate", "reason"]


def test_new_runs_start_with_a_standby_spare_and_old_capsule_maps_still_match():
    from replay_core.world import MAP_HASH
    from replay_core.state import initial_state
    st = initial_state(random.randint(1, 10**6))
    assert st["robots"]["R5"]["svc"] == "standby" and len(st["robots"]) == 5
    assert "R5" not in {r["id"] for r in W.as_json()["robots"]} and W.as_json()["spares"][0]["id"] == "R5"
    assert MAP_HASH == W.map_hash   # the spare and garage are outside the hashed map
