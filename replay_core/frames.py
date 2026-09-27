"""Compact frames for the browser: only what the floor map and replay viewer draw."""
from __future__ import annotations

# Events worth showing on a replay timeline.
_VISIBLE = {"contact", "struck", "pallet_dropped", "dock_scan", "scan", "scan_mismatch", "pick",
            "zone_enter", "zone_restricted", "zone_lifted", "job_done", "job_exception",
            "policy_applied", "bin_mislabeled", "pallets_cleared",
            "wait", "standoff", "yield", "resume",   # fleet coordination
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
        robots.append(out)
    return {
        "t": frame["t"],
        "robots": robots,
        "pallets": [p["cell"] for p in frame["pallets"]],
        "zones": [z["zone"] for z in frame["restricted"]],
        "ev": [e for e in frame["ev"] if e["type"] in _VISIBLE],
    }
