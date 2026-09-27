# Replay: reproducible robot failures on Vultr

A robot fails once → Replay captures it → replays it exactly, on demand → an **incident investigator** agent works the incident entirely in simulation (reproduces it, isolates the cause, runs what-ifs, stress-tests candidate fixes on dozens of variants of the incident and tunes them for the least throughput cost) → the fixes that hold up go through the exact-incident trial, the regression suite and a human approval → the fix ships to the fleet as a new policy version, and the failure stays behind as a regression test. Vultr holds every capsule, experiment, fix and decision.

```
Browser (ops app) ◄── WebSocket ──► VM A · control plane (FastAPI + Postgres 16)
                                      ├─ telemetry ingest → append-only event log + per-tick flight recorder
                                      ├─ failure detector (5 rules)      ├─ capsule cutter
                                      ├─ replay job queue (SKIP LOCKED)  ├─ incident investigator (OpenAI tool calling | scripted)
                                      ├─ experiment lab (variants, ddmin) ├─ promotion gate + policy versions
                                      └─ rule-based dispatcher
                                            │ fleet API (dispatch, chaos, policy)   ▲ telemetry, replay results
                                            ▼                                        │
                                     VM B · live sim (10 Hz, 4 robots) + N replay/experiment workers
```

## How each Track 2 requirement is met

| Requirement | Where |
|---|---|
| Web-based, enterprise-focused | Fleet ops app: live floor, incident inbox, live-vs-replay viewer, live investigation trace, variant grid and safety-vs-throughput curve, fix trials with Approve, regression suite, event-log rewind, policy history |
| Multi-step agentic workflow | failure → capsule → reproduce → **investigator agent** (up to 12 tool calls: reproduce, isolate_cause, what_if, stress_test, tune_fix, submit_findings; every call carries a `why`) → forked fix trials → robustness + regression gate → human approval → policy push → proof replay (`control/orchestrator.py`, `control/investigator.py`) |
| Rule-based workflow | Failure detector (`replay_core/detector.py`), dispatcher (`replay_core/dispatch.py`), promotion gate, orchestrator transitions are all deterministic rules |
| Future of work | A robotic fulfilment centre run by a (generated) company: customer orders drive the robots, people approve fixes and handle exceptions, and every incident is priced in dollars against the throughput a fix costs |
| Production-style web app | FastAPI, Postgres, migrations, WebSockets, sessions, node tokens, systemd sandboxing, firewall, Ed25519-signed policies, Merkle-chained audit ledger witnessed by a second VM, live platform telemetry |
| VM backend on Vultr | VM A (control plane + Postgres), VM B (sim + replay workers) |
| Vultr as system of record and control | Every tick, snapshot, capsule, replay, investigation step, experiment, hypothesis, approval and policy version is in Postgres on VM A; every dispatch, replay and experiment job is issued by VM A and runs on VM B |
| Serverless Inference (optional) | The investigator drives OpenAI's Responses API (reasoning model + function tools) today; `INFERENCE_PROVIDER=vultr` switches it to Vultr Serverless Inference over the OpenAI-compatible Chat Completions API. With no key, or if the model fails, a scripted investigator runs the same method. Either way every proposed fix is validated against the DSL and proven by simulation before a person sees an Approve button |

## Why replays are exact

The sim (`replay_core/`) is pure integer math (millimetres, ticks) over a JSON-only state, with a seeded SplitMix64 RNG stored in that state. Every external input (job commands, chaos, policy changes) is recorded with the tick it was applied on, and replays feed the recorded inputs back without ever re-sensing. A reproduction must match the live run's per-tick state hash, tick for tick. This is tested across processes with different hash seeds (`tests/test_determinism.py`).

A capsule is a snapshot from before the failure, plus every input through T+2 s, plus the live hashes. It reaches back to the failing job's start (up to 60 s), so the cause is always inside it. Fix trials start from the policy the fleet was running at the failure, simulate 40 s past the capsule, and compare against a control run, so a fix that only delays a failure isn't counted as preventing it. A new safety failure (collision, wrong item, zone breach), or any new failure inside the recorded window, blocks a fix; a stall or overdue job that only appears in the 40 s continuation is shown to the approver as a warning.

## Live 3D floor

The landing page renders the live fleet from VM B in three.js (`web/js/three/`), built from the sim's own map at 1 unit = 1 m and interpolated from the 10 Hz frames: racks full of cartons, wall shelving, white lane lines, loading counters with LED unit counters at the dock doors, and yellow-and-black trailers whose cargo appears and disappears on real pick and delivery events. Coordination is visible as it happens: each robot's reserved cells glow, planned routes scroll along the floor, a robot holding for another is linked to it, standoffs and the arbiter's ruling (who yields, by which rule) are called out in the scene and on the *Fleet radio*. The arbiter is the fleet's deterministic traffic rule, not the model, so a collision call is instant and replayable; the sim emits it as `wait` / `standoff` / `yield` / `resume` events without changing the simulation.

Tap a trailer (or its chip) and the camera flies to it and follows it as a chase cam while it keeps working. The panel shows what it is doing now, its sensors (speed vs the zone's cap, forward sensor, stopping distance vs sensor range, zone, reserved cells, odometer), its load with product names, the job, order and customer, and a live log that includes its side of every hold, standoff and ruling. **Ask** (or the ✦ pin above the robot) opens the operations copilot scoped to that robot or to the whole site; it answers from live telemetry, the event log, orders and the fleet policy (`control/assistant.py`), can't command robots, and every question and answer goes to the event log.

## Trailer faults and recovery

Inject **Flat tire** or **Lidar fault** on a working trailer (Live → Inject). The trailer safety-stops, hands its job back to the fleet, raises an alarm code and starts pinging (red sonar rings under it, amber beacon). A service case opens (`control/service.py`) and the copilot runs the recovery, one validated decision at a time:

1. **Pull over.** The rules compute free cells within reach (not a rack, dock, pallet, closed aisle, or another robot's reserved or planned cells) and score them (left/right of the lane and off the traffic lanes first). The copilot picks one and says why. The sim drives it there at limp speed (0.15 m/s on a flat).
2. **Diagnose.** The copilot reads the health telemetry every trailer streams (four tire pressures, wheel slip, drive-current imbalance, vibration, lidar return rate and range) and names the fault and component. A rule reading is kept alongside and wins if they disagree; a tire fault is never driven.
3. **Recover.** *Sensor fault:* the copilot takes remote control, drives it to a free garage bay at 0.4 m/s (it still obeys the traffic rules and the arbiter), and deploys the standby trailer. A technician recalibrates it and it becomes the new standby. *Tire fault:* the copilot picks a technician from the site's floor team and writes the work order. The technician walks over (you see them), replaces the tire and marks it done; the trailer rejoins the fleet. A capacity rule keeps four trailers working whenever a healthy standby is parked.

Every step is on the case (who decided: copilot, rules, technician), in the robot's live log and in the event log. With the model off, the rules make each decision. The whole pipeline ran live on Vultr: tire (right-rear, 2.4k tokens, ~40 s) and lidar (garage + spare, 2.3k tokens, ~60 s).

Each trailer's next pick is outlined on the shelf in its colour, with the product's EAN-13 barcode (a real, scannable encoding of a stable GTIN per SKU); the panel shows the barcodes of the boxes on board.

## The enterprise it runs

Each database gets its own company and site, written once by the model against a strict JSON schema (`control/enterprise.py`) from a randomly chosen industry and city: the facility, one real-looking product per rack slot that fits the slot's handling class (look-alike parts in the small-parts racks), customers with service tiers, the carrier at each dock door, the robot asset register, the floor team and a cost model. A reset produces a different one. The live deployment is currently *Canyon Hearth Hardware · West Bench Robotic Fulfilment Centre (SLC-2), Salt Lake City*, 48 SKUs from cordless drill kits to cast-iron sinks, 10 trade customers, 3 carriers. With no key, a seeded generator writes the same shape.

Customer orders are released continuously from that profile, become pick jobs for the robots, and are closed by fleet telemetry (shipped, short-shipped, exception). The floor shows live KPIs (orders/hour, value shipped, on-time picks, time since the last safety incident) and the order book; an incident shows the order it hit, what was ordered vs picked, and its estimated cost. The investigator gets the same numbers, so it can weigh one incident's cost against the orders per hour a fix gives up.

## Integrity and scale

- **Signed policies.** The control plane signs every policy version with Ed25519; the key is generated on VM A and never leaves it. VM B holds only the public key and refuses to start or hot-reload a policy whose signature does not verify (tested live: unsigned → 403, forged → 403), so a leaked node token cannot change robot behaviour.
- **Audit ledger.** A sealer groups events into blocks: each event gets a sequence number and a leaf hash of its canonical content, each block a Merkle root, and blocks chain by hash (`control/ledger.py`). Every block head is also sent to VM B, which keeps its own append-only copy and refuses a different hash for a block it has seen. **Verify integrity** on the Event log page re-hashes everything and compares with VM B; clicking any event shows its Merkle inclusion proof.
- **Platform page.** A live view of both VMs: what runs where, telemetry and job flows, host load, the sim's tick-time budget, Postgres size, per-worker throughput and utilisation, queue latency by job kind, and a capacity model from measured throughput (workers are stateless and claim with `SKIP LOCKED`, so adding a VM B node adds capacity linearly; the model shows simulations/s, stress-test time and cost per hour for 1–10 nodes).

## The incident investigator

An agent that works an incident the way a reliability engineer would, but only through deterministic simulations, so it never touches the live fleet and every number it reports can be re-run. Its tools (`control/lab.py`, `replay_core/experiments.py`):

| Tool | What it simulates |
|---|---|
| `reproduce` | the capsule N times on the workers: identical trajectory hashes, matching the live run |
| `isolate_cause` | delta debugging over the recorded jobs and chaos events: the smallest set that still causes the failure |
| `what_if` | the exact incident with extra rules in force (a counterfactual, one simulation) |
| `stress_test` | a fix against 30–60 **variants** of the incident (the hazard at other gaps, times, aisles and robots; new pick timings) vs today's policy: share of variants that stay safe, failures prevented or introduced, throughput cost |
| `tune_fix` | a numeric fix at a range of settings: the safety/throughput curve and the cheapest setting that clears the gate |
| `submit_findings` | root cause, evidence citing experiment numbers, up to 3 ranked fixes, rejected fixes with reasons |

The gate adds robustness to the old checks: a fix must avoid the exact incident, keep **≥ 95% of variants safe**, and keep the regression suite clean before the Approve button appears. The operator can challenge the agent from the same page by running any of the tools on their own fix.

## Numbers (from the test suite and scenario runs)

- Reproduction: every capsule replays hash-identical to live, 3/3, in 50–100 ms per replay.
- Three canned failures, 8 random runs each: the right fix is proven to avoid the failure in 23/23; a wrong fix is rejected in 20/23 (the misses are genuine timing side effects, not verdict errors).
- Replays are exact across machines: a capsule recorded on VM B (Intel x86, Python 3.12) replays hash-identical on an Apple M4 (ARM, Python 3.11).
- Normal operation: 497 jobs in 100 fleet-minutes, all delivered correctly, with 3 organic failures.
- Live end to end on Vultr with OpenAI diagnosis (`scripts/e2e.py`): all three scenarios go failure → 3/3 exact reproduction → hypotheses → forked trials → approval → fleet hot-reload → clean proof replay → **re-inject the original situation live, and the failure does not recur**.

- Stress tests (30 variants, today's policy vs the fix):

| Scenario | Failure | Fails today | Fix the investigator picks | Variants safe | Throughput cost | Rejected (variants safe) |
|---|---|---|---|---|---|---|
| Pallet falls ahead of a fast robot | collision | 25/30 | `speed_cap(racks, 0.6)` (tuned) | 30/30 | 22% | `speed_cap(racks, 0.8)` 93%, `min_clearance(0.4)` 17%, `reroute_avoid(<cell>)` 0% |
| Bin gets the wrong item | wrong item at the dock | 30/30 | `require_scan_confirm(loose_small)` | 30/30 | 5% | `reorder_steps(*, nearest_first)` 0% |
| Worker closes an aisle a robot is routed through | zone breach | 29/29 | `respect_closures(racks)` | 29/29 | none (−2%) | `reroute_avoid(aisle_7E)` 0%, `speed_cap(aisle_7E, 0.3)` 0% |

- Live end to end on Vultr with the investigator (gpt-6-sol), all three scenarios PASS: failure → 3/3 exact reproduction → investigation (36–40 s, 310–541 simulations each) → robustness gate → approval → signed policy verified and hot-reloaded by VM B → clean proof replay → the original situation re-injected live, and the failure does not recur.

| Incident | Investigator's fix | Variants safe | Rejected with evidence |
|---|---|---|---|
| Pallet falls ahead of R4 | `speed_cap(racks, 0.6)` | 60/60 | `speed_cap(racks, 0.7)` 87%, `min_clearance(0.8)` 25%, `reroute_avoid(<cell>)` 0% |
| Fragile bin mislabeled | `require_scan_confirm(*)` | 60/60 | speed caps (40% at 0.4 m/s; 0.2 m/s works but costs 56% throughput) |
| Worker closes aisle 3E | `respect_closures(*)` | 60/60 | `speed_cap(racks, 0.1)` 65%, `reroute_avoid(aisle_3E)` 0% |
- A variant simulation takes 15–30 ms; a 30-variant stress test takes about 1–2 s on VM B's two workers, a 9-point tune about 9 s.

The fix language has six rule types: `speed_cap`, `min_clearance`, `reroute_avoid`, `reorder_steps` and `require_scan_confirm` come from the plan. `respect_closures` (re-plan when an aisle closes; never drive into a closed one) was added after live testing showed that routing around a closed aisle can't stop a robot whose pick is inside it.

"Re-inject live" recreates the original situation rather than a random one: the same aisle and the same gap for a pallet (any rack aisle after 30 s), the same item class for a mislabeled bin, the same aisle for a worker.

## Repo

```
replay_core/   deterministic sim, fix DSL, detector, dispatcher rules, capsules, experiments (shared by both VMs)
control/       VM A: FastAPI app, ingest, orchestrator, investigator, lab, diagnosis, migrations
simnode/       VM B: live sim + fleet API (app.py), replay and experiment worker (worker.py)
web/           ops app (plain JS + canvas, served by VM A)
infra/         deploy.sh, VM bootstrap scripts, systemd units, env templates
tests/         determinism, scenarios, DSL, services, experiments, investigator
```

## Run the tests

```
uv run --group dev pytest
```

## Deploy (two Ubuntu 24.04 VMs, this machine's SSH key on both)

1. Put `VM_A_IP` and `VM_B_IP` in `.env` (see `.env.example`).
2. `infra/deploy.sh setup`. This installs Postgres 16 and the control plane on A, and the sim plus one replay worker per core on B. It generates the shared node token and the operator password into `infra/.secrets.env` (gitignored), and the policy signing key on VM A (`harden`).
3. Open `http://VM_A_IP:8000` and sign in as `ops`.

`infra/deploy.sh push` ships code changes, `status` shows health, and `logs a|b` follows the logs. The script only uses SSH; it never calls the Vultr API.

## Demo (4 minutes)

Start from a clean floor: `CONFIRM=RESET infra/deploy.sh reset` wipes the database, has the model generate a new company and site, and restarts on policy v1 with an empty ledger.

1. **Floor**: the generated company's orders are driving the robots; KPIs and the order book update live (hover a bin for its SKU). Click **Pallet falls in an aisle**; a fast robot hits it.
2. **Incidents**: open the collision: the order it hit, the customer and the estimated cost. The capsule is cut and replayed on VM B; the replay matches the live trajectory hash.
3. **Investigation** (live, about a minute): watch the agent reproduce it 3×, isolate the cause, try `speed_cap(racks, 0.8)`, see it fail the 95% gate on the variant grid, tune it on the safety-vs-throughput curve, and settle on 0.6 m/s. Click any red variant to watch it with and without the fix.
4. Challenge it: type `speed_cap(racks, 0.8)` in the form and hit **Stress test**, and the red tiles appear.
5. **Fixes under test**: the recommended fix passes the exact-incident trial, the robustness gate and the regression suite. Click **Approve → ship**. The policy goes vN → vN+1 and the robots reload it on the next tick.
6. **Re-inject live**: the same situation on the live floor, and the robot now brakes in time. The proof replay of the original capsule is clean.
7. **Event log**: **Verify integrity**: every event re-hashed, every Merkle root and block link recomputed, block heads matched against VM B. Click an event for its inclusion proof.
8. **Platform**: both VMs live, the job flows, worker utilisation, and the capacity slider for scaling out.
