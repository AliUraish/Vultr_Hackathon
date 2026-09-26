"""Control-plane and VM B pieces that don't need Postgres: diagnosis, replay worker, sim node telemetry."""
import asyncio
import json

import httpx
import pytest

from control import diagnosis
from control.config import Settings
from replay_core import capsule
from replay_core.state import state_hash
from replay_core.world import MAP_HASH
from simnode.app import Node, _check_steps
from simnode.worker import run_job
from tests.driver import Driver
from tests.test_scenarios import CASES, right_fix


def settings(key: str = "", provider: str = "vultr", model: str = "") -> Settings:
    return Settings(database_url="postgresql://x", node_token="t", simnode_url="http://b", session_secret="s",
                    admin_user="ops", admin_password="p", inference_provider=provider, inference_key=key,
                    inference_url="https://inference.test/v1", inference_model=model, auto_jobs=True,
                    cookie_secure=False, retention_hours=1)


@pytest.fixture(scope="module")
def cases():
    out = []
    for case in CASES:
        d = Driver(seed=case.seed, job_seed=case.seed)
        d.run(300)
        d.sim.request_chaos(case.scenario, wait_ticks=600)
        found = d.run(2500, stop=lambda fs, c=case: any(f["type"] == c.failure for f in fs))
        failure = next(f for f in found if f["type"] == case.failure)
        out.append((case, failure, d.cut(failure), d))
    return out


def test_playbook_puts_the_proven_fix_first(cases):
    for case, failure, cap, d in cases:
        ctx = diagnosis.context(cap, {"detail": failure["detail"]}, [], [])
        hyps = diagnosis.playbook(ctx)
        assert 1 <= len(hyps) <= 3
        assert hyps[0]["fix"] == right_fix(case, failure, d)[0], case.scenario
        assert all(h["cause"] for h in hyps)


def test_playbook_never_proposes_a_deployed_rule(cases):
    case, failure, cap, d = cases[0]
    deployed = right_fix(case, failure, d)
    hyps = diagnosis.playbook(diagnosis.context(cap, {"detail": failure["detail"]}, [], deployed))
    assert deployed[0] not in [h["fix"] for h in hyps]


def test_validate_keeps_only_dsl_rules():
    hyps, errors = diagnosis.validate({"hypotheses": [
        {"cause": "x", "fix": "__import__('os').system('rm -rf /')"},
        {"cause": "y", "fix": "speed_cap( racks , 0.50 )"},
        {"cause": "z", "fix": "speed_cap(racks, 0.5)"},     # duplicate after normalizing
        {"cause": 3, "fix": "min_clearance(0.4)"},
    ]}, [])
    assert [h["fix"] for h in hyps] == ["speed_cap(racks, 0.5)"]
    assert len(errors) == 3


def test_parse_json_accepts_fenced_answers():
    assert diagnosis.parse_json('Sure!\n```json\n{"hypotheses": []}\n```') == {"hypotheses": []}


def test_inference_retries_once_with_the_validation_errors(cases):
    case, failure, cap, _ = cases[0]
    ctx = diagnosis.context(cap, {"detail": failure["detail"]}, [], [])
    answers = ["I think it was going too fast.",
               json.dumps({"hypotheses": [{"cause": "too fast in the aisle", "fix": "speed_cap(racks, 0.5)",
                                           "rationale": "stopping distance"}]})]
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer k"
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "some-embedding"}, {"id": "llama-3.3-70b-instruct"}]})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": answers[len(sent) - 1]}}]})

    hyps, source, notes = asyncio.run(diagnosis.diagnose(settings("k"), ctx, httpx.MockTransport(handler)))
    assert source == "vultr:llama-3.3-70b-instruct"
    assert "response_format" not in sent[0]  # not every OpenAI-compatible server supports JSON mode
    assert [h["fix"] for h in hyps] == ["speed_cap(racks, 0.5)"]
    assert len(sent) == 2 and "problems" in sent[1]["messages"][-1]["content"]
    assert notes


def test_inference_outage_falls_back_to_playbook(cases):
    case, failure, cap, _ = cases[0]
    ctx = diagnosis.context(cap, {"detail": failure["detail"]}, [], [])
    down = httpx.MockTransport(lambda req: httpx.Response(503, text="down"))
    hyps, source, notes = asyncio.run(diagnosis.diagnose(settings("k"), ctx, down))
    assert source == "playbook" and hyps and "inference failed" in notes[0]


def test_worker_reproduces_and_trials(cases):
    case, failure, cap, d = cases[0]
    job = {"kind": "reproduce", "policy_rules": None, "policy_version": None, "control_failures": None,
           "control_rules": None}
    rep = run_job(job, cap)
    assert rep["status"] == "done" and rep["outcome"] == "reproduced" and rep["matches_live"]
    assert rep["frames"] and set(rep["frames"][0]) == {"t", "robots", "pallets", "zones", "ev"}
    trial = run_job({**job, "kind": "trial", "policy_rules": right_fix(case, failure, d),
                     "control_failures": rep["failures"]}, cap)
    assert trial["outcome"] == "avoided"
    regression = run_job({**job, "kind": "regression", "policy_rules": right_fix(case, failure, d),
                          "control_rules": []}, cap)
    assert regression["frames"] is None


def test_worker_reports_errors_instead_of_crashing(cases):
    broken = dict(cases[0][2], hash="0" * 64)
    out = run_job({"kind": "reproduce", "policy_rules": None, "policy_version": None,
                   "control_failures": None, "control_rules": None}, broken)
    assert out["status"] == "error" and "CapsuleError" in out["error"]


def test_sim_node_streams_contiguous_ticks_and_snapshots():
    posted: list[dict] = []
    fail_next = {"n": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if req.url.path == "/api/node/hello":
            assert body["map"] == MAP_HASH
            return httpx.Response(200, json={"policy": {"version": 3, "rules": ["speed_cap(racks, 0.5)"]}})
        if fail_next["n"]:
            fail_next["n"] -= 1
            return httpx.Response(500)
        posted.append(body)
        return httpx.Response(200, json={"ok": True})

    async def go() -> Node:
        node = Node("http://a", "t", seed=42, transport=httpx.MockTransport(handler))
        await node.hello()
        for _ in range(120):
            node.tick_once()
        assert await node.send_once() is False and len(node.outbox) == 120  # nothing lost on failure
        while node.outbox:
            assert await node.send_once()
        return node

    node = asyncio.run(go())
    ticks = [t["tick"] for b in posted for t in b["ticks"]]
    assert ticks == list(range(1, 121))
    snaps = [s for b in posted for s in b["snapshots"]]
    assert [s["tick"] for s in snaps] == [0, 50, 100]
    assert all(s["hash"] == state_hash(s["state"]) for s in snaps)
    assert all(b["policy_version"] == 3 for b in posted)
    assert node.sim.state["policy"]["rules"] == ["speed_cap(racks, 0.5)"]


def test_fleet_api_rejects_bad_steps():
    from fastapi import HTTPException
    for bad in ([], [{"op": "fly"}], [{"op": "goto", "cell": [0, 0]}], [{"op": "pick", "slot": "Z9"}],
                [{"op": "drop", "dock": "DK9"}]):
        with pytest.raises(HTTPException):
            _check_steps(bad)
    _check_steps([{"op": "goto", "cell": [3, 3]}, {"op": "pick", "slot": "A2"}, {"op": "drop", "dock": "DK1"}])


def test_capsule_survives_json_round_trip(cases):
    cap = json.loads(json.dumps(cases[1][2]))
    assert capsule.run(cap, keep_frames=False)["matches_live"]


def _openai_capture(models: list[str]):
    sent: list[dict] = []
    answer = json.dumps({"hypotheses": [{"cause": "too fast", "fix": "speed_cap(racks, 0.5)"}]})

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    return sent, httpx.MockTransport(handler)


def test_openai_picks_a_small_chat_model_and_uses_json_mode(cases):
    case, failure, cap, _ = cases[0]
    ctx = diagnosis.context(cap, {"detail": failure["detail"]}, [], [])
    sent, transport = _openai_capture(["gpt-3.5-turbo-instruct", "text-embedding-3-small", "gpt-4o",
                                       "gpt-4.1-mini", "whisper-1"])
    hyps, source, _ = asyncio.run(diagnosis.diagnose(settings("k", "openai"), ctx, transport))
    assert source == "openai:gpt-4.1-mini" and hyps
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in sent[0] and "temperature" not in sent[0] and sent[0]["max_completion_tokens"]


def test_rejected_parameters_are_adapted_and_resent(cases):
    """A server that rejects max_tokens / temperature (e.g. a newer model) still gets answered."""
    case, failure, cap, _ = cases[0]
    ctx = diagnosis.context(cap, {"detail": failure["detail"]}, [], [])
    sent: list[dict] = []
    answer = json.dumps({"hypotheses": [{"cause": "too fast", "fix": "speed_cap(racks, 0.5)"}]})

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        sent.append(body)
        for bad in ("max_tokens", "temperature"):
            if bad in body:
                return httpx.Response(400, text=f"Unsupported parameter: '{bad}' is not supported with this model.")
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    hyps, source, notes = asyncio.run(diagnosis.diagnose(settings("k", "vultr", model="future-model"), ctx,
                                                         httpx.MockTransport(handler)))
    assert source == "vultr:future-model" and hyps
    assert "max_tokens" not in sent[-1] and "temperature" not in sent[-1] and sent[-1]["max_completion_tokens"]
    assert len(notes) == 2


def test_inference_config_from_env(monkeypatch):
    from control.config import _inference
    assert _inference({"INFERENCE_KEY": "sk", "INFERENCE_PROVIDER": "openai"}) == (
        "openai", "sk", "https://api.openai.com/v1", "")
    assert _inference({"VULTR_INFERENCE_KEY": "vk"})[:3] == ("vultr", "vk", "https://api.vultrinference.com/v1")
    assert _inference({})[1] == ""  # no key: playbook only


def test_reinject_recreates_the_original_situation():
    from replay_core.scenarios import check_hints, reinject_hints, resolve
    from replay_core.world import W

    # A pallet that fell 0.3 m ahead of a robot in aisle_5E is recreated in aisle_5E, at that gap,
    # ahead of any moving robot (after a speed cap no robot is "fast").
    assert reinject_hints({"type": "spawn_pallet", "cell": [15, 5], "gap_mm": 300}) == {
        "min_v": 1, "zone": "aisle_5E", "fallback_zone": "racks", "fallback_after": 300, "gap_mm": 300}
    assert reinject_hints({"type": "mislabel", "slot": "C4"}) == {"cls": W.slots["C4"]["cls"]}
    assert reinject_hints({"type": "restrict_zone", "zone": "aisle_3W"}) == {"zone": "aisle_3W", "now": True}
    for bad in ({"zone": "moon"}, {"gap_mm": 5}, {"cls": "gold"}, {"evil": 1}):
        with pytest.raises(ValueError):
            check_hints(bad)

    d = Driver(seed=1, job_seed=1, rules=["speed_cap(racks, 0.5)"])  # nobody is fast any more
    d.run(300)
    got = None
    for _ in range(600):
        got = resolve(d.sim.state, "pallet_drop", {"min_v": 1, "zone": "racks", "gap_mm": 300})
        if got:
            break
        d.run(1)
    assert got and 270 <= got[0]["gap_mm"] <= 330 and got[0]["v"] <= 50
    closed = resolve(d.sim.state, "worker_in_aisle", {"zone": "aisle_3W", "now": True})
    assert closed[0]["zone"] == "aisle_3W"


def test_reinjected_pallet_is_survived_under_the_fix():
    """The demo's proof: same aisle, same gap, fixed policy -> the robot brakes in time."""
    d = Driver(seed=1, job_seed=1, rules=["speed_cap(racks, 0.5)"])
    d.run(300)
    d.sim.request_chaos("pallet_drop", 900, {"min_v": 1, "zone": "racks", "gap_mm": 300})
    d.run(900)
    assert not d.sim._chaos, "re-inject never found a robot in position"
    assert not [f for f in d.failures if f["type"] == "collision"]


def test_trials_start_from_the_policy_live_at_the_failure():
    from replay_core.capsule import policy_at
    cap = {"snapshot": {"policy": {"rules": ["speed_cap(racks, 0.6)"]}},
           "inputs": [[100, [{"kind": "policy", "version": 5,
                              "rules": ["speed_cap(racks, 0.6)", "require_scan_confirm(loose_small)"]}]],
                      [900, [{"kind": "policy", "version": 6, "rules": ["x"]}]]]}
    assert policy_at(cap, 50) == ["speed_cap(racks, 0.6)"]
    assert policy_at(cap, 500) == ["speed_cap(racks, 0.6)", "require_scan_confirm(loose_small)"]


def test_reinject_falls_back_from_a_quiet_aisle():
    from replay_core.live import ChaosRequest
    req = ChaosRequest("c1", "pallet_drop", 900, {"zone": "aisle_7E", "fallback_zone": "racks",
                                                  "fallback_after": 300, "gap_mm": 300}, created=1000)
    assert req.effective_hints(1100)["zone"] == "aisle_7E"
    assert req.effective_hints(1300) == {"zone": "racks", "gap_mm": 300}


def test_respect_closures_holds_robots_outside_a_closed_aisle():
    """With the rule, a closed aisle is never entered, and waiting outside it is not a stall."""
    from replay_core.world import STALL_TICKS
    for rules, expect_breach in (([], True), (["respect_closures(racks)"], False)):
        d = Driver(seed=1, job_seed=1, rules=rules)
        d.run(300)
        d.sim.request_chaos("worker_in_aisle", 600)
        d.run(900)
        breaches = [f for f in d.failures if f["type"] == "zone_breach"]
        assert bool(breaches) == expect_breach, rules
        if not expect_breach:
            assert not [f for f in d.failures if f["type"] == "stall"]
            held = [r for rec in d.records for r in rec["frame"]["robots"] if r["st"] == "held"]
            assert all(r["blk"] < STALL_TICKS for r in held)


def test_late_traffic_stalls_warn_but_safety_failures_block():
    from replay_core.capsule import classify
    target = {"type": "collision", "robot": "R2"}
    control = [{"type": "collision", "robot": "R2", "tick": 148}]
    late_stall = {"type": "stall", "robot": "R1", "tick": 509}
    assert classify([late_stall], control, target, 168) == ("avoided", [], [late_stall])
    assert classify([{"type": "stall", "robot": "R1", "tick": 150}], control, target, 168)[0] == "regressed"
    assert classify([{"type": "zone_breach", "robot": "R3", "tick": 509}], control, target, 168)[0] == "regressed"
    assert classify([{"type": "collision", "robot": "R2", "tick": 400}], control, target, 168)[0] == "reproduced"
