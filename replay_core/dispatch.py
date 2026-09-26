"""Rule-based dispatcher: turns a job into fleet commands and picks the robot.

Rule: capable robots only -> free robots only -> fewest spare capabilities
(keep the one heavy-lift robot free for heavy jobs) -> nearest to the first
pick (Manhattan cells) -> lowest robot id. No learning, no LLM.
"""
from __future__ import annotations

from .world import CELL, JOB_DEADLINE_TICKS, W


def requirements(job: dict) -> set[str]:
    return {"heavy"} if any(line["cls"] == "heavy" for line in job["lines"]) else {"standard"}


def make_job(job_id: str, slots: list[str], dock: str, created_tick: int) -> dict:
    lines = [{"slot": s, "sku": W.slots[s]["sku"], "cls": W.slots[s]["cls"]} for s in slots]
    return {
        "id": job_id, "kind": "multi" if len(lines) > 1 else "single",
        "lines": lines, "dock": dock, "deadline": created_tick + JOB_DEADLINE_TICKS,
    }


def build_steps(job: dict) -> list[dict]:
    steps: list[dict] = []
    for line in job["lines"]:
        steps.append({"op": "goto", "cell": list(W.slots[line["slot"]]["access"])})
        steps.append({"op": "pick", "slot": line["slot"], "sku": line["sku"], "cls": line["cls"]})
    steps.append({"op": "goto", "cell": list(W.docks[job["dock"]])})
    steps.append({"op": "drop", "dock": job["dock"]})
    return steps


def choose_robot(frame: dict, job: dict, exclude: set[str] | frozenset[str] = frozenset()) -> str | None:
    need = requirements(job)
    ax, ay = W.slots[job["lines"][0]["slot"]]["access"]
    best: tuple[tuple[int, int, str], str] | None = None
    for r in frame["robots"]:
        caps = W.robot_caps[r["id"]]
        if not r["free"] or r["id"] in exclude or not need <= caps:
            continue
        key = (len(caps - need), abs(r["x"] // CELL - ax) + abs(r["y"] // CELL - ay), r["id"])
        if best is None or key < best[0]:
            best = (key, r["id"])
    return best[1] if best else None


def make_cmd(robot_id: str, job: dict) -> dict:
    """The fleet-API input that assigns a job: its steps plus the job metadata."""
    return {
        "kind": "cmd", "robot": robot_id,
        "job": {"id": job["id"], "kind": job["kind"], "skus": [l["sku"] for l in job["lines"]],
                "dock": job["dock"], "deadline": job["deadline"]},
        "steps": build_steps(job),
    }
