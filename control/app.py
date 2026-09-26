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
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from replay_core.frames import ui_frame
from replay_core.scenarios import SCENARIOS, reinject_hints
from replay_core.world import TICK_HZ, W

from . import auth, db, fleet, ingest, policies
from .bus import Hub
from .config import load
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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load()
    pool = await db.create_pool(settings.database_url)
    applied = await db.migrate(pool)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    async with pool.acquire() as c, c.transaction():
        await policies.ensure_base(c)
        if await c.fetchval("SELECT 1 FROM users WHERE username = $1", settings.admin_user) is None:
            await c.execute("INSERT INTO users (username, pw_hash) VALUES ($1, $2)",
                            settings.admin_user, auth.hash_password(settings.admin_password))
    rt = Runtime(settings, pool, Hub(), SimNode(settings.simnode_url, settings.node_token),
                 auto_jobs=settings.auto_jobs)
    rt.orchestrator = Orchestrator(rt)
    app.state.settings, app.state.rt = settings, rt
    tasks = [asyncio.create_task(rt.orchestrator.loop()), asyncio.create_task(fleet.loop(rt)),
             asyncio.create_task(_retention(rt))]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await rt.sim.close()
        await pool.close()


app = FastAPI(title="Replay control plane", lifespan=lifespan)
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
    async with rt.pool.acquire() as c:
        r = await c.fetchrow(
            "UPDATE replays SET status = 'running', worker = $1, started_at = now() WHERE id = ("
            "  SELECT id FROM replays WHERE status = 'queued' ORDER BY CASE kind WHEN 'reproduce' THEN 0 "
            "  WHEN 'trial' THEN 1 WHEN 'proof' THEN 1 WHEN 'regression' THEN 2 ELSE 3 END, id "
            "  LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING id, capsule_id, hypothesis_id, kind, policy_version, policy_rules, control_rules, "
            "control_failures", body.worker)
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
            "warnings = $12, finished_at = now() WHERE id = $1 AND status = 'running' "
            "RETURNING id, capsule_id, hypothesis_id, kind, worker",
            rid, body.status, body.outcome, body.failures, body.new_failures, body.trajectory_hash,
            body.matches_live, body.first_divergence, body.frames, body.duration_ms, body.error, body.warnings)
        if r is None:
            raise HTTPException(409, "replay is not running (requeued or already finished)")
        summary = {"replay": rid, "capsule": r["capsule_id"], "kind": r["kind"], "hypothesis": r["hypothesis_id"],
                   "status": body.status, "outcome": body.outcome, "trajectory_hash": body.trajectory_hash,
                   "matches_live": body.matches_live, "worker": r["worker"], "duration_ms": body.duration_ms,
                   "error": body.error}
        await db.log_event(c, "replay.done", summary)
    rt.hub.publish("replay", {"id": rid, "capsule_id": r["capsule_id"], "kind": r["kind"], "status": body.status,
                              "outcome": body.outcome, "matches_live": body.matches_live,
                              "trajectory_hash": body.trajectory_hash, "hypothesis_id": r["hypothesis_id"]})
    assert rt.orchestrator is not None
    rt.orchestrator.kick()
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
        jobs = await c.fetch("SELECT id, kind, lines, dock, status, robot_id, deadline_tick FROM jobs "
                             "WHERE status IN ('pending', 'assigned', 'active') ORDER BY created_at")
        inbox = await c.fetchval("SELECT count(*) FROM failures WHERE status <> ALL($1::text[])",
                                 ["fixed", "dismissed", "lost"])
    return {**rt.status(), "policy": {"version": pol["version"], "rules": pol["rules"], "hash": pol["hash"]},
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
        out["chain"] = db.rows(await c.fetch(
            "SELECT id, ts, tick, robot_id, type, payload FROM events WHERE id = $1 "
            "OR (type IN ('failure.status', 'capsule.cut', 'diagnosis') AND payload->>'failure' = $2) "
            "OR (type IN ('replay.queued', 'replay.done', 'approval') AND payload->>'capsule' = $3) "
            "OR (type = 'hypothesis.status' AND (payload->>'hypothesis')::int = ANY($4::int[])) "
            "ORDER BY id", f["event_id"], str(fid), str(cid), hyp_ids))
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
    """Recreate the original situation live: same aisle and gap, same item class, same aisle closed."""
    async with rt.pool.acquire() as c:
        f = await c.fetchrow("SELECT f.run_id, f.type, f.tick, f.scenario, k.blob->'inputs' AS inputs "
                             "FROM failures f LEFT JOIN capsules k ON k.failure_id = f.id WHERE f.id = $1", fid)
        original = await original_chaos(c, f["run_id"], f["type"], f["tick"], f["inputs"]) if f else None
    if f is None or not f["scenario"] or not original:
        raise HTTPException(409, "this failure was not caused by a canned scenario")
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
