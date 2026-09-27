"""End-to-end check against a deployed mine: python scripts/e2e.py http://VM_A_IP:8000 [hazard | fault tire|sensor]

Signs in as the controller, checks the AI is dispatching trucks, then either injects a hazard and follows it
through the whole pipeline (capsule -> 3/3 reproduction -> investigation on Vultr inference -> forked trials ->
robustness + regression gate -> approval -> signed policy hot-reloaded by the live fleet -> clean proof replay ->
the original situation re-injected live, and it does not recur), or breaks a truck and follows the service case
(the copilot pulls it over, diagnoses it and pings a person; the fitter repairs it; it hauls again).
Exits non-zero on any miss. SECRETS=infra/.secrets.mining.env for the mine deployment.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

SCENARIO_FAILURE = {"rockfall": "collision", "grade_mixup": "wrong_item", "blast_closure": "zone_breach"}
SECRETS = os.environ.get("SECRETS", "infra/.secrets.env")


def secret(name: str) -> str:
    for line in (Path(__file__).resolve().parents[1] / SECRETS).read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1]
    raise SystemExit(f"{name} missing from {SECRETS}")


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


def ai_is_driving(http: httpx.Client) -> None:
    """The dispatch AI is sending trucks to the excavators (and the traffic AI rules on meetings)."""
    st = http.get("/api/state").json()
    print(f"      AI: {st['ai']['provider']} · {st['ai']['model']}")
    d = wait("the dispatch AI sent a truck to an excavator", lambda: next(
        (x for x in reversed(http.get("/api/decisions").json())
         if x.get("kind") == "dispatch" and x.get("phase") == "done" and x.get("by") == "ai"), None), 120)
    print(f"      {d['robot']} -> face {d['face']} ({d['road_m']} m, {d['ms']} ms, {d['source']}): {d['reason'][:120]}")
    traffic = [x for x in http.get("/api/decisions").json() if x.get("kind") == "traffic" and x.get("phase") == "done"]
    for x in traffic[-3:]:
        print(f"      right of way: {x['first']} first, {x['yield']} yields ({x['by']}, {x['ms']} ms): {x['reason'][:100]}")


def fault(http: httpx.Client, kind: str) -> None:
    before = {c["id"] for c in http.get("/api/service").json()}
    http.post("/api/chaos", json={"scenario": f"{kind}_fault"}).raise_for_status()
    case = wait(f"{kind} fault raised a service case", lambda: next(
        (c for c in http.get("/api/service").json() if c["id"] not in before), None), 120)
    rid, cid = case["robot_id"], case["id"]
    print(f"      case #{cid}: {rid} {case['code']}")
    alert = wait("a person was paged", lambda: next(
        (e for e in http.get("/api/events", params={"type": "service.alert", "limit": 5}).json()
         if e["payload"].get("case") == cid), None), 20)
    print(f"      page: {alert['payload']['message']}")

    def case_now():
        return next(c for c in http.get("/api/service").json() if c["id"] == cid)
    want = ("dispatched", "repairing") if kind == "tire" else ("in_repair",)
    c = wait("the copilot pulled it over, diagnosed it and planned the repair",
             lambda: (x := case_now())["status"] in want + ("resolved",) and x, 240)
    for st in c["steps"]:
        print(f"        [{st.get('by'):>10}] {st.get('title')}  {(st.get('detail') or '')[:90]}")
    if c["status"] != "resolved":
        wait("fitter on site", lambda: case_now()["status"] in ("repairing", "in_repair", "resolved"), 240)
        http.post(f"/api/service/{cid}/done", json={}).raise_for_status()
        print("  ok  controller marked the repair done")
    wait("case resolved", lambda: case_now()["status"] == "resolved", 90)
    wait(f"{rid} hauling again" if kind == "tire" else f"{rid} parked as the new standby",
         lambda: next(r for r in http.get("/api/state").json()["frame"]["robots"] if r["id"] == rid)["st"]
         not in ("fault",), 60)
    print(f"FAULT PIPELINE ({kind}): PASS")


def main() -> None:
    base = sys.argv[1].rstrip("/")
    scenario = sys.argv[2] if len(sys.argv) > 2 else "rockfall"
    http = httpx.Client(base_url=base, timeout=30.0)
    http.post("/api/login", json={"username": "ops", "password": secret("ADMIN_PASSWORD")}).raise_for_status()
    print(f"signed in to {base}")

    st = wait("live sim streaming", lambda: (s := http.get("/api/state").json())["sim_online"] and s, 90)
    t_before = st["tick"]
    policy_before = st["policy"]["version"]
    print(f"      run {st['run_id']}, tick {t_before}, policy v{policy_before}, {len(st['jobs'])} open loads")
    wait("ticks advancing", lambda: http.get("/api/state").json()["tick"] > t_before + 20, 15)
    ai_is_driving(http)
    if scenario == "fault":
        fault(http, sys.argv[3] if len(sys.argv) > 3 else "tire")
        return

    http.post("/api/chaos", json={"scenario": "clear_roads"}).raise_for_status()  # start from clear roads
    time.sleep(1)
    known = {f["id"] for f in http.get("/api/failures").json()}
    r = http.post("/api/chaos", json={"scenario": scenario})
    r.raise_for_status()
    print(f"  ok  chaos {scenario} armed: {r.json()}")
    want = SCENARIO_FAILURE[scenario]

    def new_failure():
        return next((f for f in http.get("/api/failures").json()
                     if f["id"] not in known and f["type"] == want), None)
    f = wait(f"{want} detected by the rules", new_failure, 360 if scenario == "grade_mixup" else 240)
    fid = f["id"]
    print(f"      failure #{fid}: {f['type']} {f['robot_id']} at tick {f['tick']}")

    def detail():
        return http.get(f"/api/failures/{fid}").json()
    wait("capsule cut", lambda: detail()["capsule"], 30)
    wait("first replay reproduced it", lambda: any(
        x["kind"] == "reproduce" and x["status"] == "done" for x in detail()["replays"]), 90)
    for _ in range(2):
        http.post(f"/api/failures/{fid}/replay", json={}).raise_for_status()
    def three_reproductions():
        done = [x for x in detail()["replays"] if x["kind"] == "reproduce" and x["status"] == "done"]
        return done if len(done) >= 3 else None
    reps = wait("3 reproductions done", three_reproductions, 90)
    hashes = {x["trajectory_hash"] for x in reps}
    good = [x for x in reps if x["outcome"] == "reproduced" and x["matches_live"]]
    print(f"      reproduced {len(good)}/{len(reps)}, distinct trajectory hashes: {len(hashes)} ({next(iter(hashes))[:12]})")
    if len(good) != len(reps) or len(hashes) != 1:
        raise SystemExit("FAIL  reproduction is not exact")

    d = wait("investigation finished, fixes posted", lambda: (x := detail())["hypotheses"] and x, 600)
    inv = d.get("investigation") or {}
    rep = inv.get("report") or {}
    print(f"      investigation #{inv.get('id')} by {inv.get('source')}: {len(inv.get('steps', []))} steps, "
          f"{inv.get('sims')} simulations")
    for s in inv.get("steps", []):
        print(f"        {s['n']:>2}. {s['tool']:<16} {(s['summary'] or '')[:110]}")
    for r in rep.get("rejected", []):
        print(f"        rejected {r['fix']}: {r['reason'][:90]}")
    d = wait("every trial finished and gated",
             lambda: (x := detail())["failure"]["status"] in ("awaiting_approval", "no_fix") and x, 300)
    latest = max(h["round"] for h in d["hypotheses"])
    for h in [h for h in d["hypotheses"] if h["round"] == latest]:
        trial = next((x for x in d["replays"] if x["hypothesis_id"] == h["id"] and x["kind"] == "trial"), {})
        rob = (h.get("evidence") or {}).get("robustness")
        print(f"        #{h['rank']} {h['fix_dsl']:<40} trial={trial.get('outcome')} "
              f"robust={'–' if rob is None else f'{rob:.0%}'} status={h['status']}")
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
        (x for x in detail()["replays"] if x["kind"] == "proof" and x["status"] == "done"), None), 90)
    print(f"      proof replay #{proof['id']}: {proof['outcome']}")
    suite = http.get("/api/regression").json()
    assert any(k["failure_id"] == fid for k in suite), "capsule missing from regression suite"
    print(f"  ok  capsule in regression suite ({len(suite)} total)")
    if proof["outcome"] != "avoided":
        raise SystemExit("FAIL  proof replay is not clean")

    # Live proof: recreate the original situation (same bench and gap / material / closed road) under the fix.
    last_event = http.get("/api/events", params={"limit": 1}).json()[0]["id"]
    before = max(x["id"] for x in http.get("/api/failures").json())
    r = http.post(f"/api/failures/{fid}/reinject", json={})
    r.raise_for_status()
    print(f"  ok  re-injected live with hints {r.json().get('hints')}")

    def fired():
        evs = http.get("/api/events", params={"limit": 50}).json()
        new = [e for e in evs if e["id"] > last_event]
        if any(e["type"] == "sim.chaos_expired" for e in new):
            raise SystemExit("FAIL  re-inject expired: no truck got into the original position")
        return next((e for e in new if e["type"] == "input.chaos" and e["payload"].get("scenario") == scenario), None)
    e = wait("original situation recreated in the live pit", fired, 150)
    print(f"      t{e['tick']}: {e['payload'].get('type')} {e['payload'].get('cell') or e['payload'].get('slot') or e['payload'].get('zone')}"
          f" target {e['payload'].get('target')} gap {e['payload'].get('gap_mm', '-')} mm")
    time.sleep(180 if scenario == "grade_mixup" else 40)  # a mis-tagged load only shows at the dump
    again = [x for x in http.get("/api/failures").json() if x["id"] > before and x["type"] == want]
    if again:
        raise SystemExit(f"FAIL  {want} happened again under v{new_version}: {again[0]['robot_id']} t{again[0]['tick']}")
    print(f"  ok  no {want} under v{new_version}: the fix holds live")
    http.post("/api/chaos", json={"scenario": "clear_roads"}).raise_for_status()  # leave it tidy
    print("END TO END: PASS")


if __name__ == "__main__":
    main()
