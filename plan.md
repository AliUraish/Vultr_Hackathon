Replay — reproducible robot failures on Vultr

Pitch: robot fails once → we capture it → replay it exactly on demand → an agent tries fixes in parallel replays → the proven fix ships to the fleet with the failure kept as a regression test. Vultr holds every capsule, replay, fix, and decision.

How each Track 2 requirement is satisfied
Requirement	Where it lives
Web-based, enterprise-focused	Fleet ops web app: live floor, failure inbox, replay viewer, fix approval
Multi-step agentic workflow	Failure → capsule → replay → hypothesis → forked fix trials → verify → promote
Rule-based workflow	Failure detector, dispatcher, promotion gate are deterministic rules
Future-of-work use case	Warehouse/factory fleet; humans approve fixes and answer edge cases instead of driving robots
Production-style web app	FastAPI + Postgres + WebSockets, auth, replayable event log
VM backend on Vultr (mandatory)	VM A: control plane. VM B: sim + replay workers
Vultr as system of record and control	Postgres 16 on VM A stores telemetry, capsules, fixes, policy versions, approvals; all dispatch and replay jobs are issued from VM A
Serverless Inference (optional)	Diagnosis agent: OpenAI (team's free credits) via the OpenAI-compatible API; Vultr Serverless Inference is a one-line switch (INFERENCE_PROVIDER=vultr); rule-based playbook when no key
NetBird (track partner, bonus)	Private mesh VM A ↔ VM B and zero-inbound-port access to the app

Where each thing lives (decided 2026-09-26, revised for zero extra Vultr spend)
Everything runs on Vultr; nothing runs locally except code editing and sim unit tests.
Constraint: no paid Vultr usage beyond the two mandatory VMs, and no Vultr API calls without explicit go-ahead.
Thing	Where	Why
Database (events, ticks, snapshots, capsules, hypotheses, replays, policy versions, jobs)	Postgres 16 on VM A	Free; "Vultr as system of record"
Control plane (API, dispatcher, detector, capsule cutter, replay queue, promotion gate, web app)	VM A	Mandatory VM backend; "Vultr as control"
Sim + replay workers	VM B	Parallel fork trials
Capsule blobs	Postgres (JSONB, < 1 MB each)	Object Storage dropped: extra monthly cost
Diagnosis agent	OpenAI with the team's free credits; playbook fallback; Vultr Serverless Inference if credits appear	Vultr inference is pay-per-token ($0.55/M in, $2.75/M out); only the LLM call leaves Vultr
VM A ↔ VM B + app ingress	NetBird (bonus, time-boxed); Vultr VPC as fallback	Track partner; no inbound app ports
Web app hosting	Served by VM A	Keeps the all-Vultr story
Code / deploys / secrets	GitHub repo; rsync or git pull + systemd restart; env files on the VMs	Nothing committed
Not used: Neon, Vercel, Vultr Managed DB, Vultr Object Storage, local Docker/Postgres.
Sim: deterministic 2D integer-math sim (plan's fallback) instead of PyBullet — bit-exact across processes, 50–100 ms per replay.
Architecture
Browser (ops app) ◄─WS/SSE─► VM A: Control plane (FastAPI + Postgres 16)
                                 ├─ telemetry ingest + event log
                                 ├─ failure detector (rules)
                                 ├─ capsule cutter
                                 ├─ replay job queue
                                 ├─ diagnosis agent ──► Vultr Serverless Inference
                                 ├─ promotion gate + fleet policy versions
                                 └─ NetBird routes (bonus)
                                        │ WireGuard mesh
                                        ▼
                              VM B: Sim + replay workers
                                 ├─ live sim (PyBullet, fixed timestep, 3–5 robots)
                                 ├─ replay worker pool (N deterministic sim instances)
                                 └─ fleet API: goto / pick / drop / scan per robot
The pipeline, stage by stage

1. Live operation
Jobs enter via the web app. A rule-based dispatcher assigns steps to robots by capability and proximity. Robots execute in the live sim; every tick's inputs and outputs (pose, commands, sensor reads, RNG seed, map state) stream to Postgres. This is the normal fleet loop and it's also the flight recorder.

2. Failure detection (rules, not LLM)
Collision event, stall > N ticks, task overdue, wrong-item scan, rule-zone breach. Any trigger emits a failure event with a type and timestamp T.

3. Capsule cut
The cutter pulls the full sim state snapshot nearest T−10 s plus every recorded input from that snapshot to T+2 s. Serialized, hashed, stored in Postgres on VM A. A capsule is the unit of reproducibility.

4. Deterministic replay
A replay worker loads the snapshot, feeds the recorded inputs at fixed timestep with the recorded seed. Same failure, every run. The web app shows live vs. replay side by side; the ops person clicks "replay" three times to prove it.

5. Diagnosis agent (Vultr inference)
Input: failure type, capsule summary, the last 50 events, current fleet policy. Output, strict JSON: ranked hypotheses, each with a concrete fix in a small DSL — speed_cap(zone, 0.5), min_clearance(0.4), reroute_avoid(cell), reorder_steps(job, …), require_scan_confirm(item_class). Validated before use.

6. Forked fix trials
For each hypothesis (cap 3), a replay worker loads the same capsule with the fix applied and replays. Deterministic outcome: failure reproduced or avoided. Also run the capsule against the last 10 stored capsules (regression) so a fix doesn't break earlier ones.

7. Verification + promotion (rules + human)
Fix passes its capsule and the regression set → it's queued for approval. Human approves in the app → fleet policy version increments, all robots pick it up next tick. The capsule joins the permanent regression suite. Every step is an event in the log.

8. Proof
Re-inject the original scenario live → doesn't fail. Replay the capsule against the new policy → clean. Both results stored with the policy version hash.

Data model (Postgres 16 on VM A)
events(id, ts, robot_id, type, payload) — append-only, everything
snapshots(id, ts, state_blob_ref, seed) — periodic full sim state
capsules(id, failure_event_id, snapshot_id, input_range, hash)
hypotheses(id, capsule_id, rank, cause, fix_dsl)
replays(id, capsule_id, hypothesis_id?, policy_version, outcome, hash)
policy_versions(id, parent_id, fix_dsl, approved_by, hash)
jobs / steps / assignments — the normal fleet tables

Projections (live fleet state, failure inbox) are rebuilt from events, which is what makes the whole system replayable and what makes Vultr the system of record in a defensible sense.

Web app (keep it thin)
Live floor map with robots and job progress
Failure inbox: type, robot, time, "replay" button
Replay viewer: live vs. replay side by side, scrubber
Fix trials: three replays in a row, pass/fail, approve button
Regression suite: list of capsules with current status
No analytics dashboard. The map and the replay viewer are the hero.
Two-day plan (4 people)

Day 1

0–3h · A: PyBullet world, fixed timestep, seeded RNG, 1 robot, A* nav, fleet API. B: VM setup, Postgres, event schema, telemetry ingest. C: web app skeleton, live map over WebSocket. D: dispatcher + job intake.
3–7h · A: periodic snapshot + restore that reproduces motion exactly (the linchpin — test determinism early). B: failure detector + capsule cutter. C: failure inbox + replay viewer. D: 3 robots, contention, chaos injection endpoint.
7–12h · All: one full loop — job → chaos → failure → capsule → replay reproduces. Stop here for the night with that working.

Day 2

0–4h · A: replay worker pool + fix DSL applied at load. B: diagnosis agent via Vultr inference, JSON schema. C: fix-trials UI + approve flow. D: policy versions + fleet hot-reload.
4–7h · Regression run across stored capsules; promotion gate; NetBird zero-port routes for the app (bonus).
7–10h · Seed 3 canned failure scenarios; rehearse the demo twice end to end.
10–12h · Cut whatever flaked; README with architecture and the "reproduced 3/3, fixed 1/3, regression 10/10" numbers.
Demo (4 minutes)
Fleet running. Inject a corridor blockage; robot 2 collides.
Failure inbox lights up. Click replay — crash. Replay again — same crash. Again — same.
Diagnosis agent posts three hypotheses. Three forked replays run side by side; one avoids the crash.
Regression check passes on the stored capsules. Approve. Policy v7 → v8.
Re-inject the blockage live: robot reroutes. Replay the original capsule under v8: clean.
Scrub the event log back to the collision to show the whole chain is stored on Vultr.
Risks and fallbacks
Determinism is the single make-or-break. Fixed step, seeded RNG, record every external input, never re-sense during replay. Verify by hashing the state trajectory across two replays on day 1 morning. If PyBullet drifts, drop to a 2D kinematic sim — fully deterministic, still convincing.
Keep capsules small (10–12 s) so replays run in under 5 s.
If the diagnosis agent's fixes are weak, pre-seed the DSL with 5 obvious fix types; the agent chooses parameters. Still agentic, far more reliable on stage.
NetBird is bonus; time-box to 2 hours.