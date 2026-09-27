"""Diagnosis agent: a reproduced capsule in, up to three ranked fix hypotheses out.

Two engines, same contract:
  - playbook (default, free): rules per failure type that read the capsule's
    evidence (speed, bench, the hazard input, the dig face involved) and fill in the
    parameters of the matching DSL fixes.
  - Vultr Serverless Inference (when INFERENCE_KEY is set). It must answer in strict
    JSON; answers are validated against the DSL and retried once with the errors.
Either way nothing is trusted until forked replays prove it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from replay_core.policy import FIX_TYPES, PolicyError, parse_rule
from replay_core.world import ACCEL, CELL, ITEM_CLASSES, SENSOR_RANGE, TICK_HZ, W

from .config import Settings
from .usage import USAGE

log = logging.getLogger("replay.diagnosis")

MAX_HYPOTHESES = 3
_KEEP = ("robot", "with", "v", "cell", "slot", "sku", "expected", "actual", "ok", "job", "zone",
         "dock", "reason", "id", "type", "scenario", "until", "version", "depth")


def _kmh(v_mm_per_tick: int) -> float:
    return round(v_mm_per_tick * TICK_HZ * 3.6 / 1000, 1)


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
    speeds = [_kmh(next(r["v"] for r in f["robots"] if r["id"] == target["robot"])) for f in recent]
    world_events = [
        {"tick": t, **{k: v for k, v in i.items() if k in ("type", "scenario", "cell", "slot", "sku", "zone", "ticks")},
         **({"face_material": W.slots[i["slot"]]["cls"]} if i.get("slot") in W.slots else {}),
         **({"appeared_m_ahead_of_truck": round(i["gap_mm"] / 1000, 1), "truck": i.get("target"),
             "truck_speed_kmh": _kmh(i.get("v", 0))} if isinstance(i.get("gap_mm"), int) else {})}
        for t, ins in capsule["inputs"] for i in ins if i.get("kind") == "chaos"
    ]
    return {
        "failure": {"type": target["type"], "truck": target["robot"], "robot": target["robot"], "tick": target["tick"],
                    "detail": failure.get("detail", {})},
        "robot_at_failure": {
            "cell": f"c{cell[0]}_{cell[1]}", "zones": list(W.cell_zones.get((cell[0], cell[1]), ())),
            "speed_kmh": _kmh(robot["v"]), "status": robot["st"], "load_ticket": robot["job"],
            "carrying": robot["carry"],
        },
        "max_speed_last_3s_kmh": max(speeds) if speeds else 0.0,
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
    if "benches" in zones:
        return "benches"
    return next((z for z in zones if z.startswith("bench_") or z.startswith("ramp")), zones[0] if zones else None)


def playbook(ctx: dict) -> list[dict]:
    f, r = ctx["failure"], ctx["robot_at_failure"]
    typ, rid, zones = f["type"], f["robot"], r["zones"]
    detail = f["detail"]
    chaos = ctx["world_events_in_capsule"]
    out: list[tuple[str, str, str]] = []

    if typ == "collision":
        v = max(ctx["max_speed_last_3s_kmh"], r["speed_kmh"])
        v_tick = int(v * 1000 / 3.6 / TICK_HZ)
        zone = _cap_zone(zones)
        rock = next((e["cell"] for e in chaos if e.get("type") == "spawn_rock"), None)
        if zone:
            out.append((
                f"{rid} was doing {v:.0f} km/h on {zone}. At that speed a haul truck needs {_stopping_m(v_tick):.0f} m "
                f"to stop, and the rock came off the highwall closer than that, so no amount of braking could avoid it.",
                f"speed_cap({zone}, 20)",
                "Slow trucks where rocks can fall onto the road; at 20 km/h the stopping distance is about 8 m.",
            ))
        out.append((
            f"{rid} keeps too little distance to obstacles its lidar has already seen.",
            "min_clearance(6)",
            "Brake earlier for sensed obstacles.",
        ))
        if rock:
            out.append((
                f"Trucks are routed through c{rock[0]}_{rock[1]}, where the rock fell.",
                f"reroute_avoid(c{rock[0]}_{rock[1]})",
                "Keep trucks off that road segment.",
            ))
    elif typ == "wrong_item":
        slot = next((e["slot"] for e in chaos if e.get("type") == "mislabel"), None)
        if slot is None:
            missing = sorted(set(detail.get("expected", [])) - set(detail.get("actual", [])))
            slot = missing[0].removeprefix("MAT-") if missing else None
        cls = W.slots[slot]["cls"] if slot in W.slots else None
        if cls:
            out.append((
                f"Face {slot}'s grade tag was wrong and {rid} loaded without a grade check: {cls} faces are loaded on "
                "the block model alone, so a mixed-up dig block goes unnoticed until the dump.",
                f"grade_check({cls})",
                f"Check the grade at {cls} faces before loading; a mismatch becomes an exception for grade control.",
            ))
        out.append((
            "Any dig block can be mis-tagged; trucks never confirm what they load.",
            "grade_check(*)",
            "Check every load. Safest, but slower at every face.",
        ))
        out.append((
            f"{rid} was driving too fast on the benches to notice the change.",
            "speed_cap(benches, 20)",
            "Slow down on the benches.",
        ))
    elif typ == "zone_breach":
        zone = detail.get("zone")
        if zone:
            out.append((
                f"{rid} was already routed through {zone} when the blast crew closed it, and trucks do not "
                "re-check an existing route when a road closes.",
                f"respect_closures({'benches' if zone.startswith('bench_') else zone})",
                "Re-plan when a road closes for blasting and wait outside it; covers trucks passing through and "
                "trucks whose dig face is inside.",
            ))
            out.append((
                f"{rid}'s route went through {zone}; keeping traffic off it avoids the conflict.",
                f"reroute_avoid({zone})",
                f"Route around {zone} whenever there is another way (does not help trucks loading on it).",
            ))
            out.append((
                f"{rid} entered {zone} too fast to stop at the barricade.",
                f"speed_cap({zone}, 15)",
                "Enter that bench slowly.",
            ))
        out.append((
            f"{rid} took a bench road instead of the main haul roads.",
            "reroute_avoid(benches)",
            "Prefer the two-lane haul roads to the one-lane benches.",
        ))
    elif typ in ("stall", "task_overdue"):
        zone = next((z for z in zones if z.startswith("bench_") or z == "cuts"), None)
        if zone:
            out.append((
                f"{rid} got stuck on {zone}, a one-lane road where trucks block each other.",
                f"reroute_avoid({zone})",
                f"Keep through-traffic off {zone}.",
            ))
        out.append((
            f"{rid} got stuck at {r['cell']}.",
            f"reroute_avoid({r['cell']})",
            "Route around that segment.",
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


# ---------------------------------------------------------------- LLM (Vultr Serverless Inference)

SYSTEM_PROMPT = f"""You diagnose failures of an autonomous haul truck fleet in an open-pit mine from a deterministic
replay capsule. Propose exactly {MAX_HYPOTHESES} distinct hypotheses for the root cause, most likely first; each will be
tested in its own forked replay, so make them genuinely different. Each hypothesis carries exactly one fix, written as
one rule in this DSL and nothing else:
  speed_cap(<zone>, <km/h 5-45>)
  min_clearance(<metres 1-40>)
  reroute_avoid(<zone> | c<x>_<y>)
  grade_check(<material> | *)      check the material grade at the face before loading
  respect_closures(<zone> | *)     trucks re-plan when a road closes for blasting and never drive into a closed road
Zones: {', '.join(sorted(W.zones))}. "benches" is every one-lane bench road and cut under a highwall;
bench_<upper|lower>_<w|e> is one bench road. Materials: {', '.join(ITEM_CLASSES)} (faces A ore, B lowgrade, C waste, D sand).
Rank first the fix that would also prevent similar failures elsewhere at the least cost to production:
the widest zone where the same hazard exists over one bench or segment (a rock can come off any highwall,
so "benches" beats a single bench), a targeted material over *, unless the evidence says otherwise.
A speed cap only prevents a collision with a suddenly fallen rock if the truck can stop within the distance at
which it appeared: stopping distance is about v^2 / 4 m at 2 m/s^2 (v in m/s) plus 0.1 s of travel.
Haul trucks drive up to 43 km/h empty and 32 km/h loaded, brake at 2 m/s^2 and see {SENSOR_RANGE / 1000:.0f} m ahead with lidar.
Do not repeat a rule that is already in current_policy. Ground every cause in the capsule evidence.
Answer with only this JSON, no prose:
{{"hypotheses": [{{"cause": "...", "fix": "<one DSL rule>", "rationale": "..."}}]}}"""


class InferenceError(RuntimeError):
    pass


_NOT_CHAT = ("embed", "whisper", "tts", "audio", "realtime", "transcribe", "image", "dall-e", "moderation",
             "search", "babbage", "davinci")




_MODELS: dict[str, str] = {}   # picked once per endpoint: /models is not free to call on every request


async def _pick_model(http: httpx.AsyncClient, settings: Settings) -> str:
    if settings.inference_model:
        return settings.inference_model
    key = f"{settings.inference_provider}:{settings.inference_url}"
    if key in _MODELS:
        return _MODELS[key]
    _MODELS[key] = await _choose_model(http, settings)
    return _MODELS[key]


async def _choose_model(http: httpx.AsyncClient, settings: Settings) -> str:
    r = await http.get(f"{settings.inference_url}/models")
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    chat = [i for i in ids if not any(x in i.lower() for x in _NOT_CHAT)]
    for pref in ("instruct", "llama", "qwen", "mistral"):
        match = next((i for i in chat if pref in i.lower()), None)
        if match:
            return match
    if not chat:
        raise InferenceError("no chat models available")
    return chat[0]


def _request(settings: Settings, model: str, messages: list[dict]) -> dict:
    return {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 900,
            "response_format": {"type": "json_object"}}


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
            data = r.json()
            u = data.get("usage") or {}
            USAGE.record("diagnosis", int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
            answer = data["choices"][0]["message"]["content"] or ""
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
