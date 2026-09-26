"""Postgres access: pool with JSONB codecs, ordered SQL migrations, event log helper."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import asyncpg

MIGRATIONS = Path(__file__).parent / "migrations"
_LOCK_ID = 7_417_001  # advisory lock so two starting processes never migrate at once


async def _init(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=2, max_size=10, init=_init, command_timeout=30)


async def migrate(pool: asyncpg.Pool) -> list[str]:
    applied: list[str] = []
    async with pool.acquire() as c:
        await c.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
        try:
            await c.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                            "name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            done = {r["name"] for r in await c.fetch("SELECT name FROM schema_migrations")}
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.name in done:
                    continue
                async with c.transaction():
                    await c.execute(f.read_text())
                    await c.execute("INSERT INTO schema_migrations (name) VALUES ($1)", f.name)
                applied.append(f.name)
        finally:
            await c.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)
    return applied


async def log_event(conn: asyncpg.Connection, type_: str, payload: dict[str, Any] | None = None, *,
                    run_id: str | None = None, tick: int | None = None, robot_id: str | None = None) -> int:
    """Append to the event log. Every state change in the workflow goes through here."""
    return await conn.fetchval(
        "INSERT INTO events (run_id, tick, robot_id, type, payload) VALUES ($1, $2, $3, $4, $5) RETURNING id",
        run_id, tick, robot_id, type_, payload or {},
    )


def row(r: asyncpg.Record | None) -> dict | None:
    return dict(r) if r is not None else None


def rows(rs: list[asyncpg.Record]) -> list[dict]:
    return [dict(r) for r in rs]
