"""Compact frames for the browser: only what the floor map and replay viewer draw."""
from __future__ import annotations

# Events worth showing on a replay timeline.
_VISIBLE = {"contact", "struck", "rock_fell", "dock_scan", "scan", "scan_mismatch", "pick",
            "zone_enter", "zone_restricted", "zone_lifted", "job_done", "job_exception",
            "policy_applied", "grade_mislabeled", "rocks_cleared",
            "wait", "standoff", "yield", "resume", "queue", "traffic_stale",   # fleet coordination
            "pothole_formed", "pothole_detected", "pothole_enter", "pothole_exit", "pothole_strike", "potholes_filled",
            "road_changed", "bump_jump", "advice",
            "fault_alarm", "job_released", "service_move", "service_arrived", "standby", "deployed", "repaired"}


def ui_frame(frame: dict, with_paths: bool = True) -> dict:
    robots = []
    for r in frame["robots"]:
        out = {"id": r["id"], "x": r["x"], "y": r["y"], "v": r["v"], "d": r["dir"], "st": r["st"],
               "job": r["job"], "c": len(r["carry"])}
        if with_paths:  # live frames: what the 3D floor shows about coordination and the robot's work
            out["p"] = r["path"][:8]
            out.update(r=r.get("res", []), w=r.get("wait_on") or None, cs=r["carry"], o=r.get("odo", 0),
                       op=r.get("op"), g=r.get("goal"), f=r.get("fault"), sv=r.get("svc"), h=r.get("health"))
            if r.get("adv"):
                out["ad"] = r["adv"]
        if r.get("hole"):
            out["hl"] = r["hole"]
        robots.append(out)
    return {
        "t": frame["t"],
        "robots": robots,
        "rocks": [p["cell"] for p in frame["pallets"]],
        "holes": [[p["cell"][0], p["cell"][1], p["depth"], 1 if p.get("known") else 0, p["id"], p.get("kind", "pothole")[0]]
                  for p in frame.get("potholes", [])],
        "zones": [z["zone"] for z in frame["restricted"]],
        "ev": [e for e in frame["ev"] if e["type"] in _VISIBLE],
    }
