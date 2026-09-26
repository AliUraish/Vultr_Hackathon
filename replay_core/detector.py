"""Rule-based failure detector.

A pure function of one frame, so the control plane (judging live telemetry)
and a replay worker (judging a replay) reach identical verdicts. Every rule
fires on a crossing, never on a level, so each failure is reported once.
"""
from __future__ import annotations

from .world import STALL_TICKS

FAILURE_TYPES: tuple[str, ...] = ("collision", "stall", "task_overdue", "wrong_item", "zone_breach")


def detect(frame: dict) -> list[dict]:
    t = frame["t"]
    out: list[dict] = []
    for e in frame["ev"]:
        typ = e["type"]
        if typ == "contact":
            out.append({"type": "collision", "robot": e["robot"], "tick": t,
                        "detail": {"with": e["with"], "v": e["v"], "cell": e["cell"]}})
        elif typ == "dock_scan" and not e["ok"]:
            out.append({"type": "wrong_item", "robot": e["robot"], "tick": t,
                        "detail": {"job": e["job"], "expected": e["expected"], "actual": e["actual"]}})
        elif typ == "zone_enter" and e.get("restricted"):
            out.append({"type": "zone_breach", "robot": e["robot"], "tick": t,
                        "detail": {"zone": e["zone"]}})
    for r in frame["robots"]:
        if r["blk"] == STALL_TICKS:
            out.append({"type": "stall", "robot": r["id"], "tick": t,
                        "detail": {"status": r["st"], "job": r["job"]}})
    for j in frame["jobs"]:
        if j["deadline"] == t:
            out.append({"type": "task_overdue", "robot": j["robot"], "tick": t,
                        "detail": {"job": j["id"]}})
    return out
