"""Fleet policy versions: the current one, and promoting a new one."""
from __future__ import annotations

import asyncpg

from replay_core.policy import make_policy

from .db import log_event


async def current(conn: asyncpg.Connection) -> dict:
    r = await conn.fetchrow("SELECT * FROM policy_versions ORDER BY version DESC LIMIT 1")
    if r is None:
        raise RuntimeError("no policy version; ensure_base() was not run")
    return dict(r)


async def ensure_base(conn: asyncpg.Connection) -> None:
    if await conn.fetchval("SELECT count(*) FROM policy_versions") == 0:
        pol = make_policy([], 1)
        await conn.execute(
            "INSERT INTO policy_versions (version, rules, hash, approved_by) VALUES (1, $1, $2, 'system')",
            pol["rules"], pol["hash"])
        await log_event(conn, "policy.version", {"version": 1, "rules": [], "hash": pol["hash"]})


async def promote(conn: asyncpg.Connection, fix: str, approved_by: str, capsule_id: int) -> dict:
    """New version = current rules + fix. Caller holds the transaction."""
    cur = await current(conn)
    rules = list(cur["rules"])
    if fix not in rules:
        rules.append(fix)
    pol = make_policy(rules, cur["version"] + 1)
    new = await conn.fetchrow(
        "INSERT INTO policy_versions (version, parent_id, rules, fix_dsl, hash, approved_by, source_capsule_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
        pol["version"], cur["id"], pol["rules"], fix, pol["hash"], approved_by, capsule_id)
    await log_event(conn, "policy.version", {"version": pol["version"], "parent": cur["version"],
                                             "fix": fix, "rules": pol["rules"], "hash": pol["hash"],
                                             "approved_by": approved_by, "capsule_id": capsule_id})
    return dict(new)
