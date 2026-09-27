"""What the live 3D floor and the copilot rely on: coordination events, rich live frames, sensors, the ask call."""
import asyncio
import json
from types import SimpleNamespace

import httpx

from control import assistant
from replay_core.frames import ui_frame
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
    ids = {"R1", "R2", "R3", "R4"}
    for e in ev:
        assert e["robot"] in ids
        if e["type"] == "wait":
            assert e["on"] in ids and e["on"] != e["robot"] and len(e["cell"]) == 2
        if e["type"] == "yield":
            assert e["to"] in ids and e["rule"] in ("head_on", "head_on_retry", "parked", "queue") and e["waited"] > 0
    # every standoff is between two robots that were holding for each other, and one of them yields soon after
    for s in [e for e in ev if e["type"] == "standoff"][:10]:
        pair = {s["robot"], s["with"]}
        later = [e for e in ev if e["type"] == "yield" and {e["robot"], e["to"]} == pair and 0 <= e["t"] - s["t"] <= 60]
        assert later, s


def test_events_do_not_change_the_simulation():
    # coordination events are observations only: same seed, same state hashes as before they existed
    a, b = run(1500, 3), run(1500, 3)
    assert [r["hash"] for r in a.records] == [r["hash"] for r in b.records]


def test_live_frames_carry_claims_goal_and_load():
    d = run(600)
    f = ui_frame(d.records[-1]["frame"])
    r = f["robots"][0]
    assert {"r", "w", "cs", "o", "op", "g", "p"} <= set(r)
    moving = [x for rec in d.records[-200:] for x in ui_frame(rec["frame"])["robots"] if x["st"] == "moving"]
    assert moving and all(x["r"] for x in moving) and all(x["g"] for x in moving)
    assert set(ui_frame(d.records[-1]["frame"], with_paths=False)["robots"][0]) == {"id", "x", "y", "v", "d", "st", "job", "c"}


def frame(**over):
    base = {"robots": [{"id": "R1", "x": 5500, "y": 3500, "v": 100, "dir": "E", "st": "moving", "res": [[5, 3], [6, 3]],
                        "wait_on": "", "path": [[6, 3], [7, 3]], "op": "goto", "goal": [9, 3], "odo": 12345, "carry": []},
                       {"id": "R2", "x": 9500, "y": 3500, "v": 0, "dir": "W", "st": "waiting", "res": [[9, 3]],
                        "wait_on": "R1", "path": [], "op": "goto", "goal": [2, 3], "odo": 0, "carry": ["SKU-C4"]}],
            "pallets": [{"cell": [7, 3]}], "restricted": []}
    base.update(over)
    return base


def test_sensors_match_the_sims_geometry():
    s = assistant.sensors(frame(), "R1", ["speed_cap(aisle_1W, 0.6)", "speed_cap(racks, 0.8)"])
    assert s["cell"] == "c5_3" and s["heading"] == "E" and s["speed_mps"] == 1.0
    obs = s["forward_obstacle"]
    assert obs["what"] == "pallet at c7_3" and obs["gap_m"] == 1.3 and not obs["within_sensor_range"]
    assert s["stopping_distance_m"] > 0.5 and s["reserved_cells"] == ["c5_3", "c6_3"]
    r2 = assistant.sensors(frame(), "R2", [])
    assert r2["waiting_on"] == "R1" and r2["speed_limit_mps"] == 1.2


def test_ask_sends_live_data_to_the_model(monkeypatch):
    seen = {}

    async def fake_context(rt, scope, rid):
        return {"robot": {"id": rid, "status": "waiting", "waiting_on": "R1"}, "kpis": {}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-x"}]})
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "R2 is holding for R1, which has c9_3."}]}],
            "usage": {"input_tokens": 900, "output_tokens": 30}})

    monkeypatch.setattr(assistant, "context", fake_context)
    rt = SimpleNamespace(settings=settings("k", provider="openai", model=""))
    out = asyncio.run(assistant.answer(rt, "Why did R2 stop?", "robot", "R2",
                                       [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}],
                                       httpx.MockTransport(handler)))
    assert out["answer"].startswith("R2 is holding") and out["tokens"] == 930 and out["source"] == "openai:gpt-x"
    assert "robot R2" in seen["instructions"] and seen["input"][0]["content"].startswith("LIVE DATA")
    assert seen["input"][-1] == {"role": "user", "content": "Why did R2 stop?"} and len(seen["input"]) == 4


def test_fallback_without_a_model():
    ctx = {"robot": {"id": "R2", "status": "waiting", "cell": "c9_3", "zones": ["aisle 1E"], "speed_mps": 0.0,
                     "heading": "W", "waiting_on": "R1", "job": None, "carrying": ["Zinc hex bolt"]}}
    text = assistant._fallback(ctx)
    assert "holding for R1" in text and "Zinc hex bolt" in text


def test_throttle():
    for _ in range(assistant.MAX_PER_MINUTE):
        assistant.throttle("tester")
    try:
        assistant.throttle("tester")
        raise AssertionError("should throttle")
    except assistant.AskError:
        pass
