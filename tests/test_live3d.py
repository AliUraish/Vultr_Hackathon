"""What the live 3D pit and the copilot rely on: coordination events, rich live frames, sensors, the ask call."""
import asyncio
import json
from types import SimpleNamespace

import httpx

from control import assistant
from replay_core.frames import ui_frame
from replay_core.world import ROBOT_SPECS, SPARE_SPECS
from tests.driver import Driver
from tests.test_services import settings


def run(ticks=4000, seed=7):
    d = Driver(seed=seed, job_seed=seed)
    d.run(ticks)
    return d


def test_fleet_coordination_is_visible_as_events():
    d = run()
    ev = [e for e in d.events if e["type"] in ("wait", "standoff", "yield", "resume")]
    kinds = {e["type"] for e in ev}
    assert kinds == {"wait", "standoff", "yield", "resume"}
    ids = {s["id"] for s in ROBOT_SPECS + SPARE_SPECS}
    for e in ev:
        assert e["robot"] in ids
        if e["type"] == "wait":
            assert e["on"] in ids and e["on"] != e["robot"] and len(e["cell"]) == 2
        if e["type"] == "yield":
            assert e["to"] in ids and e["rule"] in ("head_on", "head_on_retry", "parked", "queue", "make_way") \
                and e["waited"] > 0
    # every standoff is between two trucks holding for each other, and one of them yields once the fallback fires
    for s in [e for e in ev if e["type"] == "standoff"][:10]:
        pair = {s["robot"], s["with"]}
        later = [e for e in ev if e["type"] == "yield" and {e["robot"], e["to"]} == pair and 0 <= e["t"] - s["t"] <= 200]
        assert later, s


def test_events_do_not_change_the_simulation():
    # coordination events are observations only: same seed, same state hashes as before they existed
    a, b = run(1500, 3), run(1500, 3)
    assert [r["hash"] for r in a.records] == [r["hash"] for r in b.records]


def test_live_frames_carry_claims_goal_and_load():
    d = run(600)
    f = ui_frame(d.records[-1]["frame"])
    r = f["robots"][0]
    assert {"r", "w", "cs", "o", "op", "g", "p", "h"} <= set(r)
    moving = [x for rec in d.records[-200:] for x in ui_frame(rec["frame"])["robots"] if x["st"] == "moving"]
    assert moving and all(x["r"] for x in moving) and all(x["g"] for x in moving)
    assert set(ui_frame(d.records[-1]["frame"], with_paths=False)["robots"][0]) == {"id", "x", "y", "v", "d", "st", "job", "c"}


def frame(**over):
    base = {"robots": [{"id": "T01", "x": 110000, "y": 70000, "v": 1000, "dir": "E", "st": "moving",
                        "res": [[5, 3], [6, 3]], "wait_on": "", "path": [[6, 3], [7, 3]], "op": "goto", "goal": [9, 3],
                        "odo": 12345678, "carry": []},
                       {"id": "T02", "x": 190000, "y": 70000, "v": 0, "dir": "W", "st": "waiting", "res": [[9, 3]],
                        "wait_on": "T01", "path": [], "op": "goto", "goal": [2, 3], "odo": 0, "carry": ["MAT-A"]}],
            "pallets": [{"cell": [7, 3]}], "restricted": [],
            "potholes": [{"id": "H1", "cell": [6, 3], "depth": 700, "known": True}]}
    base.update(over)
    return base


def test_sensors_match_the_sims_geometry():
    s = assistant.sensors(frame(), "T01", ["speed_cap(crest_road, 30)", "speed_cap(benches, 20)"])
    assert s["cell"] == "c5_3" and s["heading"] == "E" and s["speed_kmh"] == 36.0
    obs = s["forward_obstacle"]
    assert obs["what"] == "fallen rock at c7_3" and obs["gap_m"] == 32.0 and obs["within_lidar_range"]
    assert s["potholes_nearby"][0]["at"] == "c6_3" and s["potholes_nearby"][0]["on_route"]
    assert s["stopping_distance_m"] > 25 and s["reserved_cells"] == ["c5_3", "c6_3"] and s["speed_limit_kmh"] == 30.0
    t2 = assistant.sensors(frame(), "T02", [])
    assert t2["waiting_on"] == "T01" and t2["speed_limit_kmh"] == 32 and t2["loaded"]


def test_ask_sends_live_data_to_the_model(monkeypatch):
    seen = {}

    async def fake_context(rt, scope, rid):
        return {"robot": {"id": rid, "status": "waiting", "waiting_on": "T01"}, "kpis": {}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-x"}]})
        assert req.url.path.endswith("/chat/completions")
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "T02 is holding for T01, which has c9_3."}}], "usage": {"prompt_tokens": 900, "completion_tokens": 30}})

    monkeypatch.setattr(assistant, "context", fake_context)
    rt = SimpleNamespace(settings=settings("k", provider="vultr", model=""))
    out = asyncio.run(assistant.answer(rt, "Why did T02 stop?", "robot", "T02",
                                       [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}],
                                       httpx.MockTransport(handler)))
    assert out["answer"].startswith("T02 is holding") and out["tokens"] == 930 and out["source"] == "vultr:deepseek-x"
    msgs = seen["messages"]
    assert "haul truck T02" in msgs[0]["content"] and msgs[1]["content"].startswith("LIVE DATA")
    assert msgs[-1] == {"role": "user", "content": "Why did T02 stop?"} and len(msgs) == 5


def test_fallback_without_a_model():
    ctx = {"robot": {"id": "T02", "status": "waiting", "activity": "holding for T01", "cell": "c9_3",
                     "zones": ["bench upper w"], "speed_kmh": 0.0, "heading": "W", "waiting_on": "T01",
                     "load_ticket": None, "carrying": ["High-grade copper ore"]}}
    text = assistant._fallback(ctx)
    assert "holding for T01" in text and "High-grade copper ore" in text


def test_throttle():
    for _ in range(assistant.MAX_PER_MINUTE):
        assistant.throttle("tester")
    try:
        assistant.throttle("tester")
        raise AssertionError("should throttle")
    except assistant.AskError:
        pass


def test_traffic_rules_give_loaded_trucks_right_of_way():
    from control import traffic
    a = {"id": "T03", "loaded": True, "remote_control": False, "pull_aside_segments": 1, "reroute_extra_m": 200}
    b = {"id": "T05", "loaded": False, "remote_control": False, "pull_aside_segments": 2, "reroute_extra_m": None}
    first, yielder, how, why = traffic.rule(a, b)
    assert (first, yielder, how) == ("T03", "T05", "pull_aside") and "loaded" in why
    c = {**b, "loaded": True, "pull_aside_segments": 1, "reroute_extra_m": 40}
    first, yielder, how, _ = traffic.rule(a, c)
    assert yielder == "T05" and how == "reroute"          # both loaded: the one that clears the way more easily yields
    d = {**a, "id": "T07", "remote_control": True}
    assert traffic.rule(a, d)[0] == "T07"                 # a faulted truck under remote control goes first


def test_json_skeleton_for_models_without_schema_mode():
    from control.llm import _example, obj
    schema = obj({"first": {"type": "string"}, "how": {"type": "string", "enum": ["pull_aside", "reroute"]},
                  "n": {"type": "integer"}, "items": {"type": "array", "items": obj({"x": {"type": "boolean"}})}})
    assert _example(schema) == {"first": "...", "how": "pull_aside|reroute", "n": 0, "items": [{"x": False}]}
