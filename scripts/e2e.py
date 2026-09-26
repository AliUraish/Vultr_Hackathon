"""End-to-end check against a deployed Replay: python scripts/e2e.py http://VM_A_IP:8000

Signs in as the operator, injects a canned failure, and follows it through the whole
pipeline: capsule -> 3/3 reproduction -> diagnosis -> forked trials -> approval ->
policy hot-reload on the live fleet -> clean proof replay. Exits non-zero on any miss.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

SCENARIO_FAILURE = {"pallet_drop": "collision", "mislabel_bin": "wrong_item", "worker_in_aisle": "zone_breach"}


def secret(name: str) -> str:
    for line in (Path(__file__).resolve().parents[1] / "infra/.secrets.env").read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1]
    raise SystemExit(f"{name} missing from infra/.secrets.env")


def wait(what: str, fn, timeout: float, every: float = 2.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            got = fn()
        except (httpx.HTTPError, ValueError) as exc:  # e.g. the control plane restarting
            print(f"      (retrying {what}: {exc})")
            got = None
        if got:
            print(f"  ok  {what} ({time.time() - t0:.0f}s)")
            return got
        time.sleep(every)
    raise SystemExit(f"FAIL  {what}: nothing after {timeout:.0f}s")


def main() -> None:
    base = sys.argv[1].rstrip("/")
    scenario = sys.argv[2] if len(sys.argv) > 2 else "pallet_drop"
    http = httpx.Client(base_url=base, timeout=30.0)
    http.post("/api/login", json={"username": "ops", "password": secret("ADMIN_PASSWORD")}).raise_for_status()
    print(f"signed in to {base}")

    st = wait("live sim streaming", lambda: (s := http.get("/api/state").json())["sim_online"] and s, 60)
    t_before = st["tick"]
    policy_before = st["policy"]["version"]
    print(f"      run {st['run_id']}, tick {t_before}, policy v{policy_before}, {len(st['jobs'])} open jobs")
    wait("ticks advancing", lambda: http.get("/api/state").json()["tick"] > t_before + 20, 15)

    http.post("/api/chaos", json={"scenario": "clear_floor"}).raise_for_status()  # start from a clean floor
    time.sleep(1)
    known = {f["id"] for f in http.get("/api/failures").json()}
    r = http.post("/api/chaos", json={"scenario": scenario})
    r.raise_for_status()
    print(f"  ok  chaos {scenario} armed: {r.json()}")
    want = SCENARIO_FAILURE[scenario]

    def new_failure():
        return next((f for f in http.get("/api/failures").json()
                     if f["id"] not in known and f["type"] == want), None)
    f = wait(f"{want} detected by the rules", new_failure, 240 if scenario == "mislabel_bin" else 120)
    fid = f["id"]
    print(f"      failure #{fid}: {f['type']} {f['robot_id']} at tick {f['tick']}")

    def detail():
        return http.get(f"/api/failures/{fid}").json()
    wait("capsule cut", lambda: detail()["capsule"], 30)
    wait("first replay reproduced it", lambda: any(
        x["kind"] == "reproduce" and x["status"] == "done" for x in detail()["replays"]), 60)
    for _ in range(2):
        http.post(f"/api/failures/{fid}/replay", json={}).raise_for_status()
    def three_reproductions():
        done = [x for x in detail()["replays"] if x["kind"] == "reproduce" and x["status"] == "done"]
        return done if len(done) >= 3 else None
    reps = wait("3 reproductions done", three_reproductions, 60)
    hashes = {x["trajectory_hash"] for x in reps}
    good = [x for x in reps if x["outcome"] == "reproduced" and x["matches_live"]]
    print(f"      reproduced {len(good)}/{len(reps)}, distinct trajectory hashes: {len(hashes)} ({next(iter(hashes))[:12]})")
    if len(good) != len(reps) or len(hashes) != 1:
        raise SystemExit("FAIL  reproduction is not exact")

    d = wait("hypotheses posted", lambda: (x := detail())["hypotheses"] and x, 120)
    print(f"      diagnosis by {d['hypotheses'][0]['source']}:")
    d = wait("every trial finished and gated",
             lambda: (x := detail())["failure"]["status"] in ("awaiting_approval", "no_fix") and x, 180)
    latest = max(h["round"] for h in d["hypotheses"])
    for h in [h for h in d["hypotheses"] if h["round"] == latest]:
        trial = next((x for x in d["replays"] if x["hypothesis_id"] == h["id"] and x["kind"] == "trial"), {})
        print(f"        #{h['rank']} {h['fix_dsl']:<40} trial={trial.get('outcome')} status={h['status']}")
    ready = [h for h in d["hypotheses"] if h["round"] == latest and h["status"] == "ready"]
    if not ready:
        raise SystemExit(f"FAIL  no hypothesis passed the gate (failure status {d['failure']['status']})")

    h = min(ready, key=lambda x: x["rank"])
    r = http.post(f"/api/hypotheses/{h['id']}/approve", json={})
    if r.status_code == 409:
        print(f"      approve deferred: {r.json()['detail']}")
        wait("re-check against the new policy", lambda: any(x["id"] == h["id"] and x["status"] == "ready"
                                                             for x in detail()["hypotheses"]), 120)
        r = http.post(f"/api/hypotheses/{h['id']}/approve", json={})
    r.raise_for_status()
    new_version = r.json()["result"]["version"]
    print(f"  ok  approved {h['fix_dsl']} -> policy v{new_version}")
    wait(f"live fleet running v{new_version}",
         lambda: http.get("/api/state").json()["sim_policy_version"] == new_version, 30)
    proof = wait("proof replay under the new policy", lambda: next(
        (x for x in detail()["replays"] if x["kind"] == "proof" and x["status"] == "done"), None), 60)
    print(f"      proof replay #{proof['id']}: {proof['outcome']}")
    suite = http.get("/api/regression").json()
    assert any(k["failure_id"] == fid for k in suite), "capsule missing from regression suite"
    print(f"  ok  capsule in regression suite ({len(suite)} total)")
    if proof["outcome"] != "avoided":
        raise SystemExit("FAIL  proof replay is not clean")

    # Live proof: recreate the original situation (same aisle and gap / item class / aisle) under the fix.
    last_event = http.get("/api/events", params={"limit": 1}).json()[0]["id"]
    before = max(x["id"] for x in http.get("/api/failures").json())
    r = http.post(f"/api/failures/{fid}/reinject", json={})
    r.raise_for_status()
    print(f"  ok  re-injected live with hints {r.json().get('hints')}")

    def fired():
        evs = http.get("/api/events", params={"limit": 50}).json()
        new = [e for e in evs if e["id"] > last_event]
        if any(e["type"] == "sim.chaos_expired" for e in new):
            raise SystemExit("FAIL  re-inject expired: no robot got into the original position")
        return next((e for e in new if e["type"] == "input.chaos" and e["payload"].get("scenario") == scenario), None)
    e = wait("original situation recreated on the live floor", fired, 100)
    print(f"      t{e['tick']}: {e['payload'].get('type')} {e['payload'].get('cell') or e['payload'].get('slot') or e['payload'].get('zone')}"
          f" target {e['payload'].get('target')} gap {e['payload'].get('gap_mm', '-')} mm")
    time.sleep(40 if scenario == "mislabel_bin" else 20)  # a mislabeled pick only shows at the dock
    again = [x for x in http.get("/api/failures").json() if x["id"] > before and x["type"] == want]
    if again:
        raise SystemExit(f"FAIL  {want} happened again under v{new_version}: {again[0]['robot_id']} t{again[0]['tick']}")
    print(f"  ok  no {want} under v{new_version}: the fix holds live")
    http.post("/api/chaos", json={"scenario": "clear_floor"}).raise_for_status()  # leave it tidy
    print("END TO END: PASS")


if __name__ == "__main__":
    main()
