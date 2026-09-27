"""Control plane entry point on VM A:  uvicorn control.app:app --host 0.0.0.0 --port 8000

Serves three audiences:
  /api/node/*  the sim node and replay workers on VM B (shared node token)
  /api/*       the operator web app (session cookie)
  /ws          live updates to the web app
Run exactly one process: the orchestrator, dispatcher and WebSocket hub live in it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from replay_core.frames import ui_frame
from replay_core.scenarios import SCENARIOS, reinject_hints
from replay_core.world import TICK_HZ, W

from replay_core.signing import Signer

from . import assistant, auth, db, enterprise, fleet, ingest, ledger, policies, service
from .aiops import AIOps
from .traffic import TrafficDesk
from .usage import USAGE
from .bus import Hub
from .config import load
from .lab import Lab, LabError
from .orchestrator import Orchestrator, WorkflowError, original_chaos
from .runtime import Runtime
from .simnode_client import SimNode, SimNodeError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("replay.control")
WEB = Path(__file__).resolve().parent.parent / "web"


async def _retention(rt: Runtime) -> None:
    """Per-tick telemetry is bulky; capsules are self-contained, so old ticks can go."""
    while True:
        await asyncio.sleep(600)
        keep = rt.settings.retention_hours * 3600 * TICK_HZ
        try:
            async with rt.pool.acquire() as c:
                await c.execute("DELETE FROM ticks t USING runs r WHERE t.run_id = r.id AND t.tick < r.last_tick - $1",
                                keep)
                await c.execute("DELETE FROM snapshots s USING runs r WHERE s.run_id = r.id "
                                "AND s.tick < r.last_tick - $1 "
                                "AND NOT EXISTS (SELECT 1 FROM capsules k WHERE k.snapshot_id = s.id)", keep)
        except Exception:
            log.exception("retention")


async def _provision_site(rt: Runtime) -> None:
    """First boot of a database: the model writes the mine's profile (a minute at most)."""
    for attempt in range(5):
        try:
            rt.site = await enterprise.ensure_site(rt.pool, rt.settings)
            rt.hub.publish("site", {"code": rt.site["code"], "name": rt.site["name"]})
            log.info("site %s (%s) from %s", rt.site["code"], rt.site["name"], rt.site["source"])
            return
        except Exception:
            log.exception("provisioning the site (attempt %d)", attempt + 1)
            await asyncio.sleep(5)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load()
    pool = await db.create_pool(settings.database_url)
    applied = await db.migrate(pool)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    signer = Signer.from_file(settings.policy_key_file)
    if signer is None:
        log.warning("no POLICY_SIGNING_KEY_FILE: policies go to the fleet unsigned")
    async with pool.acquire() as c, c.transaction():
        await policies.ensure_base(c, signer)
        if await c.fetchval("SELECT 1 FROM users WHERE username = $1", settings.admin_user) is None:
            await c.execute("INSERT INTO users (username, pw_hash) VALUES ($1, $2)",
                            settings.admin_user, auth.hash_password(settings.admin_password))
    rt = Runtime(settings, pool, Hub(), SimNode(settings.simnode_url, settings.node_token),
                 auto_jobs=settings.auto_jobs)
    rt.orchestrator = Orchestrator(rt)
    rt.lab = Lab(rt)
    rt.signer = signer
    rt.service = service.ServiceDesk(rt)
    rt.traffic = TrafficDesk(rt)
    rt.aiops = AIOps(rt)
    await USAGE.attach(pool, rt.hub)
    app.state.settings, app.state.rt = settings, rt
    tasks = [asyncio.create_task(rt.orchestrator.loop()), asyncio.create_task(fleet.loop(rt)),
             asyncio.create_task(_retention(rt)), asyncio.create_task(ledger.loop(rt)),
             asyncio.create_task(_provision_site(rt)), asyncio.create_task(rt.service.loop()), *rt.aiops.tasks()]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await rt.sim.close()
        await pool.close()


app = FastAPI(title="Replay control plane", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)  # three.js and JSON: ~4x smaller on the wire
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.middleware("http")
async def revalidate_web_assets(request: Request, call_next):
    """Browsers must re-check the web app after every deploy (a 304 when nothing changed)."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def get_rt(request: Request) -> Runtime:
    return request.app.state.rt


def _ws_rt(ws: WebSocket) -> Runtime:
    return ws.app.state.rt


User = Depends(auth.current_user)
Node = Depends(auth.require_node)


# ------------------------------------------------------------------ node API (VM B)

class ClaimBody(BaseModel):
    worker: str


class ResultBody(BaseModel):
    status: str = Field(pattern="^(done|error)$")
    outcome: str | None = None
    failures: list[dict] | None = None
    new_failures: list[dict] | None = None
    warnings: list[dict] | None = None
    trajectory_hash: str | None = None
    matches_live: bool | None = None
    first_divergence: int | None = None
    frames: list[dict] | None = None
    result: dict | None = None
    duration_ms: int | None = None
    error: str | None = None


@app.post("/api/node/hello", dependencies=[Node])
async def node_hello(body: dict, rt: Runtime = Depends(get_rt)) -> dict:
    return await ingest.hello(rt, body)


@app.post("/api/node/telemetry", dependencies=[Node])
async def node_telemetry(body: dict, rt: Runtime = Depends(get_rt)) -> dict:
    return await ingest.telemetry(rt, body)


@app.post("/api/node/replays/claim", dependencies=[Node], response_model=None)
async def claim(body: ClaimBody, rt: Runtime = Depends(get_rt)) -> Response | dict:
    rt.workers.setdefault(body.worker, {})["last_claim"] = time.time()
    rt.meters["claims"].add()
    async with rt.pool.acquire() as c:
        r = await c.fetchrow(
            "UPDATE replays SET status = 'running', worker = $1, started_at = now() WHERE id = ("
            "  SELECT id FROM replays WHERE status = 'queued' ORDER BY CASE kind WHEN 'reproduce' THEN 0 "
            "  WHEN 'render' THEN 0 WHEN 'regression' THEN 2 WHEN 'suite' THEN 3 ELSE 1 END, id "
            "  LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING id, capsule_id, hypothesis_id, experiment_id, kind, policy_version, policy_rules, "
            "control_rules, control_failures, spec", body.worker)
        if r is None:
            return Response(status_code=204)
        capsule_hash = await c.fetchval("SELECT hash FROM capsules WHERE id = $1", r["capsule_id"])
    return {**dict(r), "capsule_hash": capsule_hash}


@app.get("/api/node/capsules/{cid}", dependencies=[Node])
async def node_capsule(cid: int, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c:
        blob = await c.fetchval("SELECT blob FROM capsules WHERE id = $1", cid)
    if blob is None:
        raise HTTPException(404, "no such capsule")
    return blob


@app.post("/api/node/replays/{rid}/result", dependencies=[Node])
async def replay_result(rid: int, body: ResultBody, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c, c.transaction():
        r = await c.fetchrow(
            "UPDATE replays SET status = $2, outcome = $3, failures = $4, new_failures = $5, trajectory_hash = $6, "
            "matches_live = $7, first_divergence = $8, frames = $9, duration_ms = $10, error = $11, "
            "warnings = $12, result = $13, finished_at = now() WHERE id = $1 AND status = 'running' "
            "RETURNING id, capsule_id, hypothesis_id, experiment_id, kind, worker",
            rid, body.status, body.outcome, body.failures, body.new_failures, body.trajectory_hash,
            body.matches_live, body.first_divergence, body.frames, body.duration_ms, body.error, body.warnings,
            body.result)
        if r is None:
            raise HTTPException(409, "replay is not running (requeued or already finished)")
        summary = {"replay": rid, "capsule": r["capsule_id"], "kind": r["kind"], "hypothesis": r["hypothesis_id"],
                   "experiment": r["experiment_id"], "status": body.status, "outcome": body.outcome,
                   "trajectory_hash": body.trajectory_hash, "matches_live": body.matches_live, "worker": r["worker"],
                   "duration_ms": body.duration_ms, "error": body.error}
        await db.log_event(c, "replay.done", summary)
    msg = {"id": rid, "capsule_id": r["capsule_id"], "kind": r["kind"], "status": body.status,
           "outcome": body.outcome, "matches_live": body.matches_live, "trajectory_hash": body.trajectory_hash,
           "hypothesis_id": r["hypothesis_id"], "experiment_id": r["experiment_id"], "worker": r["worker"],
           "duration_ms": body.duration_ms}
    # experiment batches are progress ticks for the investigation view, not incident-page changes
    rt.hub.publish("lab" if r["experiment_id"] else "replay", msg)
    assert rt.orchestrator is not None and rt.lab is not None
    rt.orchestrator.kick()
    rt.lab.wake()
    return {"ok": True}


# ------------------------------------------------------------------ operator API

class LoginBody(BaseModel):
    username: str
    password: str


class JobBody(BaseModel):
    slots: list[str]
    dock: str


class AutoBody(BaseModel):
    enabled: bool


class ChaosBody(BaseModel):
    scenario: str


@app.post("/api/login")
async def login(body: LoginBody, response: Response, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c:
        stored = await c.fetchval("SELECT pw_hash FROM users WHERE username = $1", body.username)
    if stored is None or not auth.check_password(body.password, stored):
        raise HTTPException(401, "wrong username or password")
    response.set_cookie(auth.COOKIE, auth.make_session(body.username, rt.settings.session_secret),
                        max_age=auth.SESSION_SECONDS, httponly=True, samesite="lax",
                        secure=rt.settings.cookie_secure)
    return {"user": body.username}


@app.post("/api/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def me(user: str = User) -> dict:
    return {"user": user}


@app.get("/api/map")
async def world_map(user: str = User) -> dict:
    return {**W.as_json(), "scenarios": SCENARIOS, "tick_hz": TICK_HZ}


@app.get("/api/state")
async def state(user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c:
        pol = await policies.current(c)
        jobs = await c.fetch("SELECT id, kind, lines, dock, status, robot_id, deadline_tick, order_id FROM jobs "
                             "WHERE status IN ('pending', 'assigned', 'active') ORDER BY created_at")
        inbox = await c.fetchval("SELECT count(*) FROM failures WHERE status <> ALL($1::text[])",
                                 ["fixed", "dismissed", "lost"])
    s = rt.settings
    return {**rt.status(), "policy": {"version": pol["version"], "rules": pol["rules"], "hash": pol["hash"],
                                      "signed": bool(pol.get("signature")), "key_id": pol.get("key_id")},
            "ai": {"enabled": s.inference_enabled, "provider": s.inference_provider if s.inference_enabled else "rules",
                   "model": (s.inference_model or "auto") if s.inference_enabled else None},
            "site": {"code": rt.site["code"], "name": rt.site["name"], "company": rt.site["facility"]["company"]}
            if rt.site else None,
            "frame": ui_frame(rt.frame) if rt.frame else None, "jobs": db.rows(jobs), "open_failures": inbox}


@app.get("/api/jobs")
async def jobs(user: str = User, rt: Runtime = Depends(get_rt)) -> list[dict]:
    async with rt.pool.acquire() as c:
        return db.rows(await c.fetch("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50"))


@app.post("/api/jobs")
async def new_job(body: JobBody, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    try:
        return await fleet.create_job(rt, body.slots, body.dock, f"operator:{user}")
    except fleet.JobError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/jobs/auto")
async def auto_jobs(body: AutoBody, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    rt.auto_jobs = body.enabled
    async with rt.pool.acquire() as c:
        await db.log_event(c, "jobs.auto", {"enabled": body.enabled, "by": user}, run_id=rt.run_id)
    return {"auto_jobs": rt.auto_jobs}


@app.post("/api/chaos")
async def chaos(body: ChaosBody, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    if body.scenario not in SCENARIOS:
        raise HTTPException(400, f"unknown scenario; known: {', '.join(SCENARIOS)}")
    try:
        res = await rt.sim.chaos(body.scenario)
    except SimNodeError as exc:
        raise HTTPException(502, str(exc)) from exc
    async with rt.pool.acquire() as c:
        await db.log_event(c, "chaos.requested", {"scenario": body.scenario, "by": user, **res},
                           run_id=rt.run_id, tick=rt.last_tick)
    return res


_FAILURE_LIST = """
SELECT f.id, f.event_id, f.type, f.robot_id, f.tick, f.run_id, f.status, f.scenario, f.note, f.detail,
       f.created_at, f.updated_at,
       k.id AS capsule_id, k.hash AS capsule_hash, k.in_regression,
       (SELECT count(*) FROM replays r WHERE r.capsule_id = k.id AND r.kind = 'reproduce'
          AND r.outcome = 'reproduced' AND r.matches_live) AS reproduced,
       (SELECT count(*) FROM replays r WHERE r.capsule_id = k.id AND r.kind = 'reproduce'
          AND r.status = 'done') AS replayed
FROM failures f LEFT JOIN capsules k ON k.failure_id = f.id
"""


@app.get("/api/failures")
async def failures(user: str = User, rt: Runtime = Depends(get_rt)) -> list[dict]:
    async with rt.pool.acquire() as c:
        return db.rows(await c.fetch(_FAILURE_LIST + " ORDER BY f.id DESC LIMIT 100"))


@app.get("/api/failures/{fid}")
async def failure(fid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c:
        f = await c.fetchrow(_FAILURE_LIST + " WHERE f.id = $1", fid)
        if f is None:
            raise HTTPException(404, "no such failure")
        out: dict[str, Any] = {"failure": dict(f), "capsule": None, "replays": [], "hypotheses": [], "chain": []}
        cid = f["capsule_id"]
        hyp_ids: list[int] = []
        if cid:
            out["capsule"] = db.row(await c.fetchrow(
                "SELECT id, run_id, start_tick, end_tick, fail_tick, hash, size_bytes, in_regression, created_at, "
                "blob->'policy' AS policy, blob->'baseline_failures' AS live_failures FROM capsules WHERE id = $1",
                cid))
            out["replays"] = db.rows(await c.fetch(
                "SELECT id, hypothesis_id, kind, policy_version, policy_rules, status, outcome, new_failures, warnings, "
                "trajectory_hash, matches_live, first_divergence, worker, duration_ms, error, created_at, "
                "finished_at FROM replays WHERE capsule_id = $1 AND kind IN ('reproduce', 'trial', 'proof') "
                "ORDER BY id", cid))
            out["hypotheses"] = db.rows(await c.fetch(
                "SELECT h.*, (SELECT count(*) FROM replays r WHERE r.hypothesis_id = h.id AND r.kind = 'regression' "
                "AND r.status = 'done') AS regression_done FROM hypotheses h WHERE capsule_id = $1 "
                "ORDER BY round DESC, rank", cid))
            hyp_ids = [h["id"] for h in out["hypotheses"]]
        out["investigation"] = await _investigation(c, fid)
        order = await enterprise.failure_order(c, f)
        out["order"] = db.row(order)
        out["impact"] = enterprise.impact(rt.site, f["type"], order) if rt.site else None
        out["chain"] = db.rows(await c.fetch(
            "SELECT id, ts, tick, robot_id, type, payload FROM events WHERE id = $1 "
            "OR (type IN ('failure.status', 'capsule.cut', 'diagnosis', 'investigation.started', "
            "'investigation.done') AND payload->>'failure' = $2) "
            "OR (type IN ('replay.queued', 'replay.done', 'approval') AND payload->>'capsule' = $3) "
            "OR (type = 'hypothesis.status' AND (payload->>'hypothesis')::int = ANY($4::int[])) "
            "ORDER BY id", f["event_id"], str(fid), str(cid), hyp_ids))
    return out


async def _investigation(c, fid: int, inv_id: int | None = None) -> dict | None:
    """The latest investigation of a failure (or a given one), with its trace and experiments."""
    inv = await c.fetchrow("SELECT * FROM investigations WHERE ($2::int IS NOT NULL AND id = $2) "
                           "OR ($2::int IS NULL AND failure_id = $1) ORDER BY id DESC LIMIT 1", fid, inv_id)
    if inv is None:
        return None
    steps = await c.fetch("SELECT n, tool, args, why, status, summary, experiment_id, created_at, finished_at "
                          "FROM investigation_steps WHERE investigation_id = $1 ORDER BY n", inv["id"])
    exps = await c.fetch("SELECT id, kind, params, status, summary, sims, duration_ms, error, created_at, finished_at, "
                         "(SELECT count(*) FROM replays r WHERE r.experiment_id = e.id) AS jobs, "
                         "(SELECT count(*) FROM replays r WHERE r.experiment_id = e.id AND r.status = 'done') AS jobs_done "
                         "FROM experiments e WHERE investigation_id = $1 ORDER BY id", inv["id"])
    return {**dict(inv), "steps": db.rows(steps), "experiments": db.rows(exps)}


@app.get("/api/investigations/{iid}")
async def investigation(iid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c:
        out = await _investigation(c, 0, iid)
    if out is None:
        raise HTTPException(404, "no such investigation")
    return out


async def _act(coro) -> dict:
    try:
        res = await coro
    except WorkflowError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "result": res}


@app.post("/api/failures/{fid}/replay")
async def replay_again(fid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    assert rt.orchestrator is not None
    return await _act(rt.orchestrator.replay_again(fid))


@app.post("/api/failures/{fid}/diagnose")
async def rediagnose(fid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    assert rt.orchestrator is not None
    return await _act(rt.orchestrator.rediagnose(fid))


@app.post("/api/failures/{fid}/dismiss")
async def dismiss(fid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    assert rt.orchestrator is not None
    return await _act(rt.orchestrator.dismiss(fid, user))


@app.post("/api/failures/{fid}/reinject")
async def reinject(fid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    """Recreate the original situation live: same bench and gap, same material, same road closed."""
    async with rt.pool.acquire() as c:
        f = await c.fetchrow("SELECT f.run_id, f.type, f.tick, f.scenario, k.blob->'inputs' AS inputs "
                             "FROM failures f LEFT JOIN capsules k ON k.failure_id = f.id WHERE f.id = $1", fid)
        original = await original_chaos(c, f["run_id"], f["type"], f["tick"], f["inputs"]) if f else None
    if f is None or not f["scenario"] or not original:
        raise HTTPException(409, "this failure was not caused by an injected hazard")
    hints = reinject_hints(original)
    try:
        res = await rt.sim.chaos(f["scenario"], hints, wait_ticks=900)
    except SimNodeError as exc:
        raise HTTPException(502, str(exc)) from exc
    async with rt.pool.acquire() as c:
        await db.log_event(c, "chaos.requested", {"scenario": f["scenario"], "by": user, "reinject_of": fid,
                                                  **res}, run_id=rt.run_id, tick=rt.last_tick)
    return res


@app.post("/api/hypotheses/{hid}/approve")
async def approve(hid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    assert rt.orchestrator is not None
    return await _act(rt.orchestrator.approve(hid, user))


@app.get("/api/capsules/{cid}/live")
async def capsule_live(cid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c:
        blob = await c.fetchval("SELECT blob FROM capsules WHERE id = $1", cid)
    if blob is None:
        raise HTTPException(404, "no such capsule")
    return {"start": blob["start"], "end": blob["end"], "failure": blob["failure"], "hash": blob["hash"],
            "frames": [ui_frame(f, with_paths=False) for f in blob["live_frames"]]}


@app.get("/api/replays/{rid}")
async def replay(rid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c:
        r = await c.fetchrow("SELECT * FROM replays WHERE id = $1", rid)
    if r is None:
        raise HTTPException(404, "no such replay")
    return dict(r)


class ExperimentBody(BaseModel):
    kind: str = Field(pattern="^(reproduce|isolate|what_if|stress|tune)$")
    fix: str | None = Field(default=None, max_length=120)
    rules: list[str] | None = Field(default=None, max_length=4)
    values: list[float] | None = Field(default=None, max_length=12)
    n: int | None = Field(default=None, ge=6, le=60)


class RenderBody(BaseModel):
    variant: int = Field(ge=0, le=60)
    which: str = Field(default="fix", pattern="^(fix|base)$")
    fix: str | None = Field(default=None, max_length=120)


async def _lab(coro) -> dict:
    try:
        return await coro
    except LabError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/capsules/{cid}/experiments")
async def experiments(cid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> list[dict]:
    assert rt.lab is not None
    return await rt.lab.history(cid)


@app.post("/api/capsules/{cid}/experiments")
async def run_experiment(cid: int, body: ExperimentBody, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    """An operator runs one instrument by hand (the investigator uses the same ones)."""
    lab = rt.lab
    assert lab is not None
    if body.kind in ("stress", "tune") and not body.fix:
        raise HTTPException(400, f"{body.kind} needs a fix")
    if body.kind == "what_if" and not (body.rules or body.fix):
        raise HTTPException(400, "what_if needs rules")
    runs = {
        "reproduce": lambda: lab.reproduce(cid),
        "isolate": lambda: lab.isolate(cid),
        "what_if": lambda: lab.what_if(cid, body.rules or [body.fix or ""]),
        "stress": lambda: lab.stress(cid, body.fix or "", body.n),
        "tune": lambda: lab.tune(cid, body.fix or "", body.values, body.n),
    }
    async with rt.pool.acquire() as c:
        await db.log_event(c, "experiment.requested", {"capsule": cid, "by": user, **body.model_dump()})
    return await _lab(runs[body.kind]())


@app.get("/api/experiments/{eid}")
async def experiment(eid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    assert rt.lab is not None
    try:
        return await rt.lab.get(eid)
    except LabError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/experiments/{eid}/render")
async def render_variant(eid: int, body: RenderBody, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    assert rt.lab is not None
    return await _lab(rt.lab.render(eid, body.variant, body.which, body.fix))


@app.get("/api/site")
async def site(user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    if rt.site is None:
        raise HTTPException(503, "the site is still being provisioned")
    return rt.site


@app.get("/api/orders")
async def orders(limit: int = 40, user: str = User, rt: Runtime = Depends(get_rt)) -> list[dict]:
    async with rt.pool.acquire() as c:
        return db.rows(await c.fetch(
            "SELECT o.*, c.name AS customer, c.tier FROM orders o JOIN customers c ON c.id = o.customer_id "
            "ORDER BY o.created_at DESC LIMIT $1", max(1, min(limit, 200))))


@app.get("/api/kpis")
async def kpis(user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    return await enterprise.kpis(rt)


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=600)
    scope: str = Field(default="site", pattern="^(robot|site)$")
    robot: str | None = Field(default=None, pattern="^T[0-9]{2}$")
    history: list[dict] = Field(default_factory=list, max_length=12)


@app.post("/api/ask")
async def ask(body: AskBody, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    """The operations copilot: a question about one truck or the whole pit, answered from live data."""
    if body.scope == "robot" and not body.robot:
        raise HTTPException(400, "which truck?")
    try:
        return await assistant.ask(rt, user, body.question, body.scope, body.robot if body.scope == "robot" else None,
                                   body.history)
    except assistant.AskError as exc:
        raise HTTPException(429 if "too many" in str(exc) else 409, str(exc)) from exc


@app.get("/api/service")
async def service_cases(user: str = User, rt: Runtime = Depends(get_rt)) -> list[dict]:
    """Open truck service cases, and the ones resolved in the last 10 minutes."""
    return await service.open_cases(rt.pool)


@app.post("/api/service/{cid}/done")
async def service_done(cid: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    """The controller marks a fitter's repair done (the fitter normally does)."""
    try:
        return await rt.service.mark_done(cid, user)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/robots/{rid}/events")
async def robot_events(rid: str, limit: int = 40, user: str = User, rt: Runtime = Depends(get_rt)) -> list[dict]:
    """A truck's recent activity in the current run, newest first (backfills the live log)."""
    async with rt.pool.acquire() as c:
        return db.rows(await c.fetch(
            "SELECT id, tick, type, payload FROM events WHERE run_id = $1 AND robot_id = $2 "
            "AND (type LIKE 'sim.%' OR type IN ('dispatch', 'ai.dispatch', 'ai.traffic', 'traffic.rule', 'failure', "
            "'input.chaos')) ORDER BY id DESC LIMIT $3",
            rt.run_id, rid, max(1, min(limit, 200))))


@app.get("/api/decisions")
async def decisions(user: str = User, rt: Runtime = Depends(get_rt)) -> list[dict]:
    """Recent dispatch, traffic and service decisions (AI or rules), oldest first."""
    return list(rt.decisions)


@app.get("/api/ai")
async def ai_status(user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    """The AI agents running the pit (drivers, auditor, road crew, supervisor, hazards, autopilot) and what the
    Vultr Serverless Inference calls behind them cost."""
    return rt.aiops.status() if rt.aiops else {"usage": USAGE.snapshot()}


@app.get("/api/ai/usage")
async def ai_usage(hours: int = 60, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    return {**USAGE.snapshot(), "hourly": await USAGE.history(max(1, min(168, hours)))}


@app.get("/api/faces")
async def faces(user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    """Each dig face: trucks on their way or under the excavator, and how long it has been left alone."""
    return rt.faces


@app.get("/api/audit")
async def audit(user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c:
        blocks = db.rows(await c.fetch("SELECT * FROM audit_blocks ORDER BY n DESC LIMIT 12"))
        unsealed = await c.fetchval("SELECT count(*) FROM events WHERE seq IS NULL")
        total = await c.fetchval("SELECT COALESCE(max(n), 0) FROM audit_blocks")
    return {"blocks": blocks, "total_blocks": total, "unsealed": unsealed,
            "signing": {"mode": "ed25519" if rt.signer else "unsigned", "key_id": rt.signer.key_id if rt.signer else None}}


@app.post("/api/audit/verify")
async def audit_verify(user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    res = await ledger.verify(rt)
    async with rt.pool.acquire() as c:
        await db.log_event(c, "audit.verified", {"by": user, **{k: res[k] for k in ("ok", "blocks", "events",
                                                                                      "head", "problem_count", "ms")},
                                                  "witness": res["witness"]})
    return res


@app.get("/api/audit/proof/{event_id}")
async def audit_proof(event_id: int, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    try:
        return await ledger.proof(rt, event_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/regression")
async def regression(user: str = User, rt: Runtime = Depends(get_rt)) -> list[dict]:
    async with rt.pool.acquire() as c:
        return db.rows(await c.fetch("""
            SELECT k.id, k.hash, k.fail_tick, k.created_at, f.id AS failure_id, f.type, f.robot_id, f.scenario,
                   pv.version AS fixed_in, pv.fix_dsl,
                   (SELECT row_to_json(x) FROM (SELECT r.id, r.outcome, r.policy_version, r.status, r.finished_at
                      FROM replays r WHERE r.capsule_id = k.id AND r.kind IN ('suite', 'proof')
                      ORDER BY r.id DESC LIMIT 1) x) AS latest
            FROM capsules k JOIN failures f ON f.id = k.failure_id
            LEFT JOIN policy_versions pv ON pv.source_capsule_id = k.id
            WHERE k.in_regression ORDER BY k.id"""))


@app.post("/api/regression/run")
async def regression_run(user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    assert rt.orchestrator is not None
    return {"queued": await rt.orchestrator.run_suite()}


@app.get("/api/policy")
async def policy_versions(user: str = User, rt: Runtime = Depends(get_rt)) -> list[dict]:
    async with rt.pool.acquire() as c:
        return db.rows(await c.fetch("SELECT * FROM policy_versions ORDER BY version DESC"))


@app.get("/api/events")
async def events(before: int | None = None, type: str | None = None, limit: int = 200,
                 user: str = User, rt: Runtime = Depends(get_rt)) -> list[dict]:
    limit = max(1, min(limit, 500))
    async with rt.pool.acquire() as c:
        return db.rows(await c.fetch(
            "SELECT id, ts, run_id, tick, robot_id, type, payload FROM events "
            "WHERE ($1::bigint IS NULL OR id < $1) AND ($2::text IS NULL OR type LIKE $2 || '%') "
            "ORDER BY id DESC LIMIT $3", before, type, limit))


@app.get("/api/timeline")
async def timeline(user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c:
        span = await c.fetchrow("SELECT min(tick) AS first, max(tick) AS last FROM ticks WHERE run_id = $1",
                                rt.run_id)
    return {"run_id": rt.run_id, "first": span["first"], "last": span["last"]}


@app.get("/api/frame")
async def frame_at(tick: int, run: str | None = None, user: str = User, rt: Runtime = Depends(get_rt)) -> dict:
    async with rt.pool.acquire() as c:
        fr = await c.fetchval("SELECT frame FROM ticks WHERE run_id = $1 AND tick = $2", run or rt.run_id, tick)
    if fr is None:
        raise HTTPException(404, "no telemetry for that tick")
    return ui_frame(fr)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    if auth.ws_user(websocket) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    rt = _ws_rt(websocket)
    q = rt.hub.subscribe()
    try:
        while True:
            await websocket.send_text(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        rt.hub.unsubscribe(q)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/healthz")
async def healthz(rt: Runtime = Depends(get_rt)) -> dict:
    return {"ok": True, **rt.status()}
