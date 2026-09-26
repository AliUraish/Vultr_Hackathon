"""Replay worker on VM B:  python -m simnode.worker --id b1 [--procs 3]

Claims replay jobs from the control plane's queue, replays the capsule
deterministically (with the recorded policy, or with a candidate fix), and
reports outcome, failures and trajectory hash. Workers hold no state beyond a
small capsule cache, so any number can run in parallel.
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import os
import time

import httpx

from replay_core import capsule as caps
from replay_core.frames import ui_frame

log = logging.getLogger("replay.worker")
KEEP_FRAMES = ("reproduce", "trial", "proof")  # replays a person watches; regression runs only need verdicts
CACHE_SIZE = 32


def run_job(job: dict, blob: dict) -> dict:
    t0 = time.perf_counter()
    keep = job["kind"] in KEEP_FRAMES
    try:
        res = caps.run(blob, job["policy_rules"], job["policy_version"] or 0, keep_frames=keep,
                       control_failures=job["control_failures"], control_rules=job["control_rules"])
    except Exception as exc:  # report it; the orchestrator treats it as a failed replay
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": int((time.perf_counter() - t0) * 1000)}
    return {
        "status": "done", "outcome": res["outcome"], "failures": res["failures"],
        "new_failures": res["new_failures"], "warnings": res["warnings"], "trajectory_hash": res["trajectory_hash"],
        "matches_live": res["matches_live"], "first_divergence": res["first_divergence"],
        "frames": [ui_frame(f, with_paths=False) for f in res["frames"]] if keep else None,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
    }


def serve(worker_id: str, control_url: str, token: str) -> None:
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {worker_id} %(levelname)s: %(message)s")
    http = httpx.Client(base_url=control_url, headers={"X-Node-Token": token}, timeout=30.0)
    cache: dict[int, dict] = {}
    while True:
        try:
            r = http.post("/api/node/replays/claim", json={"worker": worker_id})
        except httpx.HTTPError as exc:
            log.warning("control plane unreachable: %s", exc)
            time.sleep(2.0)
            continue
        if r.status_code == 204:
            time.sleep(0.5)
            continue
        if r.status_code >= 400:
            log.warning("claim failed: %s %s", r.status_code, r.text[:200])
            time.sleep(2.0)
            continue
        job = r.json()
        cid = job["capsule_id"]
        blob = cache.get(cid)
        if blob is None or blob.get("hash") != job["capsule_hash"]:
            blob = http.get(f"/api/node/capsules/{cid}").raise_for_status().json()
            if len(cache) >= CACHE_SIZE:
                cache.pop(next(iter(cache)))
            cache[cid] = blob
        result = run_job(job, blob)
        log.info("replay %s (%s, capsule %s): %s %s in %s ms", job["id"], job["kind"], cid, result["status"],
                 result.get("outcome") or result.get("error"), result["duration_ms"])
        for attempt in range(3):
            try:
                http.post(f"/api/node/replays/{job['id']}/result", json=result).raise_for_status()
                break
            except httpx.HTTPError as exc:
                log.warning("result for replay %s not delivered (%s), attempt %d", job["id"], exc, attempt + 1)
                time.sleep(1.0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", default=os.environ.get("WORKER_ID", "worker"))
    ap.add_argument("--procs", type=int, default=1, help="worker processes to run")
    args = ap.parse_args()
    control, token = os.environ.get("CONTROL_URL", "").rstrip("/"), os.environ.get("NODE_TOKEN", "")
    if not control or not token:
        raise SystemExit("CONTROL_URL and NODE_TOKEN must be set")
    if args.procs == 1:
        serve(args.id, control, token)
        return
    procs = [mp.get_context("spawn").Process(target=serve, args=(f"{args.id}-{i}", control, token), daemon=True)
             for i in range(1, args.procs + 1)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()


if __name__ == "__main__":
    main()
