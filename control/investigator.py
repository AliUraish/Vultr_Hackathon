"""Incident investigator: works a reproduced incident the way an engineer would, entirely in simulation.

Tools (each runs deterministic simulations on the VM B workers, see lab.py):
  reproduce       replay the capsule several times: is it deterministic, does it match the live run?
  isolate_cause   delta debugging: the smallest set of recorded events that still causes the failure
  what_if         the exact incident with extra rules in force (counterfactual)
  stress_test     a fix against N variants of the incident: robustness and throughput cost
  tune_fix        one fix at a range of settings: the cheapest setting that stays safe
  submit_findings root cause, evidence, ranked fixes, rejected fixes

Every call carries a `why` and is written to investigation_steps with its result, so the trace a
person reviews is exactly what the agent did. The agent never touches the live fleet: its fixes
become hypotheses that still need the exact-incident trial, the regression suite and a human.

Drivers: OpenAI tool calling (any OpenAI-compatible endpoint, INFERENCE_*), or a scripted
investigator that follows the same method with the playbook's candidates when there is no key
or the model fails.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

import httpx

from replay_core import experiments as xp
from replay_core.policy import PolicyError, normalize
from replay_core.world import SENSOR_RANGE, W

from . import diagnosis
from .config import Settings
from .db import log_event
from .diagnosis import InferenceError, _pick_model
from .lab import MIN_ROBUSTNESS

log = logging.getLogger("replay.investigator")

MAX_STEPS = 12          # tool calls before the agent must submit
MAX_SIMS = 1200         # simulations before the agent must submit
MAX_FIXES = 3


class Lab(Protocol):
    async def reproduce(self, cid: int, times: int = 3, inv: int | None = None) -> dict: ...
    async def isolate(self, cid: int, budget: int = 90, inv: int | None = None) -> dict: ...
    async def what_if(self, cid: int, rules: list[str], inv: int | None = None) -> dict: ...
    async def stress(self, cid: int, fix: str, n: int | None = None, seed: int = 0, inv: int | None = None) -> dict: ...
    async def tune(self, cid: int, fix: str, values: list[float] | None = None, n: int | None = None,
                   seed: int = 0, inv: int | None = None) -> dict: ...


class Recorder(Protocol):
    inv: int | None

    async def step(self, n: int, tool: str, args: dict, why: str) -> None: ...
    async def step_done(self, n: int, status: str, summary: str, experiment: int | None) -> None: ...
    async def note(self, text: str) -> None: ...


# ---------------------------------------------------------------- tools

def _why() -> dict:
    return {"type": "string", "description": "One sentence: what you expect to learn and why it matters now."}


TOOLS = [
    {"type": "function", "function": {
        "name": "reproduce",
        "description": "Replay the incident capsule several times on the simulation workers with the recorded "
                       "policy. Confirms the failure is deterministic and matches the live run tick for tick. "
                       "Do this first.",
        "parameters": {"type": "object", "properties": {
            "times": {"type": "integer", "minimum": 2, "maximum": 5}, "why": _why()},
            "required": ["why"]}}},
    {"type": "function", "function": {
        "name": "isolate_cause",
        "description": "Delta debugging over the recorded inputs (jobs, chaos events): finds the smallest set of "
                       "events that still causes the failure. Tells you which events matter.",
        "parameters": {"type": "object", "properties": {"why": _why()}, "required": ["why"]}}},
    {"type": "function", "function": {
        "name": "what_if",
        "description": "Counterfactual on the exact incident: replay it with extra policy rules in force and "
                       "report whether the failure still happens, when, and whether anything new breaks. "
                       "One simulation; use it to test a hypothesis about the cause.",
        "parameters": {"type": "object", "properties": {
            "rules": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3,
                      "description": "DSL rules, e.g. [\"speed_cap(racks, 0.6)\"]"},
            "why": _why()}, "required": ["rules", "why"]}}},
    {"type": "function", "function": {
        "name": "stress_test",
        "description": "Test one fix against N variants of the incident (the hazard at other gaps, times, "
                       "aisles, robots; new pick timings) versus today's policy. Returns robustness (share of "
                       "variants that stay safe), how many failures it prevents or introduces, and the "
                       f"throughput cost. A fix needs robustness >= {MIN_ROBUSTNESS} to pass the gate.",
        "parameters": {"type": "object", "properties": {
            "fix": {"type": "string", "description": "One DSL rule"},
            "variants": {"type": "integer", "minimum": 10, "maximum": 60},
            "why": _why()}, "required": ["fix", "why"]}}},
    {"type": "function", "function": {
        "name": "tune_fix",
        "description": "Stress-test one numeric fix (speed_cap or min_clearance) at a range of settings and "
                       "return the safety/throughput curve and the cheapest setting that passes the gate.",
        "parameters": {"type": "object", "properties": {
            "fix": {"type": "string", "description": "The fix at any setting, e.g. speed_cap(racks, 0.6)"},
            "values": {"type": "array", "items": {"type": "number"}, "maxItems": 10,
                       "description": "Settings to try (optional; sensible defaults otherwise)"},
            "why": _why()}, "required": ["fix", "why"]}}},
    {"type": "function", "function": {
        "name": "submit_findings",
        "description": "Finish the investigation. Rank up to 3 fixes, best first; each must be one DSL rule. "
                       "Cite experiment numbers as evidence. List fixes you tested and rejected, with why.",
        "parameters": {"type": "object", "properties": {
            "root_cause": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "fixes": {"type": "array", "maxItems": MAX_FIXES, "items": {"type": "object", "properties": {
                "fix": {"type": "string"}, "why": {"type": "string"}}, "required": ["fix", "why"]}},
            "rejected": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
                "fix": {"type": "string"}, "reason": {"type": "string"}}, "required": ["fix", "reason"]}},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
            "required": ["root_cause", "evidence", "fixes", "confidence"]}}},
]

SYSTEM_PROMPT = f"""You are the incident investigator for a warehouse robot fleet. A failure was recorded as a
deterministic capsule (full state + every input). You investigate it only through simulation tools; you
never touch the live fleet. Work like a careful reliability engineer, and be efficient:

1. reproduce: confirm the failure replays deterministically (identical hashes, matches live).
2. isolate_cause: find which recorded events actually cause it.
3. Form a hypothesis about the mechanism. Use what_if (1 simulation) to test counterfactuals on the
   exact incident, e.g. "would a lower speed have let it stop?".
4. stress_test candidate fixes on variants of the incident; a fix that only fixes this one
   incident is not good enough. Compare robustness AND throughput cost.
5. tune_fix a numeric fix to find the cheapest setting that keeps >= {MIN_ROBUSTNESS:.0%} of variants safe.
6. submit_findings: root cause, evidence (cite the numbers), up to {MAX_FIXES} fixes ranked best first,
   and the fixes you rejected with the reason.

Budget: {MAX_STEPS} tool calls and {MAX_SIMS} simulations. Every call needs a short `why`.

Experiments run against recorded_policy: the rules the fleet ran when it failed. current_policy is what
it runs now (it may have changed since). Do not propose a rule that is already in current_policy; if
current_policy already contains what fixes this incident, check it with what_if and say so.

Fixes are single rules in this DSL:
  speed_cap(<zone>, <m/s 0.1-1.2>)        max speed in a zone
  min_clearance(<m 0.05-1.0>)             distance kept to obstacles already sensed
  reroute_avoid(<zone> | c<x>_<y>)        planner avoids these cells when it can
  reorder_steps(<single|multi|*>, <nearest_first|farthest_first|as_given>)
  require_scan_confirm(<item class> | *)  scan the bin before picking; a mismatch becomes an exception
  respect_closures(<zone> | *)            re-plan when a zone closes; never drive into a closed zone
Zones: {', '.join(sorted(W.zones))}. "racks" = every rack aisle. Item classes: boxed (racks A-B),
loose_small (C-D), fragile (E), heavy (F).
Physics: robots drive up to 1.2 m/s, brake at 1 m/s^2 (stopping distance ~ v^2/2 m) and sense
{SENSOR_RANGE / 1000} m ahead. A pallet that falls closer than the stopping distance cannot be avoided
by braking; clearance rules only act on obstacles already sensed.
Prefer the fix that generalises (the widest zone where the same hazard exists, a targeted item
class over *) at the lowest throughput cost. If `business` is given, put the trade-off in money:
the estimated cost of one such incident vs what a fix's throughput cost means in orders per hour, and
say it in the findings. Always call a tool; finish with submit_findings."""


def _compact(tool: str, res: dict) -> dict:
    """What the model sees of a result: the numbers, not the per-variant tiles."""
    keep = {
        "reproduce": ("summary", "identical", "matches_live", "reproduced", "hash"),
        "isolate_cause": ("summary", "minimal", "tested", "total", "ok"),
        "what_if": ("summary", "outcome", "first_failure", "new_failures", "warnings"),
        "stress_test": ("summary", "fix", "robustness", "baseline_robustness", "prevented", "still_fails",
                        "introduced", "clean", "not_placed", "cost_pct", "jobs_base", "jobs_fix", "passes"),
        "tune_fix": ("summary", "best", "baseline_robustness"),
    }[tool]
    out = {k: res[k] for k in keep if k in res}
    if tool == "tune_fix":
        out["points"] = [{k: p[k] for k in ("fix", "robustness", "cost_pct", "prevented", "still_fails", "introduced")}
                         for p in res.get("points", [])]
    if tool == "what_if":
        out["new_failures"] = [f"{f['type']} {f['robot']} t{f['tick']}" for f in out.get("new_failures", [])]
        out["warnings"] = [f"{f['type']} {f['robot']} t{f['tick']}" for f in out.get("warnings", [])]
    if tool == "isolate_cause":
        out["minimal"] = [m["input"] for m in out.get("minimal", [])]
    out["experiment"] = res.get("experiment")
    return out


# ---------------------------------------------------------------- the investigation

class Investigation:
    def __init__(self, settings: Settings, lab: Lab, cid: int, ctx: dict, rec: Recorder,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings, self.lab, self.cid, self.ctx, self.rec = settings, lab, cid, ctx, rec
        self.transport = transport
        self.n = 0
        self.sims = 0
        self.evidence: dict[str, dict] = {}    # fix -> stress numbers (from stress_test or tune_fix)
        self.experiments: list[int] = []
        self.source = "scripted"
        self.tokens = {"in": 0, "out": 0}
        self.notes: list[str] = []

    # -- running tools --------------------------------------------------------

    def budget_left(self) -> dict:
        return {"steps": MAX_STEPS - self.n, "simulations": max(0, MAX_SIMS - self.sims)}

    async def call(self, tool: str, args: dict) -> dict:
        """Run one tool (not submit_findings), record it, return what the model should see."""
        self.n += 1
        n, why = self.n, str(args.get("why") or "")[:300]
        clean = {k: v for k, v in args.items() if k != "why"}
        await self.rec.step(n, tool, clean, why)
        try:
            res = await self._dispatch(tool, clean)
        except (PolicyError, ValueError, KeyError, TypeError) as exc:
            await self.rec.step_done(n, "error", str(exc)[:300], None)
            return {"error": str(exc)[:300], "budget_left": self.budget_left()}
        self.sims += int(res.get("sims") or 0) + int(res.get("baseline_sims") or 0)
        eid = res.get("experiment")
        if eid:
            self.experiments.append(eid)
        await self.rec.step_done(n, "done", res.get("summary", ""), eid)
        return {**_compact(tool, res), "budget_left": self.budget_left()}

    async def _dispatch(self, tool: str, a: dict) -> dict:
        inv, cid = self.rec.inv, self.cid
        if tool == "reproduce":
            return await self.lab.reproduce(cid, int(a.get("times") or 3), inv=inv)
        if tool == "isolate_cause":
            return await self.lab.isolate(cid, inv=inv)
        if tool == "what_if":
            rules = a.get("rules")
            if not isinstance(rules, list) or not rules:
                raise ValueError("rules must be a non-empty list of DSL rules")
            return await self.lab.what_if(cid, [str(r) for r in rules][:3], inv=inv)
        if tool == "stress_test":
            fix = normalize([str(a.get("fix") or "")])[0]
            if self.sims + int(a.get("variants") or 30) * 2 > MAX_SIMS:
                raise ValueError("simulation budget exhausted: submit your findings")
            res = await self.lab.stress(cid, fix, a.get("variants"), inv=inv)
            self.evidence[fix] = self._numbers(res)
            return res
        if tool == "tune_fix":
            fix = str(a.get("fix") or "")
            values = a.get("values") or None
            if values is not None:
                values = [float(v) for v in values][:10]
            if self.sims + 30 * (len(values or xp.sweep(fix)) + 1) > MAX_SIMS:
                raise ValueError("simulation budget exhausted: submit your findings")
            res = await self.lab.tune(cid, fix, values, inv=inv)
            for p in res.get("points", []):
                self.evidence.setdefault(p["fix"], self._numbers({**p, "experiment": res.get("experiment")}))
            return res
        raise ValueError(f"unknown tool {tool}")

    @staticmethod
    def _numbers(res: dict) -> dict:
        return {k: res.get(k) for k in ("robustness", "baseline_robustness", "prevented", "still_fails",
                                        "introduced", "placed", "cost_pct", "jobs_base", "jobs_fix", "experiment")}

    # -- finishing ------------------------------------------------------------

    async def finish(self, raw: dict) -> dict:
        """Validate the findings; make sure every recommended fix has stress-test evidence."""
        fixes, rejected = [], []
        for item in (raw.get("fixes") or [])[:MAX_FIXES + 2]:
            if not isinstance(item, dict):
                continue
            try:
                rule = normalize([str(item.get("fix") or "")])[0]
            except PolicyError as exc:
                rejected.append({"fix": str(item.get("fix"))[:120], "reason": f"not a valid rule: {exc}"})
                continue
            if rule in self.ctx["current_policy"] or any(f["fix"] == rule for f in fixes):
                continue
            fixes.append({"fix": rule, "why": str(item.get("why") or "")[:400]})
        fixes = fixes[:MAX_FIXES]
        for f in fixes:  # the gate needs robustness numbers for every fix it is asked to approve
            if f["fix"] not in self.evidence:
                await self.call("stress_test", {"fix": f["fix"], "why": "gate: every proposed fix is stress-tested"})
            f["stress"] = self.evidence.get(f["fix"])
        for r in raw.get("rejected") or []:
            if isinstance(r, dict) and r.get("fix"):
                rejected.append({"fix": str(r["fix"])[:120], "reason": str(r.get("reason") or "")[:300],
                                 "stress": self.evidence.get(str(r["fix"]))})
        self.n += 1
        report = {
            "root_cause": str(raw.get("root_cause") or "")[:1200],
            "evidence": [str(e)[:400] for e in (raw.get("evidence") or [])][:8],
            "fixes": fixes, "rejected": rejected[:6],
            "confidence": raw.get("confidence") if raw.get("confidence") in ("low", "medium", "high") else "medium",
            "source": self.source, "steps": self.n, "simulations": self.sims, "experiments": self.experiments,
            "tokens": self.tokens, "notes": self.notes, "min_robustness": MIN_ROBUSTNESS,
        }
        await self.rec.step(self.n, "submit_findings", {"root_cause": report["root_cause"],
                                                        "fixes": [f["fix"] for f in fixes],
                                                        "confidence": report["confidence"]}, "write up the findings")
        await self.rec.step_done(self.n, "done", f"{len(fixes)} fix(es) proposed: "
                                 + (", ".join(f["fix"] for f in fixes) or "none"), None)
        return report

    async def run(self) -> dict:
        if self.settings.inference_enabled:
            try:
                return await self._llm()
            except (InferenceError, httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as exc:
                log.warning("investigator model failed, continuing scripted: %s", exc)
                self.notes.append(f"model failed ({str(exc)[:160]}); scripted investigator took over")
                await self.rec.note(self.notes[-1])
                self.source += " → scripted"
        return await self._scripted()

    # -- the model drives ---------------------------------------------------

    async def _llm(self) -> dict:
        s = self.settings
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {s.inference_key}"}, timeout=120.0,
                                     transport=self.transport) as http:
            model = await _pick_model(http, s)
            self.source = f"{s.inference_provider}:{model}"
            # OpenAI's reasoning models only take function tools on the Responses API;
            # other OpenAI-compatible endpoints (Vultr Serverless Inference) speak Chat Completions.
            convo: Conversation = (Responses if s.inference_provider == "openai" else Chat)(
                http, s, model, "Investigate this incident.\n" + json.dumps(self.ctx), self)
            nudges = turns = 0
            while True:
                turns += 1
                if turns > MAX_STEPS + 6:
                    raise InferenceError("the model did not submit findings within its budget")
                force = self.n >= MAX_STEPS or self.sims >= MAX_SIMS
                text, calls = await convo.ask(force)
                if text:
                    await self.rec.note(text[:600])
                if not calls:
                    nudges += 1
                    if nudges > 2:
                        raise InferenceError("the model stopped calling tools")
                    convo.say("Call one of the tools. When you are done, call submit_findings.")
                    continue
                outputs = []
                for cid, name, raw in calls:
                    try:
                        args = json.loads(raw or "{}")
                    except json.JSONDecodeError:
                        args = None
                    if not isinstance(args, dict):
                        out: dict[str, Any] = {"error": "arguments must be a JSON object"}
                    elif name == "submit_findings":
                        return await self.finish(args)
                    elif force:
                        out = {"error": "budget exhausted: call submit_findings now"}
                    else:
                        out = await self.call(name, args)
                    outputs.append((cid, json.dumps(out)))
                convo.results(outputs)

    async def post(self, http: httpx.AsyncClient, url: str, body: dict, model: str) -> dict:
        r = await http.post(url, json=body)
        for _ in range(3):  # a model may reject a parameter; drop it and resend
            if r.status_code != 400 or not _adapt(body, r.text):
                break
            self.notes.append(f"adjusted request for {model}: {r.text[:120]}")
            r = await http.post(url, json=body)
        if r.status_code >= 400:
            raise InferenceError(f"inference HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    # -- the scripted investigator -------------------------------------------

    async def _scripted(self) -> dict:
        f = self.ctx["failure"]
        cands = [h["fix"] for h in diagnosis.playbook(self.ctx)]
        rep = await self.call("reproduce", {"times": 3, "why": "confirm the failure replays deterministically"})
        iso = await self.call("isolate_cause", {"why": "find which recorded events actually cause it"})
        if cands:
            await self.call("what_if", {"rules": [cands[0]], "why": f"would {cands[0]} have prevented this incident?"})
        tested: dict[str, dict] = {}
        for fix in cands[:3]:
            out = await self.call("stress_test", {"fix": fix, "variants": 30,
                                                  "why": f"does {fix} hold up across variants of the incident?"})
            if "error" not in out:
                tested[fix] = out
        tunable = next((fx for fx in cands if xp.setting(fx) is not None and (tested.get(fx) or {}).get("prevented")),
                       None)
        if tunable:
            t = await self.call("tune_fix", {"fix": tunable, "why": "find the cheapest setting that stays safe"})
            if t.get("best"):
                tested[t["best"]] = {**self.evidence.get(t["best"], {}), "fix": t["best"]}
        passing = sorted((fx for fx, r in tested.items() if (r.get("robustness") or 0) >= MIN_ROBUSTNESS),
                         key=lambda fx: tested[fx].get("cost_pct") or 0)
        failing = [fx for fx in tested if fx not in passing]
        def pct(x: Any) -> str:
            return "n/a" if x is None else f"{x * 100:.0f}%"
        evidence = [rep.get("summary", ""), iso.get("summary", "")]
        evidence += [f"{fx}: {pct(tested[fx].get('robustness'))} of variants safe, throughput cost "
                     f"{tested[fx].get('cost_pct')}%" for fx in tested]
        cause = next((h["cause"] for h in diagnosis.playbook(self.ctx)), f"{f['type']} of {f['robot']}")
        return await self.finish({
            "root_cause": cause,
            "evidence": [e for e in evidence if e],
            "fixes": [{"fix": fx, "why": f"{pct(tested[fx].get('robustness'))} of variants safe at "
                                         f"{tested[fx].get('cost_pct')}% throughput cost"} for fx in passing[:MAX_FIXES]]
                     or [{"fix": fx, "why": "best available, below the robustness bar"} for fx in failing[:1]],
            "rejected": [{"fix": fx, "reason": f"only {pct(tested[fx].get('robustness'))} of variants safe"}
                         for fx in failing],
            "confidence": "high" if passing else "low",
        })


def _adapt(body: dict, error: str) -> bool:
    """Drop or rename a parameter the server rejected. True if something changed."""
    if "max_tokens" in error and "max_tokens" in body:
        body["max_completion_tokens"] = max(body.pop("max_tokens"), 4000)
        return True
    for param in ("parallel_tool_calls", "temperature", "reasoning", "tool_choice"):
        if param in error and param in body:
            del body[param]
            return True
    return False


Call = tuple[str, str, str]   # (call id, tool name, JSON arguments)


class Conversation(Protocol):
    async def ask(self, force: bool) -> tuple[str, list[Call]]: ...
    def results(self, outputs: list[tuple[str, str]]) -> None: ...
    def say(self, text: str) -> None: ...


class Chat:
    """Chat Completions with function tools (any OpenAI-compatible endpoint)."""

    def __init__(self, http: httpx.AsyncClient, s: Settings, model: str, task: str, inv: Investigation) -> None:
        self.http, self.s, self.model, self.inv = http, s, model, inv
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]

    async def ask(self, force: bool) -> tuple[str, list[Call]]:
        body: dict[str, Any] = {"model": self.model, "messages": self.messages, "tools": TOOLS,
                                "tool_choice": {"type": "function", "function": {"name": "submit_findings"}}
                                if force else "required", "parallel_tool_calls": False}
        if self.s.inference_provider == "openai":
            body["max_completion_tokens"] = 6000
        else:
            body["max_tokens"] = 1500
            body["temperature"] = 0.2
        data = await self.inv.post(self.http, f"{self.s.inference_url}/chat/completions", body, self.model)
        usage = data.get("usage") or {}
        self.inv.tokens["in"] += int(usage.get("prompt_tokens") or 0)
        self.inv.tokens["out"] += int(usage.get("completion_tokens") or 0)
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        self.messages.append({"role": "assistant", "content": msg.get("content") or "",
                              **({"tool_calls": calls} if calls else {})})
        return str(msg.get("content") or ""), [(c["id"], c["function"]["name"], c["function"].get("arguments"))
                                               for c in calls]

    def results(self, outputs: list[tuple[str, str]]) -> None:
        self.messages += [{"role": "tool", "tool_call_id": cid, "content": out} for cid, out in outputs]

    def say(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


RESPONSES_TOOLS = [{"type": "function", **t["function"]} for t in TOOLS]


class Responses:
    """OpenAI Responses API: reasoning models with function tools. The conversation is kept
    server-side and continued with previous_response_id, so reasoning carries across turns."""

    def __init__(self, http: httpx.AsyncClient, s: Settings, model: str, task: str, inv: Investigation) -> None:
        self.http, self.s, self.model, self.inv = http, s, model, inv
        self.prev: str | None = None
        self.pending: list[dict] = [{"role": "user", "content": task}]

    async def ask(self, force: bool) -> tuple[str, list[Call]]:
        body: dict[str, Any] = {"model": self.model, "instructions": SYSTEM_PROMPT, "input": self.pending,
                                "tools": RESPONSES_TOOLS, "parallel_tool_calls": False,
                                "tool_choice": {"type": "function", "name": "submit_findings"} if force else "required",
                                "max_output_tokens": 8000, "reasoning": {"effort": "low"}}
        if self.prev:
            body["previous_response_id"] = self.prev
        data = await self.inv.post(self.http, f"{self.s.inference_url}/responses", body, self.model)
        usage = data.get("usage") or {}
        self.inv.tokens["in"] += int(usage.get("input_tokens") or 0)
        self.inv.tokens["out"] += int(usage.get("output_tokens") or 0)
        self.prev, self.pending = data["id"], []
        text, calls = [], []
        for item in data.get("output") or []:
            if item.get("type") == "function_call":
                calls.append((item["call_id"], item["name"], item.get("arguments")))
            elif item.get("type") == "message":
                text += [c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text"]
        return " ".join(t for t in text if t), calls

    def results(self, outputs: list[tuple[str, str]]) -> None:
        self.pending += [{"type": "function_call_output", "call_id": cid, "output": out} for cid, out in outputs]

    def say(self, text: str) -> None:
        self.pending.append({"role": "user", "content": text})


# ---------------------------------------------------------------- persistence (Postgres)

class DbRecorder:
    """Writes the trace to investigation_steps and streams it to the browser."""

    def __init__(self, rt, inv: int, fid: int, cid: int) -> None:
        self.rt, self.inv, self.fid, self.cid = rt, inv, fid, cid
        self.t0 = time.monotonic()

    def _publish(self, **data: Any) -> None:
        self.rt.hub.publish("investigation", {"id": self.inv, "failure_id": self.fid, "capsule_id": self.cid,
                                              **data})

    async def step(self, n: int, tool: str, args: dict, why: str) -> None:
        async with self.rt.pool.acquire() as c:
            await c.execute("INSERT INTO investigation_steps (investigation_id, n, tool, args, why) "
                            "VALUES ($1, $2, $3, $4, $5)", self.inv, n, tool, args, why)
        self._publish(step={"n": n, "tool": tool, "args": args, "why": why, "status": "running"})

    async def step_done(self, n: int, status: str, summary: str, experiment: int | None) -> None:
        async with self.rt.pool.acquire() as c, c.transaction():
            row = await c.fetchrow("UPDATE investigation_steps SET status = $3, summary = $4, experiment_id = $5, "
                                   "finished_at = now() WHERE investigation_id = $1 AND n = $2 "
                                   "RETURNING tool, args, why", self.inv, n, status, summary, experiment)
            await log_event(c, "investigation.step", {"investigation": self.inv, "failure": self.fid, "n": n,
                                                      "tool": row["tool"] if row else None, "status": status,
                                                      "why": row["why"] if row else None, "summary": summary,
                                                      "experiment": experiment})
        self._publish(step={"n": n, "status": status, "summary": summary, "experiment": experiment})

    async def note(self, text: str) -> None:
        self._publish(note=text)


__all__ = ["DbRecorder", "Investigation", "MAX_SIMS", "MAX_STEPS", "TOOLS"]
