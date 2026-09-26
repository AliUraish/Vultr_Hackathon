"""Capsules: the unit of reproducibility.

A capsule is a full state snapshot from before a failure plus every input
recorded from that snapshot through T+2 s, and the live per-tick state hashes.
Replaying it with its own policy must reproduce the live run hash-for-hash;
replaying it with a candidate policy shows whether a fix avoids the failure.
"""
from __future__ import annotations

from typing import Any

from .detector import detect
from .engine import make_frame, set_policy, step
from .hashing import chain_hash, clone, sha256
from .policy import make_policy
from .state import ENGINE_VERSION, state_hash
from .world import MAP_HASH, TICK_HZ

CAPSULE_FORMAT = 1
PRE_TICKS = 10 * TICK_HZ       # start at least 10 s before the failure...
MAX_PRE_TICKS = 60 * TICK_HZ   # ...or at the failing job's start, but never more than 60 s back
POST_TICKS = 2 * TICK_HZ       # and run 2 s past it
# Fix trials keep simulating past the capsule end (no new inputs) so a fix that only
# delays the failure beyond T+2 s is not mistaken for one that prevents it.
HORIZON_TICKS = 40 * TICK_HZ
# What blocks a fix: a new safety failure at any time, or any new failure inside the recorded window.
# A stall or overdue job that only shows up in the 40 s continuation (no new jobs, nobody clears the
# floor) is weak evidence, so it is reported as a warning for the approver instead.
SAFETY_FAILURES = ("collision", "wrong_item", "zone_breach")


class CapsuleError(ValueError):
    pass


def window(fail_tick: int, job_start_tick: int | None = None, run_start_tick: int = 0) -> tuple[int, int]:
    """(target start tick, end tick) for a failure at fail_tick.

    Reaching back to the failing job's start matters: a wrong item picked 15 s
    before the dock scan must be inside the capsule, or no fix can change it.
    """
    start = fail_tick - PRE_TICKS
    if job_start_tick is not None:
        start = min(start, job_start_tick)
    start = max(start, fail_tick - MAX_PRE_TICKS, run_start_tick)
    return start, fail_tick + POST_TICKS


def pick_snapshot(snapshot_ticks: list[int], target_start: int) -> int:
    """Latest snapshot at or before the target start (earliest one if none is)."""
    before = [t for t in snapshot_ticks if t <= target_start]
    if before:
        return max(before)
    if not snapshot_ticks:
        raise CapsuleError("no snapshots available")
    return min(snapshot_ticks)


def build(run_id: str, snapshot_state: dict, records: list[dict], failure: dict) -> dict:
    """Cut a capsule. `records` are ticks snapshot+1 .. end: {tick, inputs, hash, frame}."""
    start = snapshot_state["tick"]
    ticks = [r["tick"] for r in records]
    if not ticks or ticks != list(range(start + 1, start + 1 + len(ticks))):
        raise CapsuleError(f"telemetry gap: need ticks {start + 1}.. contiguous, got {ticks[:3]}..")
    end = ticks[-1]
    if not start < failure["tick"] <= end:
        raise CapsuleError(f"failure tick {failure['tick']} outside capsule [{start}, {end}]")
    core = {
        "format": CAPSULE_FORMAT,
        "run_id": run_id,
        "map": snapshot_state["map"],
        "seed": snapshot_state["seed"],
        "start": start,
        "end": end,
        "failure": {"type": failure["type"], "robot": failure["robot"], "tick": failure["tick"]},
        "snapshot": clone(snapshot_state),
        "inputs": [[r["tick"] - 1, clone(r["inputs"])] for r in records if r["inputs"]],
        "live_hashes": [r["hash"] for r in records],
    }
    capsule = dict(core)
    capsule["hash"] = sha256(core)
    capsule["policy"] = {k: snapshot_state["policy"][k] for k in ("version", "rules", "hash")}
    capsule["baseline_failures"] = [f for r in records for f in detect(r["frame"])]
    capsule["live_frames"] = [r["frame"] for r in records]
    return capsule


def policy_at(capsule: dict, tick: int) -> list[str]:
    """Rules the live fleet was running at `tick`: the snapshot's, or a later hot-reload before it.
    Fix trials start from these, so a fix approved mid-capsule is not silently dropped."""
    rules = list(capsule["snapshot"]["policy"]["rules"])
    for t, ins in capsule["inputs"]:
        if t < tick:
            for i in ins:
                if i.get("kind") == "policy":
                    rules = list(i["rules"])
    return rules


def verify(capsule: dict) -> None:
    core = {k: capsule[k] for k in ("format", "run_id", "map", "seed", "start", "end",
                                    "failure", "snapshot", "inputs", "live_hashes")}
    if sha256(core) != capsule["hash"]:
        raise CapsuleError("capsule hash mismatch: contents were altered")
    if capsule["map"] != MAP_HASH:
        raise CapsuleError("capsule was recorded on a different map")
    recorded = capsule["snapshot"].get("engine", 1)
    if recorded != ENGINE_VERSION:
        raise CapsuleError(f"capsule was recorded by sim engine v{recorded}; this is v{ENGINE_VERSION}")


def _simulate(capsule: dict, policy_rules: list[str] | None, policy_version: int,
              horizon: int, keep_frames: bool) -> tuple[list[str], list[dict], list[dict], str]:
    state = clone(capsule["snapshot"])
    override = policy_rules is not None
    if override:
        set_policy(state, make_policy(policy_rules, policy_version), [])
    policy_hash = state["policy"]["hash"]
    inputs_at = {t: ins for t, ins in capsule["inputs"]}
    hashes: list[str] = []
    frames: list[dict] = []
    failures: list[dict] = []
    for t in range(capsule["start"], capsule["end"] + horizon):
        ins = clone(inputs_at.get(t, []))
        if override:
            ins = [i for i in ins if i.get("kind") != "policy"]
        events = step(state, ins)
        frame = make_frame(state, events, ins)
        hashes.append(state_hash(state))
        failures.extend(detect(frame))
        if keep_frames:
            frames.append(frame)
    return hashes, frames, failures, policy_hash


def classify(failures: list[dict], control: list[dict], target: dict, window_end: int
             ) -> tuple[str, list[dict], list[dict]]:
    """(outcome, blocking new failures, warnings). See SAFETY_FAILURES."""
    hit = any(f["type"] == target["type"] and f["robot"] == target["robot"] for f in failures)
    seen = {(f["type"], f["robot"]) for f in control}
    new = [f for f in failures if (f["type"], f["robot"]) not in seen]
    blocking = [f for f in new if f["type"] in SAFETY_FAILURES or f["tick"] <= window_end]
    warnings = [f for f in new if f not in blocking]
    return ("reproduced" if hit else "regressed" if blocking else "avoided"), blocking, warnings


def run(capsule: dict, policy_rules: list[str] | None = None, policy_version: int = 0,
        keep_frames: bool = True, control_failures: list[dict] | None = None,
        control_rules: list[str] | None = None, horizon: int = HORIZON_TICKS) -> dict[str, Any]:
    """Replay a capsule.

    Without policy_rules this is a reproduction: the recorded policy runs and the
    per-tick hashes are compared with the live run. With policy_rules it is a fix
    trial: the fleet runs that policy instead, and the result is compared against
    a control run over the same horizon (recorded policy, or control_rules --
    regression checks use the current fleet policy as the control):
      reproduced  the target failure still happens (possibly later)
      regressed   it doesn't, but a new safety failure appears, or any new failure in the window
      avoided     neither (new stalls/overdue jobs past the window come back as warnings)
    """
    verify(capsule)
    override = policy_rules is not None
    hashes, frames, failures, policy_hash = _simulate(
        capsule, policy_rules, policy_version, horizon, keep_frames)
    window = capsule["end"] - capsule["start"]

    if override and control_failures is None:
        control_failures = _simulate(capsule, control_rules, 0, horizon, False)[2]
    outcome, blocking, warnings = classify(failures, (control_failures if override else failures),
                                           capsule["failure"], capsule["end"])

    matches = None
    divergence = None
    if not override:
        live = capsule["live_hashes"]
        matches = hashes[:window] == live
        if not matches:
            divergence = next((capsule["start"] + 1 + i for i, (a, b)
                               in enumerate(zip(hashes, live)) if a != b), None)
    return {
        "outcome": outcome,
        "failures": failures,
        "new_failures": blocking,
        "warnings": warnings,
        "trajectory_hash": chain_hash(hashes[:window]),
        "horizon_hash": chain_hash(hashes),
        "matches_live": matches,
        "first_divergence": divergence,
        "ticks": len(hashes),
        "window_ticks": window,
        "policy_hash": policy_hash,
        "control_failures": control_failures if override else failures,
        "frames": frames,
    }
