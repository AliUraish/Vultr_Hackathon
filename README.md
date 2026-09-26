# Replay: reproducible robot failures on Vultr

A robot fails once → Replay captures it → replays it exactly, on demand → a diagnosis agent proposes fixes → each fix is tried in forked replays → the proven fix ships to the fleet as a new policy version, and the failure stays behind as a regression test. Vultr holds every capsule, replay, fix and decision.

```
Browser (ops app) ◄── WebSocket ──► VM A · control plane (FastAPI + Postgres 16)
                                      ├─ telemetry ingest → append-only event log + per-tick flight recorder
                                      ├─ failure detector (5 rules)      ├─ capsule cutter
                                      ├─ replay job queue (SKIP LOCKED)  ├─ diagnosis agent (LLM | playbook)
                                      ├─ rule-based dispatcher           └─ promotion gate + policy versions
                                            │ fleet API (dispatch, chaos, policy)   ▲ telemetry, replay results
                                            ▼                                        │
                                     VM B · live sim (10 Hz, 4 robots) + N replay workers
```

## How each Track 2 requirement is met

| Requirement | Where |
|---|---|
| Web-based, enterprise-focused | Fleet ops app: live floor, failure inbox, live-vs-replay viewer, fix trials with Approve, regression suite, event-log rewind, policy history |
| Multi-step agentic workflow | failure → capsule → reproduce → diagnose → forked fix trials → regression → human approval → policy push → proof replay (`control/orchestrator.py`) |
| Rule-based workflow | Failure detector (`replay_core/detector.py`), dispatcher (`replay_core/dispatch.py`), promotion gate, orchestrator transitions are all deterministic rules |
| Future of work | Warehouse fleet; people approve fixes and handle exceptions (mislabeled bins, blocked picks) instead of driving robots |
| Production-style web app | FastAPI, Postgres, migrations, WebSockets, sessions, node tokens, systemd, firewall, replayable event log |
| VM backend on Vultr | VM A (control plane + Postgres), VM B (sim + replay workers) |
| Vultr as system of record and control | Every tick, snapshot, capsule, replay, hypothesis, approval and policy version is in Postgres on VM A; every dispatch and replay job is issued by VM A |
| Serverless Inference (optional) | The diagnosis agent speaks the OpenAI-compatible API: OpenAI today, or Vultr Serverless Inference by setting `INFERENCE_PROVIDER=vultr`. With no key it uses a rule-based playbook. Either way, every proposed fix is validated against the DSL and proven by forked replays before a person sees an Approve button |

## Why replays are exact

The sim (`replay_core/`) is pure integer math (millimetres, ticks) over a JSON-only state, with a seeded SplitMix64 RNG stored in that state. Every external input (job commands, chaos, policy changes) is recorded with the tick it was applied on, and replays feed the recorded inputs back without ever re-sensing. A reproduction must match the live run's per-tick state hash, tick for tick. This is tested across processes with different hash seeds (`tests/test_determinism.py`).

A capsule is a snapshot from before the failure, plus every input through T+2 s, plus the live hashes. It reaches back to the failing job's start (up to 60 s), so the cause is always inside it. Fix trials start from the policy the fleet was running at the failure, simulate 40 s past the capsule, and compare against a control run, so a fix that only delays a failure isn't counted as preventing it. A new safety failure (collision, wrong item, zone breach), or any new failure inside the recorded window, blocks a fix; a stall or overdue job that only appears in the 40 s continuation is shown to the approver as a warning.

## Numbers (from the test suite and scenario runs)

- Reproduction: every capsule replays hash-identical to live, 3/3, in 50–100 ms per replay.
- Three canned failures, 8 random runs each: the right fix is proven to avoid the failure in 23/23; a wrong fix is rejected in 20/23 (the misses are genuine timing side effects, not verdict errors).
- Replays are exact across machines: a capsule recorded on VM B (Intel x86, Python 3.12) replays hash-identical on an Apple M4 (ARM, Python 3.11).
- Normal operation: 497 jobs in 100 fleet-minutes, all delivered correctly, with 3 organic failures.
- Live end to end on Vultr with OpenAI diagnosis (`scripts/e2e.py`): all three scenarios go failure → 3/3 exact reproduction → hypotheses → forked trials → approval → fleet hot-reload → clean proof replay → **re-inject the original situation live, and the failure does not recur**.

| Scenario | Failure | Fix the agent should find |
|---|---|---|
| Pallet falls ahead of a fast robot | collision | `speed_cap(racks, 0.5)` |
| Bin gets the wrong item | wrong item at the dock | `require_scan_confirm(<class>)` |
| Worker closes an aisle a robot is routed through | zone breach | `respect_closures(racks)` |

The fix language has six rule types: `speed_cap`, `min_clearance`, `reroute_avoid`, `reorder_steps` and `require_scan_confirm` come from the plan. `respect_closures` (re-plan when an aisle closes; never drive into a closed one) was added after live testing showed that routing around a closed aisle can't stop a robot whose pick is inside it.

"Re-inject live" recreates the original situation rather than a random one: the same aisle and the same gap for a pallet (any rack aisle after 30 s), the same item class for a mislabeled bin, the same aisle for a worker.

## Repo

```
replay_core/   deterministic sim, fix DSL, detector, dispatcher rules, capsules (shared by both VMs)
control/       VM A: FastAPI app, ingest, orchestrator, diagnosis, migrations
simnode/       VM B: live sim + fleet API (app.py), replay worker (worker.py)
web/           ops app (plain JS + canvas, served by VM A)
infra/         deploy.sh, VM bootstrap scripts, systemd units, env templates
tests/         determinism, scenarios, DSL, services
```

## Run the tests

```
uv run --group dev pytest
```

## Deploy (two Ubuntu 24.04 VMs, this machine's SSH key on both)

1. Put `VM_A_IP` and `VM_B_IP` in `.env` (see `.env.example`).
2. `infra/deploy.sh setup`. This installs Postgres 16 and the control plane on A, and the sim plus one replay worker per core on B. It generates the shared node token and the operator password into `infra/.secrets.env` (gitignored).
3. Open `http://VM_A_IP:8000` and sign in as `ops`.

`infra/deploy.sh push` ships code changes, `status` shows health, and `logs a|b` follows the logs. The script only uses SSH; it never calls the Vultr API.

## Demo (4 minutes)

1. Floor: the fleet is running auto-generated jobs. Click **Pallet falls in an aisle**; a fast robot hits it.
2. Failures: the collision appears. Open it. The capsule is cut and replayed on VM B, and the replay matches the live trajectory hash. Click **Replay again** twice: 3/3, same hash.
3. Diagnosis posts three hypotheses. Three forked replays run side by side with one scrubber: the speed cap avoids the crash, and min_clearance still crashes.
4. The regression suite passes. Click **Approve**. The policy goes vN → vN+1, and the robots reload it on the next tick.
5. **Re-inject live**: the robot now brakes in time. The proof replay of the original capsule under the new policy is clean.
6. Event log: drag the rewind slider back to the collision. Every step of the chain is in Postgres on Vultr.
