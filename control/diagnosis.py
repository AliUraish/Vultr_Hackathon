"""Diagnosis agent: a reproduced capsule in, up to three ranked fix hypotheses out.

Two engines, same contract:
  - playbook (default, free): rules per failure type that read the capsule's
    evidence (speed, zone, the chaos input, the bin involved) and fill in the
    parameters of the matching DSL fixes.
  - an LLM (only if INFERENCE_KEY is set): OpenAI, or Vultr Serverless
    Inference, or any OpenAI-compatible endpoint. It must answer in strict JSON;
    answers are validated against the DSL and retried once with the errors.
Either way nothing is trusted until forked replays prove it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from replay_core.policy import FIX_TYPES, JOB_KINDS, STRATEGIES, PolicyError, parse_rule
from replay_core.world import ACCEL, CELL, ITEM_CLASSES, SENSOR_RANGE, TICK_HZ, W

from .config import Settings

log = logging.getLogger("replay.diagnosis")

MAX_HYPOTHESES = 3
_KEEP = ("robot", "with", "v", "cell", "slot", "sku", "expected", "actual", "ok", "job", "zone",
         "dock", "reason", "id", "type", "scenario", "until", "version")


def _mps(v_mm_per_tick: int) -> float:
    return v_mm_per_tick * TICK_HZ / 1000


def _stopping_m(v_mm_per_tick: int) -> float:
    d, v = 0, v_mm_per_tick
    while v > 0:
        v = max(v - ACCEL, 0)
        d += v
    return d / 1000


def context(capsule: dict, failure: dict, events: list[dict], policy_rules: list[str]) -> dict:
    """Everything the diagnosis sees, taken from the capsule and the event log."""
    target = capsule["failure"]
    frames = capsule["live_frames"]
    at = next((f for f in frames if f["t"] == target["tick"]), frames[-1])
    robot = next(r for r in at["robots"] if r["id"] == target["robot"])
    cell = [robot["x"] // CELL, robot["y"] // CELL]
    recent = [f for f in frames if target["tick"] - 30 <= f["t"] <= target["tick"]]
    speeds = [_mps(next(r["v"] for r in f["robots"] if r["id"] == target["robot"])) for f in recent]
    world_events = [
        {"tick": t, **{k: v for k, v in i.items() if k in ("type", "scenario", "cell", "slot", "sku", "zone", "ticks")},
         **({"slot_item_class": W.slots[i["slot"]]["cls"]} if i.get("slot") in W.slots else {})}
        for t, ins in capsule["inputs"] for i in ins if i.get("kind") == "chaos"
    ]
    return {
        "failure": {"type": target["type"], "robot": target["robot"], "tick": target["tick"],
                    "detail": failure.get("detail", {})},
        "robot_at_failure": {
            "cell": f"c{cell[0]}_{cell[1]}", "zones": list(W.cell_zones.get((cell[0], cell[1]), ())),
            "speed_mps": _mps(robot["v"]), "status": robot["st"], "job": robot["job"],
            "carrying": robot["carry"],
        },
        "max_speed_last_3s_mps": max(speeds) if speeds else 0.0,
        "world_events_in_capsule": world_events,
        "failures_in_capsule": [{k: f[k] for k in ("type", "robot", "tick")} for f in capsule["baseline_failures"]],
        "recent_events": [
            {"tick": e.get("tick"), "type": e["type"],
             **{k: v for k, v in (e.get("payload") or {}).items() if k in _KEEP}}
            for e in events
        ],
        "current_policy": policy_rules,
        "capsule": {"start_tick": capsule["start"], "end_tick": capsule["end"], "hash": capsule["hash"]},
    }


# ---------------------------------------------------------------- playbook

def _cap_zone(zones: list[str]) -> str | None:
    if "racks" in zones:
        return "racks"
    return next((z for z in zones if z.startswith("aisle_")), zones[0] if zones else None)


def playbook(ctx: dict) -> list[dict]:
    f, r = ctx["failure"], ctx["robot_at_failure"]
    typ, rid, zones = f["type"], f["robot"], r["zones"]
    detail = f["detail"]
    chaos = ctx["world_events_in_capsule"]
    out: list[tuple[str, str, str]] = []

    if typ == "collision":
        v = max(ctx["max_speed_last_3s_mps"], r["speed_mps"])
        v_tick = int(v * 1000 / TICK_HZ)
        zone = _cap_zone(zones)
        pallet = next((e["cell"] for e in chaos if e.get("type") == "spawn_pallet"), None)
        if zone:
            out.append((
                f"{rid} was doing {v:.1f} m/s in {zone}. At that speed it needs {_stopping_m(v_tick):.2f} m to "
                f"stop but senses only {SENSOR_RANGE / 1000:.1f} m ahead, so it could not brake for an obstacle "
                "that appeared close in front of it.",
                f"speed_cap({zone}, 0.5)",
                "Slow robots where obstacles can appear suddenly; at 0.5 m/s the stopping distance is about 0.1 m.",
            ))
        out.append((
            f"{rid} keeps too little distance to obstacles it has already sensed.",
            "min_clearance(0.4)",
            "Brake earlier for sensed obstacles.",
        ))
        if pallet:
            out.append((
                f"Traffic is routed through c{pallet[0]}_{pallet[1]}, where the pallet fell.",
                f"reroute_avoid(c{pallet[0]}_{pallet[1]})",
                "Keep robots out of that cell.",
            ))
    elif typ == "wrong_item":
        slot = next((e["slot"] for e in chaos if e.get("type") == "mislabel"), None)
        if slot is None:
            missing = sorted(set(detail.get("expected", [])) - set(detail.get("actual", [])))
            slot = missing[0].removeprefix("SKU-") if missing else None
        cls = W.slots[slot]["cls"] if slot in W.slots else None
        if cls:
            out.append((
                f"Bin {slot} held the wrong item and {rid} picked it without checking: {cls} items are "
                "picked without a scan, so a mislabeled bin goes unnoticed until the dock.",
                f"require_scan_confirm({cls})",
                f"Scan {cls} bins before picking; a mismatch becomes an exception for a person.",
            ))
        out.append((
            "Any bin can be mislabeled; robots never confirm what they pick.",
            "require_scan_confirm(*)",
            "Scan every pick. Safest, but slower.",
        ))
        out.append((
            "The pick order sent the robot to a neighbouring look-alike bin.",
            "reorder_steps(*, nearest_first)",
            "Pick in nearest-first order.",
        ))
    elif typ == "zone_breach":
        zone = detail.get("zone")
        if zone:
            out.append((
                f"{rid} was already routed through {zone} when it was closed, and robots do not re-plan "
                "an existing route when an aisle closes.",
                f"reroute_avoid({zone})",
                f"Route around {zone} whenever there is another way.",
            ))
            out.append((
                f"{rid} entered {zone} too fast to stop at the boundary.",
                f"speed_cap({zone}, 0.3)",
                "Enter that aisle slowly.",
            ))
        out.append((
            f"{rid} took a path through the rack aisles instead of the main floor.",
            "reroute_avoid(racks)",
            "Prefer the open floor to rack aisles.",
        ))
    elif typ in ("stall", "task_overdue"):
        zone = next((z for z in zones if z.startswith("aisle_")), None)
        if zone:
            out.append((
                f"{rid} got stuck in {zone}, a one-robot-wide aisle where robots block each other.",
                f"reroute_avoid({zone})",
                f"Keep through-traffic out of {zone}.",
            ))
        out.append((
            f"{rid} got stuck at {r['cell']}.",
            f"reroute_avoid({r['cell']})",
            "Route around that cell.",
        ))
        out.append((
            "Multi-pick jobs send robots back and forth through busy aisles.",
            "reorder_steps(multi, nearest_first)",
            "Pick the nearest line first.",
        ))

    hyps, _ = validate({"hypotheses": [{"cause": c, "fix": x, "rationale": why} for c, x, why in out]},
                       ctx["current_policy"])
    return hyps


# ---------------------------------------------------------------- validation

def validate(raw: Any, policy_rules: list[str]) -> tuple[list[dict], list[str]]:
    """Keep only well-formed hypotheses whose fix parses in the DSL and is not already deployed."""
    hyps = raw.get("hypotheses") if isinstance(raw, dict) else None
    if not isinstance(hyps, list) or not hyps:
        return [], ["top-level object must have a non-empty 'hypotheses' list"]
    out: list[dict] = []
    errors: list[str] = []
    for i, h in enumerate(hyps):
        if len(out) == MAX_HYPOTHESES:
            break
        if not isinstance(h, dict) or not isinstance(h.get("cause"), str) or not isinstance(h.get("fix"), str):
            errors.append(f"hypothesis {i}: needs string fields 'cause' and 'fix'")
            continue
        try:
            rule = str(parse_rule(h["fix"]))
        except PolicyError as exc:
            errors.append(f"hypothesis {i}: {exc}")
            continue
        if rule in policy_rules or any(o["fix"] == rule for o in out):
            errors.append(f"hypothesis {i}: {rule} is already in the policy or repeated")
            continue
        out.append({"cause": h["cause"][:600], "fix": rule, "rationale": str(h.get("rationale", ""))[:400]})
    return out, errors


def parse_json(text: str) -> Any:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the answer")
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------- LLM (OpenAI-compatible)

SYSTEM_PROMPT = f"""You diagnose warehouse robot fleet failures from a deterministic replay capsule.
Propose exactly {MAX_HYPOTHESES} distinct hypotheses for the root cause, most likely first; each will be
tested in its own forked replay, so make them genuinely different. Each hypothesis carries exactly
one fix, written as one rule in this DSL and nothing else:
  speed_cap(<zone>, <m/s 0.1-1.2>)
  min_clearance(<metres 0.05-1.0>)
  reroute_avoid(<zone> | c<x>_<y>)
  reorder_steps(<{'|'.join(JOB_KINDS)}>, <{'|'.join(STRATEGIES)}>)
  require_scan_confirm(<item class> | *)
Zones: {', '.join(sorted(W.zones))}. "racks" is every rack aisle; aisle_<row><W|E> is one aisle.
Item classes: {', '.join(ITEM_CLASSES)}. Racks A-B hold boxed, C-D loose_small, E fragile, F heavy
items; a slot id is rack letter + number (e.g. D8).
Rank first the fix that would also prevent similar failures elsewhere at the least cost to throughput:
a zone rule over a single cell, a targeted item class over *, unless the evidence says otherwise.
Robots move up to 1.2 m/s, brake at 1 m/s^2 and sense obstacles {SENSOR_RANGE / 1000} m ahead.
Do not repeat a rule that is already in current_policy. Ground every cause in the capsule evidence.
Answer with only this JSON, no prose:
{{"hypotheses": [{{"cause": "...", "fix": "<one DSL rule>", "rationale": "..."}}]}}"""


class InferenceError(RuntimeError):
    pass


# Small, cheap chat models that follow a JSON schema well, in order of preference.
OPENAI_PREFERRED = ("gpt-4.1-mini", "gpt-4o-mini", "gpt-5-mini", "gpt-4.1", "gpt-4o")
_NOT_CHAT = ("embed", "whisper", "tts", "audio", "realtime", "transcribe", "image", "dall-e", "moderation",
             "search", "babbage", "davinci")




async def _pick_model(http: httpx.AsyncClient, settings: Settings) -> str:
    if settings.inference_model:
        return settings.inference_model
    r = await http.get(f"{settings.inference_url}/models")
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    chat = [i for i in ids if not any(x in i.lower() for x in _NOT_CHAT)]
    if settings.inference_provider == "openai":
        for pref in OPENAI_PREFERRED:
            if pref in chat:
                return pref
        # OpenAI's "-instruct" models are completions-only; elsewhere "-instruct" means a chat model.
        chat = [i for i in chat if i.startswith("gpt-") and "-instruct" not in i] or chat
    for pref in ("instruct", "llama", "qwen", "mistral"):
        match = next((i for i in chat if pref in i.lower()), None)
        if match:
            return match
    if not chat:
        raise InferenceError("no chat models available")
    return chat[0]


def _request(settings: Settings, model: str, messages: list[dict]) -> dict:
    body: dict[str, Any] = {"model": model, "messages": messages}
    if settings.inference_provider == "openai":
        # Works for every current OpenAI chat model, reasoning ones included (they reject
        # max_tokens and any non-default temperature). The budget covers hidden reasoning tokens.
        body["max_completion_tokens"] = 4000
        body["response_format"] = {"type": "json_object"}
    else:
        body["temperature"] = 0.2
        body["max_tokens"] = 900
    return body


def _adapt(body: dict, error: str) -> bool:
    """Fix a request the server rejected for an unsupported parameter. True if something changed."""
    if "max_tokens" in error and "max_tokens" in body:
        body["max_completion_tokens"] = max(body.pop("max_tokens"), 4000)
        return True
    for param in ("temperature", "response_format"):
        if param in error and param in body:
            del body[param]
            return True
    return False


async def _llm(ctx: dict, settings: Settings,
               transport: httpx.AsyncBaseTransport | None = None) -> tuple[list[dict], str, list[str]]:
    headers = {"Authorization": f"Bearer {settings.inference_key}"}
    notes: list[str] = []
    async with httpx.AsyncClient(headers=headers, timeout=60.0, transport=transport) as http:
        model = await _pick_model(http, settings)
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(ctx)}]
        best: list[dict] = []
        for attempt in (1, 2):
            body = _request(settings, model, messages)
            r = await http.post(f"{settings.inference_url}/chat/completions", json=body)
            for _ in range(2):  # a newer model may reject a parameter; adjust and resend
                if r.status_code != 400 or not _adapt(body, r.text):
                    break
                notes.append(f"adjusted request for {model}: {r.text[:120]}")
                r = await http.post(f"{settings.inference_url}/chat/completions", json=body)
            if r.status_code >= 400:
                raise InferenceError(f"inference HTTP {r.status_code}: {r.text[:200]}")
            answer = r.json()["choices"][0]["message"]["content"] or ""
            try:
                hyps, errors = validate(parse_json(answer), ctx["current_policy"])
            except (ValueError, json.JSONDecodeError) as exc:
                hyps, errors = [], [f"not valid JSON: {exc}"]
            if len(hyps) > len(best):
                best = hyps
            if not errors:
                break
            notes.append(f"attempt {attempt}: {'; '.join(errors)}")
            messages += [{"role": "assistant", "content": answer},
                         {"role": "user", "content": "Your answer had problems: " + "; ".join(errors)
                          + ". Reply again with only the corrected JSON."}]
        if not best:
            raise InferenceError("no valid hypotheses after retry")
        return best, f"{settings.inference_provider}:{model}", notes


async def diagnose(settings: Settings, ctx: dict,
                   transport: httpx.AsyncBaseTransport | None = None) -> tuple[list[dict], str, list[str]]:
    """(hypotheses, source, notes). Falls back to the playbook if inference is off or fails."""
    if settings.inference_enabled:
        try:
            return await _llm(ctx, settings, transport)
        except (InferenceError, httpx.HTTPError, KeyError, IndexError) as exc:
            log.warning("inference failed, using playbook: %s", exc)
            return playbook(ctx), "playbook", [f"inference failed: {exc}"]
    return playbook(ctx), "playbook", []


__all__ = ["FIX_TYPES", "context", "diagnose", "playbook", "validate"]
