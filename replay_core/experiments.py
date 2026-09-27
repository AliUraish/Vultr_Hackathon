"""Experiments on a recorded incident: the investigator's instruments.

Every experiment is a deterministic simulation derived from a capsule, so every
number the investigator reports can be re-run and gets the same answer.

  variant   the incident's situation re-created with a twist: the hazard placed at
            a different gap, time, robot or aisle, and/or new random pick/drop
            timings; run under an explicit policy
  stress    a fix against N variants: of the variants where the hazard causes the
            failure under the current policy, how many does the fix prevent, and
            what does it cost in distance travelled and jobs completed
  isolate   delta debugging (ddmin) over the recorded inputs: the smallest set of
            events that still reproduces the failure
"""
from __future__ import annotations

import random
from typing import Any

from .capsule import SAFETY_FAILURES, policy_at
from .detector import detect
from .engine import make_frame, set_policy, step
from .frames import ui_frame
from .hashing import clone
from .policy import PolicyError, make_policy, parse_rule
from .rng import seed_state
from .scenarios import reinject_hints, resolve
from .world import TICK_HZ, W

HAZARD = {"collision": "pallet_drop", "wrong_item": "mislabel_bin", "zone_breach": "worker_in_aisle"}
AFTER = {"collision": 20 * TICK_HZ, "wrong_item": 90 * TICK_HZ, "zone_breach": 30 * TICK_HZ}
DEFAULT_AFTER = 40 * TICK_HZ
PLACE_WITHIN = 40 * TICK_HZ   # a hazard that finds no robot in position within 40 s leaves the variant unplaced
ISOLATE_TAIL = 10 * TICK_HZ
SPREAD = 12 * TICK_HZ         # variants place the hazard up to 12 s before or after the original
AISLES = tuple(sorted(z for z in W.zones if z.startswith("aisle_")))


# ------------------------------------------------------------------ describing things for people

def describe_input(t: int, i: dict) -> str:
    kind = i.get("kind")
    if kind == "cmd":
        job = i.get("job") or {}
        picks = [s.get("slot") for s in i.get("steps", []) if s.get("op") == "pick"]
        return f"t{t}: job {job.get('id', '?')} → {i.get('robot')} (pick {', '.join(picks) or '–'} → {job.get('dock', '?')})"
    if kind == "chaos":
        where = i.get("cell") or i.get("slot") or i.get("zone") or ""
        if isinstance(where, list):
            where = f"c{where[0]}_{where[1]}"
        extra = f", {i['gap_mm']} mm ahead of {i.get('target')}" if isinstance(i.get("gap_mm"), int) else ""
        return f"t{t}: {i.get('type')} {where}{extra}"
    return f"t{t}: {kind} {i.get('robot', '')}".strip()


def describe_variant(v: dict) -> str:
    hz = v.get("hazard")
    parts = []
    if hz:
        h = hz["hints"]
        if "gap_mm" in h:
            parts.append(f"pallet {h['gap_mm']} mm ahead")
        if "zone" in h:
            parts.append(f"in {h['zone']}")
        if "cls" in h:
            parts.append(f"{h['cls']} bin")
        parts.append(f"at t{hz['at']}")
    if v.get("reseed"):
        parts.append("new pick timings")
    return ", ".join(parts) or "as recorded"


# ------------------------------------------------------------------ variants

def original_hazard(capsule: dict) -> tuple[int | None, dict | None]:
    """The recorded chaos input behind the failure, if it is inside the capsule."""
    want = HAZARD.get(capsule["failure"]["type"])
    found: tuple[int | None, dict | None] = (None, None)
    for t, ins in capsule["inputs"]:
        if t > capsule["failure"]["tick"]:
            break
        for i in ins:
            if i.get("kind") == "chaos" and i.get("scenario") == want:
                found = (t, i)
    return found


def make_variants(capsule: dict, n: int, seed: int = 0) -> list[dict]:
    """Variant 0 re-creates the incident itself; the rest perturb it. Deterministic in (capsule, n, seed)."""
    ftype = capsule["failure"]["type"]
    scen = HAZARD.get(ftype)
    t0, orig = original_hazard(capsule)
    base = {k: v for k, v in (reinject_hints(orig) if orig else {}).items()
            if k not in ("fallback_zone", "fallback_after", "now")}
    at0 = t0 if t0 is not None else max(capsule["start"], capsule["failure"]["tick"] - 10 * TICK_HZ)
    first = {"id": 0, "reseed": None,
             "hazard": {"scenario": scen, "hints": base, "at": at0} if (scen and orig) else None}
    out = [first]
    rng = random.Random(f"{capsule['hash']}:{seed}")
    for i in range(1, n):
        v: dict[str, Any] = {"id": i, "reseed": rng.randrange(1, 2**31) if rng.random() < 0.7 else None}
        hints: dict[str, Any] = {}
        if ftype == "collision":  # mostly other aisles, so a fix scoped to the incident's aisle is caught out
            r = rng.random()
            zone = rng.choice(AISLES) if r < 0.6 else "racks" if r < 0.85 else base.get("zone", "racks")
            hints = {"min_v": 1, "zone": zone, "gap_mm": rng.randint(160, 440)}
        elif ftype == "wrong_item" and "cls" in base and rng.random() < 0.7:
            hints = {"cls": base["cls"]}
        elif ftype == "zone_breach" and "zone" in base and rng.random() < 0.4:
            hints = {"zone": base["zone"]}
        v["hazard"] = ({"scenario": scen, "hints": hints, "at": max(capsule["start"], at0 + rng.randint(-SPREAD, SPREAD))}
                       if scen else None)
        out.append(v)
    for v in out:
        v["label"] = "the incident, re-created" if v["id"] == 0 else describe_variant(v)
    return out


def run_variant(capsule: dict, rules: list[str], variant: dict, keep_frames: bool = False) -> dict[str, Any]:
    """Run one variant under an explicit policy. Returns what the stress test needs (and frames if asked)."""
    ftype = capsule["failure"]["type"]
    state = clone(capsule["snapshot"])
    set_policy(state, make_policy(rules, 0), [])
    if variant.get("reseed"):
        state["rng"] = seed_state(variant["reseed"])
    hz = variant.get("hazard")
    scen = hz["scenario"] if hz else None
    inputs_at: dict[int, list] = {}
    for t, ins in capsule["inputs"]:
        keep = [i for i in ins if i.get("kind") != "policy"
                and not (hz and i.get("kind") == "chaos" and i.get("scenario") == scen)]
        if keep:
            inputs_at[t] = keep
    after = AFTER.get(ftype, DEFAULT_AFTER)
    dist0 = sum(r["odo"] for r in state["robots"].values())
    fired_at: int | None = None
    fired: dict | None = None
    failures: list[dict] = []
    frames: list[dict] = []
    jobs = wait = 0
    t = capsule["start"]
    while True:
        ins = clone(inputs_at.get(t, []))
        if hz and fired_at is None and hz["at"] <= t < hz["at"] + PLACE_WITHIN:
            got = resolve(state, scen, hz["hints"])
            if got:
                ins += got
                fired_at, fired = t, got[0]
        events = step(state, ins)
        frame = make_frame(state, events, ins)
        failures.extend(detect(frame))
        jobs += sum(1 for e in events if e["type"] == "job_done" and e.get("ok"))
        wait += sum(1 for r in frame["robots"] if r["st"] in ("waiting", "blocked", "held"))
        if keep_frames:
            frames.append(ui_frame(frame, with_paths=False))
        t += 1
        if t < capsule["end"]:
            continue
        if hz is None and t >= capsule["end"] + DEFAULT_AFTER:
            break
        if hz is not None and fired_at is None and t >= hz["at"] + PLACE_WITHIN:
            break
        if fired_at is not None and t >= fired_at + after:
            break
        if t > capsule["end"] + 4000:
            break
    since = fired_at if hz else capsule["start"]
    relevant = [f for f in failures if since is not None and f["tick"] > since]
    hits = [f for f in relevant if f["type"] == ftype]
    latent = False
    if ftype == "wrong_item" and not hits:  # a slow fix can merely delay the dock scan past the window:
        open_jobs = state["jobs"]            # a robot still carrying a wrong item counts as failing
        latent = any(i.get("job") in open_jobs and i["sku"] not in open_jobs[i["job"]]["skus"]
                     for r in state["robots"].values() for i in r["carry"])
    return {
        "id": variant["id"],
        "fired": hz is None or fired_at is not None,
        "fired_at": fired_at,
        "hazard": {k: fired[k] for k in ("type", "cell", "slot", "zone", "gap_mm", "target") if k in fired} if fired else None,
        "target": bool(hits) or latent,
        "latent": latent,
        "first_hit": hits[0]["tick"] if hits else None,
        "safety": sorted({f["type"] for f in relevant if f["type"] in SAFETY_FAILURES and f["type"] != ftype}),
        "jobs": jobs,
        "dist_m": round((sum(r["odo"] for r in state["robots"].values()) - dist0) / 1000, 1),
        "wait_s": round(wait / TICK_HZ, 1),
        "ticks": t - capsule["start"],
        **({"frames": frames} if keep_frames else {}),
    }


def _failed(r: dict) -> bool:
    return bool(r["fired"] and (r["target"] or r["safety"]))


def tile(b: dict, c: dict) -> str:
    """How one variant came out: baseline policy vs the candidate."""
    if not c["fired"]:
        return "not_placed"      # the hazard found no robot in position under the fix: no evidence
    if _failed(c):
        return "still_fails" if _failed(b) else "introduced"
    return "prevented" if _failed(b) else "clean"


def aggregate(variants: list[dict], baseline: list[dict], candidate: list[dict]) -> dict[str, Any]:
    """Stress-test verdict for a fix.

    robustness  share of the variants (where the hazard was placed) that end without the failure
                or any other safety failure under the fix
    prevented   of the variants that fail today, how many the fix saves
    introduced  variants that were fine today but fail with the fix
    cost_pct    lost throughput: distance travelled per second vs today, same variants
    """
    base = {r["id"]: r for r in baseline}
    cand = {r["id"]: r for r in candidate}
    tiles = []
    counts = {"prevented": 0, "still_fails": 0, "introduced": 0, "clean": 0, "not_placed": 0}
    placed_b = failed_b = 0
    db = dc = tb = tc = jb = jc = 0.0
    for v in variants:
        b, c = base.get(v["id"]), cand.get(v["id"])
        if b is None or c is None:
            continue
        kind = tile(b, c)
        counts[kind] += 1
        placed_b += b["fired"]
        failed_b += _failed(b)
        if b["fired"] and c["fired"]:
            db += b["dist_m"]; dc += c["dist_m"]; tb += b["ticks"]; tc += c["ticks"]
            jb += b["jobs"]; jc += c["jobs"]
        tiles.append({"id": v["id"], "label": v.get("label", ""), "outcome": kind,
                      "base": {"hit": b["target"], "safety": b["safety"], "at": b["first_hit"]},
                      "fix": {"hit": c["target"], "safety": c["safety"], "at": c["first_hit"],
                              "hazard": c["hazard"]}})
    placed_c = len(tiles) - counts["not_placed"]
    safe_c = placed_c - counts["still_fails"] - counts["introduced"]
    rate_b, rate_c = (db / tb if tb else 0.0), (dc / tc if tc else 0.0)
    return {
        "variants": len(tiles),
        "placed": placed_c,
        "robustness": round(safe_c / placed_c, 3) if placed_c else None,
        "baseline_robustness": round((placed_b - failed_b) / placed_b, 3) if placed_b else None,
        "baseline_failed": failed_b,
        **counts,
        "cost_pct": round((rate_b - rate_c) / rate_b * 100, 1) if rate_b else None,
        "jobs_base": int(jb), "jobs_fix": int(jc),
        "tiles": tiles,
    }


# ------------------------------------------------------------------ tuning

TUNABLE = {"speed_cap": (1, [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1]),
           "min_clearance": (0, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8])}


def sweep(fix: str, values: list[float] | None = None) -> list[str]:
    """The same fix at a range of settings: speed_cap(racks, 0.6) -> speed_cap(racks, 0.3) .. (racks, 1.1)."""
    rule = parse_rule(fix)
    if rule.name not in TUNABLE:
        raise PolicyError(f"{rule.name} has no numeric setting to tune; tunable: {', '.join(TUNABLE)}")
    pos, default = TUNABLE[rule.name]
    out: list[str] = []
    for v in (values or default)[:12]:
        args = list(rule.args)
        args[pos] = str(v)
        text = str(parse_rule(f"{rule.name}({', '.join(args)})"))
        if text not in out:
            out.append(text)
    return out


def setting(fix: str) -> float | None:
    rule = parse_rule(fix)
    return float(rule.args[TUNABLE[rule.name][0]]) if rule.name in TUNABLE else None


def pick_setting(points: list[dict], min_robustness: float) -> dict | None:
    """Cheapest setting that clears the robustness bar (points: {fix, robustness, cost_pct})."""
    ok = [p for p in points if p["robustness"] is not None and p["robustness"] >= min_robustness]
    return min(ok, key=lambda p: (p["cost_pct"] if p["cost_pct"] is not None else 1e9, -p["robustness"])) if ok else None


# ------------------------------------------------------------------ cause isolation

def _reproduces(capsule: dict, allowed: set[tuple[int, int]]) -> bool:
    """Replay with the recorded policy but only the allowed (tick, index) inputs; does the failure recur?

    "The failure" means the same kind of failure by any robot: with fewer jobs a different
    robot may be the one that drives into the pallet, and that is still the same incident."""
    target = capsule["failure"]
    state = clone(capsule["snapshot"])
    inputs_at: dict[int, list] = {}
    for t, ins in capsule["inputs"]:
        keep = [i for idx, i in enumerate(ins) if i.get("kind") == "policy" or (t, idx) in allowed]
        if keep:
            inputs_at[t] = keep
    end = min(capsule["end"], target["tick"] + ISOLATE_TAIL)
    for t in range(capsule["start"], end):
        ins = clone(inputs_at.get(t, []))
        events = step(state, ins)
        for f in detect(make_frame(state, events, ins)):
            if f["type"] == target["type"]:
                return True
    return False


def isolate(capsule: dict, budget: int = 90) -> dict[str, Any]:
    """ddmin over the recorded inputs (policy changes are always kept)."""
    elems = [(t, idx) for t, ins in capsule["inputs"] for idx, i in enumerate(ins) if i.get("kind") != "policy"]
    lookup = {(t, idx): i for t, ins in capsule["inputs"] for idx, i in enumerate(ins)}
    trace: list[dict] = []
    tests = 0

    def fails(subset: list[tuple[int, int]]) -> bool:
        nonlocal tests
        tests += 1
        ok = _reproduces(capsule, set(subset))
        trace.append({"size": len(subset), "fails": ok})
        return ok

    if not fails(elems):
        return {"ok": False, "reason": "the failure does not reproduce from the recorded inputs alone",
                "tested": tests, "total": len(elems), "trace": trace}
    if fails([]):
        return {"ok": True, "minimal": [], "tested": tests, "total": len(elems), "trace": trace,
                "summary": "fails with no recorded inputs at all: the cause is already in the starting state"}
    cur, n = elems, 2
    while len(cur) >= 2 and tests < budget:
        size = max(1, len(cur) // n)
        chunks = [cur[i:i + size] for i in range(0, len(cur), size)]
        reduced = False
        for c in chunks:
            if tests >= budget:
                break
            if fails(c):
                cur, n, reduced = c, 2, True
                break
        if not reduced:
            for c in chunks:
                if tests >= budget:
                    break
                comp = [e for e in cur if e not in c]
                if comp and fails(comp):
                    cur, n, reduced = comp, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= len(cur):
                break
            n = min(len(cur), n * 2)
    minimal = [{"tick": t, "input": describe_input(t, lookup[(t, idx)]), "kind": lookup[(t, idx)].get("kind")}
               for t, idx in cur]
    if len(minimal) == len(elems):
        summary = (f"all {len(elems)} recorded events are needed: the failure depends on the exact timing of "
                   "every robot's job, so it is a physics/timing problem, not one bad input")
    else:
        summary = f"{len(minimal)} of {len(elems)} recorded events are enough to cause it"
    return {"ok": True, "minimal": minimal, "tested": tests, "total": len(elems), "trace": trace,
            "budget_hit": tests >= budget, "summary": summary}


def baseline_rules(capsule: dict) -> list[str]:
    return policy_at(capsule, capsule["failure"]["tick"])


__all__ = ["HAZARD", "aggregate", "baseline_rules", "describe_input", "isolate", "make_variants", "pick_setting",
           "run_variant", "setting", "sweep", "tile"]
