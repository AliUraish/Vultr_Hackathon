"""The incident investigator: scripted method, model-driven tool calling, budgets and fallbacks."""
import asyncio
import json

import httpx
import pytest

from control import diagnosis
from control.investigator import MAX_STEPS, Investigation
from tests.driver import Driver
from tests.local_lab import LocalLab, MemoryRecorder
from tests.test_scenarios import CASES
from tests.test_services import settings


@pytest.fixture(scope="module")
def cases():
    out = []
    for case in CASES:
        d = Driver(seed=case.seed, job_seed=case.seed)
        d.run(400)
        d.sim.request_chaos(case.scenario, wait_ticks=1200)
        found = d.run(3500, stop=lambda fs, c=case: any(f["type"] == c.failure for f in fs))
        failure = next(f for f in found if f["type"] == case.failure)
        cap = d.cut(failure)
        ctx = diagnosis.context(cap, {"detail": failure["detail"]}, [], [])
        out.append((case, cap, ctx))
    return out


def investigate(cap, ctx, s=None, transport=None):
    lab, rec = LocalLab({1: cap}), MemoryRecorder()
    inv = Investigation(s or settings(), lab, 1, ctx, rec, transport)
    return asyncio.run(inv.run()), rec, lab


EXPECT = {"rockfall": "speed_cap(benches,", "grade_mixup": "grade_check(ore)",
          "blast_closure": "respect_closures(benches)"}


def test_scripted_investigator_finds_a_robust_fix(cases):
    for case, cap, ctx in cases:
        report, rec, _ = investigate(cap, ctx)
        tools = [s["tool"] for s in rec.steps.values()]
        assert tools[:3] == ["reproduce", "isolate_cause", "what_if"], tools
        assert tools[-1] == "submit_findings" and all(s["status"] == "done" for s in rec.steps.values())
        assert all(s["why"] for s in rec.steps.values())
        top = report["fixes"][0]
        assert top["fix"].replace(" ", "").startswith(EXPECT[case.scenario].replace(" ", "")), report["fixes"]
        assert top["stress"]["robustness"] >= 0.95 and report["confidence"] == "high"
        assert report["source"] == "scripted" and report["simulations"] > 30
        assert report["rejected"] and all(r["stress"]["robustness"] < 0.95 for r in report["rejected"])


def test_scripted_tunes_the_speed_cap(cases):
    _, cap, ctx = cases[0]
    report, rec, _ = investigate(cap, ctx)
    assert "tune_fix" in [s["tool"] for s in rec.steps.values()]
    top = report["fixes"][0]
    tune = next(s for s in rec.steps.values() if s["tool"] == "tune_fix")
    assert tune["status"] == "done" and top["fix"].startswith("speed_cap(benches,")   # the cheapest safe setting wins


def _chat(calls=None, content=None, usage=(100, 20)):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


def test_model_drives_the_tools(cases):
    _, cap, ctx = cases[1]
    script = [
        [("reproduce", {"times": 2, "why": "determinism"})],
        [("stress_test", {"fix": "grade_check(ore)", "variants": 12, "why": "robust?"})],
        [("submit_findings", {"root_cause": "mis-tagged ore face loaded without a grade check",
                              "evidence": ["2/2 reproduced"], "confidence": "high",
                              "fixes": [{"fix": "grade_check(ore)", "why": "targeted"},
                                        {"fix": "grade_check(*)", "why": "broader"},
                                        {"fix": "teleport(T01)", "why": "invalid"}],
                              "rejected": [{"fix": "speed_cap(benches, 20)", "reason": "irrelevant"}]})],
    ]
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        assert body["tools"] and body["tool_choice"] == "required" and body["max_tokens"]
        return httpx.Response(200, json=_chat(script[len(seen) - 1]))

    s = settings("k", provider="vultr", model="llama-test")
    report, rec, lab = investigate(cap, ctx, s, httpx.MockTransport(handler))
    assert report["source"] == "vultr:llama-test" and report["tokens"] == {"in": 300, "out": 60}
    assert [f["fix"] for f in report["fixes"]] == ["grade_check(ore)", "grade_check(*)"]
    # the second fix was never stress-tested by the model: the investigator does it before the gate
    assert [s["tool"] for s in rec.steps.values()] == ["reproduce", "stress_test", "stress_test", "submit_findings"]
    assert all(f["stress"]["robustness"] is not None for f in report["fixes"])
    assert any("not a valid rule" in r["reason"] for r in report["rejected"])
    # tool results went back to the model with the call ids
    tool_msgs = [m for m in seen[-1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_0", "call_0"]
    assert "robustness" in json.loads(tool_msgs[1]["content"])


def test_bad_tool_arguments_come_back_as_errors(cases):
    _, cap, ctx = cases[1]
    script = [[("stress_test", {"fix": "warp_speed(9)", "why": "?"})],
              [("submit_findings", {"root_cause": "x", "evidence": [], "confidence": "low",
                                    "fixes": [{"fix": "grade_check(*)", "why": "y"}]})]]
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 2:
            msgs = json.loads(req.content)["messages"]
            assert "unknown fix" in json.loads(msgs[-1]["content"])["error"]
        return httpx.Response(200, json=_chat(script[n["i"] - 1]))

    report, rec, _ = investigate(cap, ctx, settings("k", provider="vultr", model="m"), httpx.MockTransport(handler))
    assert rec.steps[1]["status"] == "error" and report["fixes"][0]["fix"] == "grade_check(*)"


def test_budget_forces_submission(cases):
    _, cap, ctx = cases[2]
    forced: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["tool_choice"] != "required":
            forced.append(body["tool_choice"])
            return httpx.Response(200, json=_chat([("submit_findings", {
                "root_cause": "closure ignored", "evidence": [], "confidence": "medium",
                "fixes": [{"fix": "respect_closures(benches)", "why": "z"}]})]))
        return httpx.Response(200, json=_chat([("what_if", {"rules": ["respect_closures(benches)"], "why": "again"})]))

    report, rec, _ = investigate(cap, ctx, settings("k", provider="vultr", model="m"), httpx.MockTransport(handler))
    assert forced == [{"type": "function", "function": {"name": "submit_findings"}}]
    assert sum(s["tool"] == "what_if" for s in rec.steps.values()) == MAX_STEPS
    assert report["fixes"][0]["fix"] == "respect_closures(benches)"


def test_model_outage_hands_over_to_the_scripted_investigator(cases):
    _, cap, ctx = cases[2]
    down = httpx.MockTransport(lambda req: httpx.Response(503, text="overloaded"))
    report, rec, _ = investigate(cap, ctx, settings("k", provider="vultr", model="m"), down)
    assert report["source"] == "vultr:m → scripted" and "scripted investigator took over" in report["notes"][0]
    assert report["fixes"][0]["fix"] == "respect_closures(benches)"


def test_rejected_parameters_are_dropped_and_retried(cases):
    _, cap, ctx = cases[2]
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if "parallel_tool_calls" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'parallel_tool_calls'"}})
        return httpx.Response(200, json=_chat([("submit_findings", {
            "root_cause": "c", "evidence": [], "confidence": "low",
            "fixes": [{"fix": "respect_closures(*)", "why": "w"}]})]))

    report, _, _ = investigate(cap, ctx, settings("k", provider="vultr", model="m"), httpx.MockTransport(handler))
    assert "parallel_tool_calls" not in bodies[-1] and report["source"] == "vultr:m"

