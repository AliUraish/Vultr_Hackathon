"""Tamper-evident audit ledger over the event log.

The event log is append-only by convention; the ledger makes changes to it detectable. A sealer
running in the control plane takes events that have not been sealed yet (in insert order), gives
each a sequence number and a leaf hash of its canonical content, and closes them into a block:

    leaf        sha256(canonical JSON of id, ts, run, tick, robot, type, payload)
    merkle_root binary Merkle tree over the block's leaves (last leaf duplicated on odd levels)
    hash        sha256(n | first_seq | last_seq | events | merkle_root | prev_hash)

Blocks chain by prev_hash, so editing, deleting or reordering any sealed event changes its leaf,
its block's root and every block hash after it. Each block head is also sent to the sim node on
VM B, which keeps its own append-only copy and refuses to re-witness a block number with a
different hash, so rewriting history on VM A alone is detectable too.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

import asyncpg

from .runtime import Runtime

log = logging.getLogger("replay.ledger")

BLOCK_MAX = 500          # events per block at most
SEAL_EVERY = 3.0         # seconds between blocks
SETTLE = "1 second"      # leave very fresh rows for the next block
GENESIS = "0" * 64
_LOCK = 7_417_003


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical(e: dict | asyncpg.Record) -> str:
    ts = e["ts"]
    return json.dumps({"id": e["id"], "ts": ts.isoformat() if hasattr(ts, "isoformat") else ts,
                       "run": e["run_id"], "tick": e["tick"], "robot": e["robot_id"], "type": e["type"],
                       "payload": e["payload"]}, sort_keys=True, separators=(",", ":"), default=str)


def leaf(e: dict | asyncpg.Record) -> str:
    return _h(canonical(e))


def merkle_root(leaves: list[str]) -> str:
    if not leaves:
        return _h("")
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_h(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_proof(leaves: list[str], index: int) -> list[tuple[str, str]]:
    """Sibling hashes from leaf to root: [(side, hash)], side 'L' or 'R' of the sibling."""
    proof, level, i = [], list(leaves), index
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sib = i ^ 1
        proof.append(("L" if sib < i else "R", level[sib]))
        level = [_h(level[k] + level[k + 1]) for k in range(0, len(level), 2)]
        i //= 2
    return proof


def check_proof(leaf_hash: str, proof: list[tuple[str, str]], root: str) -> bool:
    h = leaf_hash
    for side, sib in proof:
        h = _h(sib + h) if side == "L" else _h(h + sib)
    return h == root


def block_hash(n: int, first: int, last: int, count: int, root: str, prev: str) -> str:
    return _h(f"{n}|{first}|{last}|{count}|{root}|{prev}")


# ---------------------------------------------------------------- sealing

async def seal_once(pool: asyncpg.Pool) -> dict | None:
    async with pool.acquire() as c, c.transaction():
        await c.execute("SELECT pg_advisory_xact_lock($1)", _LOCK)
        rows = await c.fetch("SELECT id, ts, run_id, tick, robot_id, type, payload FROM events "
                             f"WHERE seq IS NULL AND ts < now() - interval '{SETTLE}' ORDER BY id LIMIT $1", BLOCK_MAX)
        if not rows:
            return None
        head = await c.fetchrow("SELECT n, last_seq, hash FROM audit_blocks ORDER BY n DESC LIMIT 1")
        n = (head["n"] + 1) if head else 1
        first = (head["last_seq"] + 1) if head else 1
        prev = head["hash"] if head else GENESIS
        leaves = [leaf(r) for r in rows]
        await c.executemany("UPDATE events SET seq = $2, leaf = $3 WHERE id = $1",
                            [(r["id"], first + i, lf) for i, (r, lf) in enumerate(zip(rows, leaves))])
        root = merkle_root(leaves)
        last = first + len(rows) - 1
        h = block_hash(n, first, last, len(rows), root, prev)
        await c.execute("INSERT INTO audit_blocks (n, first_seq, last_seq, events, merkle_root, prev_hash, hash) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7)", n, first, last, len(rows), root, prev, h)
    return {"n": n, "first_seq": first, "last_seq": last, "events": len(rows), "merkle_root": root,
            "prev_hash": prev, "hash": h}


async def witness_pending(rt: Runtime) -> None:
    """Send unwitnessed block heads to VM B."""
    async with rt.pool.acquire() as c:
        blocks = await c.fetch("SELECT n, hash, merkle_root, prev_hash, events FROM audit_blocks "
                               "WHERE NOT witnessed ORDER BY n LIMIT 50")
    for b in blocks:
        try:
            await rt.sim.witness(dict(b))
        except Exception as exc:  # VM B away or refusing: retried on the next round
            log.warning("block %s not witnessed: %s", b["n"], exc)
            return
        async with rt.pool.acquire() as c:
            await c.execute("UPDATE audit_blocks SET witnessed = true WHERE n = $1", b["n"])


async def loop(rt: Runtime) -> None:
    while True:
        await asyncio.sleep(SEAL_EVERY)
        try:
            for _ in range(20):  # catch up in bursts after a pause
                b = await seal_once(rt.pool)
                if b is None:
                    break
                rt.hub.publish("ledger", b)
            await witness_pending(rt)
        except Exception:
            log.exception("ledger sealer")


# ---------------------------------------------------------------- verification

async def verify(rt: Runtime) -> dict[str, Any]:
    """Recompute every leaf, Merkle root and block hash from the stored events; compare with VM B."""
    t0 = time.monotonic()
    problems: list[str] = []
    async with rt.pool.acquire() as c:
        blocks = await c.fetch("SELECT * FROM audit_blocks ORDER BY n")
        unsealed = await c.fetchval("SELECT count(*) FROM events WHERE seq IS NULL")
        prev, events = GENESIS, 0
        for b in blocks:
            if b["prev_hash"] != prev:
                problems.append(f"block {b['n']}: chain broken (prev hash does not match block {b['n'] - 1})")
            if block_hash(b["n"], b["first_seq"], b["last_seq"], b["events"], b["merkle_root"], b["prev_hash"]) != b["hash"]:
                problems.append(f"block {b['n']}: header altered")
            rows = await c.fetch("SELECT id, ts, run_id, tick, robot_id, type, payload, seq, leaf FROM events "
                                 "WHERE seq BETWEEN $1 AND $2 ORDER BY seq", b["first_seq"], b["last_seq"])
            if len(rows) != b["events"]:
                problems.append(f"block {b['n']}: {b['events'] - len(rows)} sealed event(s) missing")
            leaves = []
            for r in rows:
                lf = leaf(r)
                if lf != r["leaf"]:
                    problems.append(f"event {r['id']} (seq {r['seq']}, {r['type']}): content changed after sealing")
                leaves.append(lf)
            if merkle_root(leaves) != b["merkle_root"]:
                problems.append(f"block {b['n']}: Merkle root does not match its events")
            events += len(rows)
            prev = b["hash"]
    witness: dict[str, Any] = {"checked": 0, "matching": 0, "conflicts": [], "reachable": True}
    try:
        seen = {w["n"]: w["hash"] for w in await rt.sim.witnessed()}
        for b in blocks:
            if b["n"] in seen:
                witness["checked"] += 1
                if seen[b["n"]] == b["hash"]:
                    witness["matching"] += 1
                else:
                    witness["conflicts"].append(b["n"])
                    problems.append(f"block {b['n']}: differs from the copy witnessed by VM B")
    except Exception as exc:
        witness["reachable"] = False
        witness["error"] = str(exc)[:200]
    return {"ok": not problems, "blocks": len(blocks), "events": events, "unsealed": unsealed,
            "head": blocks[-1]["hash"] if blocks else GENESIS, "problems": problems[:20],
            "problem_count": len(problems), "witness": witness, "ms": int((time.monotonic() - t0) * 1000)}


async def proof(rt: Runtime, event_id: int) -> dict[str, Any]:
    """Inclusion proof for one event: its leaf, the path to its block's Merkle root, the block."""
    async with rt.pool.acquire() as c:
        e = await c.fetchrow("SELECT id, ts, run_id, tick, robot_id, type, payload, seq, leaf FROM events WHERE id = $1",
                             event_id)
        if e is None or e["seq"] is None:
            raise ValueError("event not sealed yet")
        b = await c.fetchrow("SELECT * FROM audit_blocks WHERE $1 BETWEEN first_seq AND last_seq", e["seq"])
        leaves = [r["leaf"] for r in await c.fetch("SELECT leaf FROM events WHERE seq BETWEEN $1 AND $2 ORDER BY seq",
                                                   b["first_seq"], b["last_seq"])]
    path = merkle_proof(leaves, e["seq"] - b["first_seq"])
    return {"event": e["id"], "seq": e["seq"], "leaf": e["leaf"], "recomputed_leaf": leaf(e),
            "block": b["n"], "merkle_root": b["merkle_root"], "block_hash": b["hash"], "witnessed": b["witnessed"],
            "path": path, "valid": leaf(e) == e["leaf"] and check_proof(e["leaf"], path, b["merkle_root"])}


__all__ = ["block_hash", "check_proof", "leaf", "loop", "merkle_proof", "merkle_root", "proof", "seal_once", "verify"]
