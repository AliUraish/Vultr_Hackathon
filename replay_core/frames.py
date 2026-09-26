"""Compact frames for the browser: only what the floor map and replay viewer draw."""
from __future__ import annotations

# Events worth showing on a replay timeline.
_VISIBLE = {"contact", "struck", "pallet_dropped", "dock_scan", "scan", "scan_mismatch", "pick",
            "zone_enter", "zone_restricted", "zone_lifted", "job_done", "job_exception",
            "policy_applied", "bin_mislabeled", "pallets_cleared"}


def ui_frame(frame: dict, with_paths: bool = True) -> dict:
    robots = []
    for r in frame["robots"]:
        out = {"id": r["id"], "x": r["x"], "y": r["y"], "v": r["v"], "d": r["dir"], "st": r["st"],
               "job": r["job"], "c": len(r["carry"])}
        if with_paths:
            out["p"] = r["path"][:8]
        robots.append(out)
    return {
        "t": frame["t"],
        "robots": robots,
        "pallets": [p["cell"] for p in frame["pallets"]],
        "zones": [z["zone"] for z in frame["restricted"]],
        "ev": [e for e in frame["ev"] if e["type"] in _VISIBLE],
    }
