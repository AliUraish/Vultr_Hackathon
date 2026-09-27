"""Sim node entry point on VM B:  uvicorn simnode.app:app --host 0.0.0.0 --port 8100

Runs the live haul fleet at a fixed 10 Hz, exposes the fleet API the control plane
dispatches through (goto / load / dump / grade check, hazards, right-of-way rulings,
service moves, policy hot-reload), and streams every tick to the control plane. Telemetry is buffered, so a control
plane restart does not punch holes in the flight recorder.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import time
from collections import deque
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from replay_core.hoststats import host_stats
from replay_core.live import LiveSim
from replay_core.policy import PolicyError, make_policy
from replay_core.scenarios import SCENARIOS, check_hints
from replay_core.signing import SignatureError, Verifier
from replay_core.state import state_hash
from replay_core.world import MAP_HASH, TICK_HZ, W

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("replay.simnode")

OPS = ("goto", "pick", "drop", "scan")
BATCH_TICKS = 50
MAX_BUFFER_TICKS = 10 * 60 * TICK_HZ  # 10 minutes of telemetry while the control plane is away


class Witness:
    """VM B's own append-only copy of the audit ledger's block heads (see control/ledger.py)."""

    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self.blocks: dict[int, dict] = {}
        if self.path and self.path.exists():
            for line in self.path.read_text().splitlines():
                with contextlib.suppress(ValueError, KeyError):
                    b = json.loads(line)
                    self.blocks[int(b["n"])] = b

    def add(self, block: dict) -> bool:
        n = int(block["n"])
        seen = self.blocks.get(n)
        if seen is not None:
            if seen["hash"] != block["hash"]:
                raise ValueError(f"block {n} was already witnessed with a different hash")
            return False
        rec = {"n": n, "hash": block["hash"], "merkle_root": block.get("merkle_root"),
               "prev_hash": block.get("prev_hash"), "events": block.get("events"), "at": time.time()}
        if self.path:  # on disk first: a head only counts as witnessed once it is durable
            with self.path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
        self.blocks[n] = rec
        return True


class Node:
    def __init__(self, control_url: str, token: str, seed: int,
                 transport: httpx.AsyncBaseTransport | None = None, verifier: Verifier | None = None,
                 witness: Witness | None = None) -> None:
        self.http = httpx.AsyncClient(base_url=control_url, headers={"X-Node-Token": token}, timeout=10.0,
                                      transport=transport)
        self.verifier = verifier
        self.witness = witness or Witness(None)
        self.tick_ms: deque[float] = deque(maxlen=600)
        self.late_ticks = 0
        self.rejected_policies = 0
        self.seed = seed
        self.run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{seed % 10000:04d}"
        self.sim: LiveSim | None = None
        self.outbox: deque[dict] = deque()
        self.snapshots: deque[dict] = deque()
        self.notices: list[dict] = []
        self.frame: dict | None = None
        self.dropped = 0

    async def hello(self) -> None:
        """Register the run and get the fleet policy to start with. Retries until VM A answers."""
        delay = 1.0
        while True:
            try:
                r = await self.http.post("/api/node/hello",
                                         json={"run_id": self.run_id, "seed": self.seed, "map": MAP_HASH})
                r.raise_for_status()
                pol = r.json()["policy"]
                if self.verifier:  # never start the fleet on rules the control plane did not sign
                    self.verifier.verify(pol["version"], pol["rules"], pol.get("signature"))
                self.sim = LiveSim(self.seed, pol["rules"], pol["version"])
                log.info("run %s started, seed %s, policy v%s", self.run_id, self.seed, pol["version"])
                return
            except (httpx.HTTPError, KeyError, SignatureError) as exc:
                log.warning("control plane not ready (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)

    def tick_once(self) -> None:
        assert self.sim is not None
        t0 = time.perf_counter()
        rec = self.sim.tick()
        self.tick_ms.append((time.perf_counter() - t0) * 1000)
        self.frame = rec.frame
        self.outbox.append({"tick": rec.tick, "hash": rec.hash, "inputs": rec.inputs, "frame": rec.frame})
        if rec.snapshot is not None:
            self.snapshots.append({"tick": rec.snapshot["tick"], "hash": state_hash(rec.snapshot),
                                   "state": rec.snapshot})
        for req in self.sim.expired:
            where = f" ({', '.join(f'{k}={v}' for k, v in req.hints.items())})" if req.hints else ""
            self.notices.append({"type": "chaos_expired", "tick": rec.tick, "request": req.id,
                                 "scenario": req.scenario, "hints": req.hints,
                                 "message": f"{req.scenario}: no truck got into position{where}"})
        self.sim.expired.clear()
        while len(self.outbox) > MAX_BUFFER_TICKS:
            self.outbox.popleft()
            self.dropped += 1

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        period = 1 / TICK_HZ
        due = loop.time()
        while True:
            self.tick_once()
            due += period
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                self.late_ticks += 1
                if delay < -1.0:  # fell far behind (e.g. the VM paused): don't try to catch up
                    due = loop.time()
                await asyncio.sleep(0)

    async def send_once(self) -> bool:
        """Deliver the oldest buffered telemetry. False if the control plane didn't take it."""
        assert self.sim is not None
        if not (self.outbox or self.snapshots or self.notices):
            return True
        ticks = [self.outbox[i] for i in range(min(BATCH_TICKS, len(self.outbox)))]
        snaps = list(self.snapshots)
        notices = list(self.notices)
        body = {"run": {"id": self.run_id, "seed": self.seed}, "ticks": ticks, "snapshots": snaps,
                "notices": notices, "policy_version": self.sim.state["policy"]["version"], "host": self.stats()}
        try:
            r = await self.http.post("/api/node/telemetry", json=body)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("telemetry not delivered (%d ticks buffered): %s", len(self.outbox), exc)
            return False
        for _ in ticks:
            self.outbox.popleft()
        for _ in snaps:
            self.snapshots.popleft()
        del self.notices[:len(notices)]
        return True

    def stats(self) -> dict:
        ms = sorted(self.tick_ms)
        pick = (lambda q: round(ms[min(len(ms) - 1, int(q * len(ms)))], 3)) if ms else (lambda q: None)
        return {**host_stats(), "tick_ms_p50": pick(0.5), "tick_ms_p99": pick(0.99), "late_ticks": self.late_ticks,
                "buffered_ticks": len(self.outbox), "dropped_ticks": self.dropped,
                "policy_signing": {"mode": "ed25519" if self.verifier else "unsigned",
                                   "key_id": self.verifier.key_id if self.verifier else None,
                                   "rejected": self.rejected_policies},
                "witnessed_blocks": len(self.witness.blocks)}

    async def send(self) -> None:
        while True:
            await asyncio.sleep(0.2 if await self.send_once() else 1.0)


def _settings() -> tuple[str, str, int]:
    control = os.environ.get("CONTROL_URL", "")
    token = os.environ.get("NODE_TOKEN", "")
    if not control or not token:
        raise RuntimeError("CONTROL_URL and NODE_TOKEN must be set")
    seed = int(os.environ.get("SIM_SEED") or secrets.randbits(31))
    return control.rstrip("/"), token, seed


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    control, token, seed = _settings()
    verifier = Verifier.from_file(os.environ.get("POLICY_PUBLIC_KEY_FILE"))
    if verifier is None:
        log.warning("no POLICY_PUBLIC_KEY_FILE: accepting unsigned policies")
    node = Node(control, token, seed, verifier=verifier, witness=Witness(os.environ.get("WITNESS_FILE")))
    app.state.node, app.state.token = node, token
    await node.hello()
    tasks = [asyncio.create_task(node.run()), asyncio.create_task(node.send())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await node.http.aclose()


app = FastAPI(title="Replay sim node", lifespan=lifespan)


def require_token(request: Request) -> None:
    if not hmac.compare_digest(request.headers.get("x-node-token", ""), request.app.state.token):
        raise HTTPException(401, "bad node token")


def get_node(request: Request) -> Node:
    node: Node = request.app.state.node
    if node.sim is None:
        raise HTTPException(503, "sim not started")
    return node


Auth = [Depends(require_token)]


class Commands(BaseModel):
    steps: list[dict]
    job: dict | None = None


class Chaos(BaseModel):
    scenario: str
    wait_ticks: int = 300
    hints: dict = {}


class Policy(BaseModel):
    version: int
    rules: list[str]
    signature: str | None = None


class Service(BaseModel):
    robot: str
    op: str
    cell: list[int] | None = None
    by: str = ""


class Traffic(BaseModel):
    robot: str
    to: str
    op: str = "yield"
    reason: str = ""
    by: str = ""


class Advice(BaseModel):
    robot: str
    cap: int                      # mm/tick
    ttl: int = 150
    reason: str = ""
    by: str = ""


class Road(BaseModel):
    id: str
    op: str = "repair"
    crew: str = ""
    by: str = ""


class Block(BaseModel):
    n: int
    hash: str
    merkle_root: str | None = None
    prev_hash: str | None = None
    events: int | None = None


def _check_steps(steps: list[dict]) -> None:
    if not steps:
        raise HTTPException(400, "no steps")
    for s in steps:
        op = s.get("op")
        if op not in OPS:
            raise HTTPException(400, f"unknown op {op!r}; fleet API ops: {', '.join(OPS)}")
        if op == "goto":
            cell = s.get("cell")
            if not (isinstance(cell, list) and len(cell) == 2 and W.passable((cell[0], cell[1]))):
                raise HTTPException(400, f"goto needs a floor cell, got {cell!r}")
        elif op in ("pick", "scan") and s.get("slot") not in W.slots:
            raise HTTPException(400, f"unknown slot {s.get('slot')!r}")
        elif op == "drop" and s.get("dock") not in W.docks:
            raise HTTPException(400, f"unknown dock {s.get('dock')!r}")


@app.post("/fleet/robots/{rid}/commands", dependencies=Auth)
async def commands(rid: str, body: Commands, node: Node = Depends(get_node)) -> dict:
    if rid not in W.robot_caps:
        raise HTTPException(404, f"no truck {rid}")
    _check_steps(body.steps)
    inp = {"kind": "cmd", "robot": rid, "steps": body.steps}
    if body.job:
        inp["job"] = body.job
    assert node.sim is not None
    return {"input_id": node.sim.submit(inp), "tick": node.sim.tick_no}


@app.post("/fleet/robots/{rid}/cancel", dependencies=Auth)
async def cancel(rid: str, node: Node = Depends(get_node)) -> dict:
    assert node.sim is not None
    return {"input_id": node.sim.submit({"kind": "cancel", "robot": rid})}


@app.post("/fleet/chaos", dependencies=Auth)
async def chaos(body: Chaos, node: Node = Depends(get_node)) -> dict:
    if body.scenario not in SCENARIOS:
        raise HTTPException(400, f"unknown scenario; known: {', '.join(SCENARIOS)}")
    try:
        hints = check_hints(body.hints)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    assert node.sim is not None
    return {"request_id": node.sim.request_chaos(body.scenario, max(1, min(body.wait_ticks, 1200)), hints),
            "tick": node.sim.tick_no, "hints": hints}


@app.post("/fleet/policy", dependencies=Auth)
async def policy(body: Policy, node: Node = Depends(get_node)) -> dict:
    try:
        make_policy(body.rules, body.version)
    except PolicyError as exc:
        raise HTTPException(400, str(exc)) from exc
    if node.verifier:
        try:
            node.verifier.verify(body.version, body.rules, body.signature)
        except SignatureError as exc:
            node.rejected_policies += 1
            log.warning("rejected policy v%s: %s", body.version, exc)
            raise HTTPException(403, str(exc)) from exc
    assert node.sim is not None
    return {"input_id": node.sim.submit({"kind": "policy", "version": body.version, "rules": body.rules}),
            "applies_at_tick": node.sim.tick_no}


@app.post("/fleet/service", dependencies=Auth)
async def service(body: Service, node: Node = Depends(get_node)) -> dict:
    """Service control for a faulted or standby truck: move (remote), standby, deploy, repair."""
    if body.robot not in W.robot_caps:
        raise HTTPException(404, f"no truck {body.robot}")
    if body.op not in ("move", "standby", "deploy", "repair"):
        raise HTTPException(400, "op must be move, standby, deploy or repair")
    if body.op == "move" and not (body.cell and len(body.cell) == 2 and W.passable((body.cell[0], body.cell[1]))):
        raise HTTPException(400, "move needs a floor cell")
    inp = {"kind": "service", "robot": body.robot, "op": body.op, "by": body.by[:60]}
    if body.op == "move":
        inp["cell"] = [body.cell[0], body.cell[1]]
    assert node.sim is not None
    return {"input_id": node.sim.submit(inp), "tick": node.sim.tick_no}


@app.post("/fleet/traffic", dependencies=Auth)
async def traffic(body: Traffic, node: Node = Depends(get_node)) -> dict:
    """A right-of-way ruling from the control plane's traffic desk: `robot` yields to `to`."""
    if body.robot not in W.robot_caps or body.to not in W.robot_caps or body.robot == body.to:
        raise HTTPException(400, "two different trucks, please")
    if body.op != "yield":
        raise HTTPException(400, "op must be yield")
    assert node.sim is not None
    return {"input_id": node.sim.submit({"kind": "traffic", "robot": body.robot, "to": body.to, "op": "yield",
                                         "reason": body.reason[:240], "by": body.by[:80]}),
            "tick": node.sim.tick_no}


@app.post("/fleet/advice", dependencies=Auth)
async def advice(body: Advice, node: Node = Depends(get_node)) -> dict:
    """Speed advice from a truck's AI driver on the control plane (recorded, so replays stay exact)."""
    if body.robot not in W.robot_caps:
        raise HTTPException(400, "unknown truck")
    assert node.sim is not None
    return {"input_id": node.sim.submit({"kind": "advice", "robot": body.robot, "cap": int(body.cap), "ttl": int(body.ttl),
                                         "reason": body.reason[:160], "by": body.by[:80]}), "tick": node.sim.tick_no}


@app.post("/fleet/road", dependencies=Auth)
async def road(body: Road, node: Node = Depends(get_node)) -> dict:
    """A road repair the road-crew AI ordered has been done."""
    if body.op != "repair" or not body.id.startswith("H"):
        raise HTTPException(400, "op must be repair, id a road feature")
    assert node.sim is not None
    return {"input_id": node.sim.submit({"kind": "road", "op": "repair", "id": body.id, "crew": body.crew[:40],
                                         "by": body.by[:80]}), "tick": node.sim.tick_no}


@app.post("/fleet/witness", dependencies=Auth)
async def witness(body: Block, request: Request) -> dict:
    node: Node = request.app.state.node
    try:
        added = node.witness.add(body.model_dump())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"witnessed": body.n, "new": added}


@app.get("/fleet/witness", dependencies=Auth)
async def witnessed(request: Request) -> dict:
    node: Node = request.app.state.node
    return {"blocks": [{"n": n, "hash": b["hash"]} for n, b in sorted(node.witness.blocks.items())]}


@app.get("/fleet/state", dependencies=Auth)
async def fleet_state(node: Node = Depends(get_node)) -> dict:
    return {"run_id": node.run_id, "frame": node.frame}


@app.get("/health")
async def health(request: Request) -> dict:
    node: Node = request.app.state.node
    return {"ok": node.sim is not None, "run_id": node.run_id,
            "tick": node.sim.tick_no if node.sim else None,
            "buffered_ticks": len(node.outbox), "dropped_ticks": node.dropped,
            "policy_signing": "ed25519" if node.verifier else "unsigned"}
