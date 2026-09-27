"""The platform view: how the two Vultr VMs are being used, measured live, and what scaling out buys.

VM A runs the control plane and Postgres (system of record); VM B runs the live sim and a pool of
stateless workers that claim replay and experiment jobs from Postgres with SKIP LOCKED. Workers
cache capsules and hold no other state, so throughput scales by adding VM B nodes with the same
node token: nothing else changes. The capacity model below uses measured per-worker throughput.
"""
from __future__ import annotations

import time
from typing import Any

from replay_core.hoststats import host_stats

from .runtime import Runtime

VM_B_HOURLY_USD = 0.021      # vc2-2c-2gb, the VM B plan in use
VM_A_HOURLY_USD = 0.014      # vc2-1c-2gb
WINDOW_MIN = 15


async def snapshot(rt: Runtime) -> dict[str, Any]:
    async with rt.pool.acquire() as c:
        db_bytes = await c.fetchval("SELECT pg_database_size(current_database())")
        tables = {r["relname"]: int(r["n"]) for r in await c.fetch(
            "SELECT relname, n_live_tup AS n FROM pg_stat_user_tables WHERE relname = ANY($1::text[])",
            ["events", "ticks", "snapshots", "capsules", "replays", "experiments", "investigations",
             "investigation_steps", "orders", "jobs", "audit_blocks", "policy_versions"])}
        queue = [dict(r) for r in await c.fetch(
            "SELECT kind, count(*) FILTER (WHERE status = 'queued') AS queued, "
            "count(*) FILTER (WHERE status = 'running') AS running, "
            "count(*) FILTER (WHERE status = 'done' AND finished_at > now() - make_interval(mins => $1)) AS done, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE finished_at > now() - make_interval(mins => $1)) AS p50_ms, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE finished_at > now() - make_interval(mins => $1)) AS p95_ms, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM started_at - created_at) * 1000) "
            "  FILTER (WHERE finished_at > now() - make_interval(mins => $1)) AS wait_p95_ms "
            "FROM replays WHERE created_at > now() - interval '6 hours' GROUP BY kind ORDER BY kind", WINDOW_MIN)]
        workers = [dict(r) for r in await c.fetch(
            "SELECT worker, count(*) AS jobs, sum(duration_ms) AS busy_ms, "
            "sum(CASE WHEN experiment_id IS NULL THEN 1 ELSE COALESCE((result->>'sims')::int, 1) END) AS sims, "
            "max(finished_at) AS last_done FROM replays WHERE worker IS NOT NULL AND status = 'done' "
            "AND finished_at > now() - make_interval(mins => $1) GROUP BY worker ORDER BY worker", WINDOW_MIN)]
        sims_total = await c.fetchval(
            "SELECT COALESCE(sum(CASE WHEN experiment_id IS NULL THEN 1 ELSE COALESCE((result->>'sims')::int, 1) END), 0) "
            "FROM replays WHERE status = 'done'")
        events_min = await c.fetchval("SELECT count(*) FROM events WHERE id > (SELECT COALESCE(max(id), 0) - 20000 FROM events) "
                                      "AND ts > now() - interval '60 seconds'")
        llm = await c.fetchrow("SELECT count(*) AS investigations, "
                               "COALESCE(sum((report->'tokens'->>'in')::int), 0) AS tokens_in, "
                               "COALESCE(sum((report->'tokens'->>'out')::int), 0) AS tokens_out, "
                               "COALESCE(sum(sims), 0) AS sims FROM investigations")
        ledger = await c.fetchrow("SELECT count(*) AS blocks, COALESCE(max(last_seq), 0) AS sealed, "
                                  "count(*) FILTER (WHERE witnessed) AS witnessed FROM audit_blocks")

    now = time.time()
    for w in workers:
        w["busy_ms"] = int(w["busy_ms"] or 0)
        w["utilization"] = round(min(1.0, w["busy_ms"] / (WINDOW_MIN * 60_000)), 3)
        w["sims_per_busy_s"] = round(w["sims"] / (w["busy_ms"] / 1000), 1) if w["busy_ms"] else None
        seen = rt.workers.get(w["worker"], {}).get("last_claim")
        w["last_seen_s"] = round(now - seen, 1) if seen else None
    for name, info in rt.workers.items():  # idle workers still claim every half second
        if not any(w["worker"] == name for w in workers):
            workers.append({"worker": name, "jobs": 0, "busy_ms": 0, "sims": 0, "utilization": 0.0,
                            "sims_per_busy_s": None, "last_seen_s": round(now - info["last_claim"], 1)})
    live_workers = [w for w in workers if w["last_seen_s"] is not None and w["last_seen_s"] < 10]
    rates = [w["sims_per_busy_s"] for w in workers if w["sims_per_busy_s"]]
    per_worker = round(sum(rates) / len(rates), 1) if rates else None
    n_workers = max(1, len(live_workers))
    cores_b = (rt.sim_host or {}).get("cpus") or 2
    capacity = None
    if per_worker:
        fleet = per_worker * n_workers
        capacity = {
            "sims_per_s_per_worker": per_worker, "workers": n_workers, "sims_per_s": round(fleet, 1),
            "stress_30_s": round(30 / fleet, 1), "tune_9x30_s": round(270 / fleet, 1),
            "investigation_s": round(600 / fleet, 1),
            "per_extra_node": {"workers": cores_b, "sims_per_s": round(per_worker * cores_b, 1),
                               "usd_per_hour": VM_B_HOURLY_USD},
        }
    return {
        "at": now,
        "vm_a": {"role": "control plane · Postgres 16 · web app", "host": host_stats(),
                 "uptime_s": int(now - rt.started), "ws_clients": len(rt.hub._clients),
                 "db_bytes": db_bytes, "tables": tables, "events_per_min": events_min,
                 "usd_per_hour": VM_A_HOURLY_USD},
        "vm_b": {"role": "live sim (10 Hz) · replay + experiment workers", "online": rt.sim_online,
                 "host": rt.sim_host, "ticks_per_s": rt.meters["ticks"].rate(), "policy_version": rt.sim_policy_version,
                 "usd_per_hour": VM_B_HOURLY_USD},
        "queue": queue, "workers": sorted(workers, key=lambda w: w["worker"]),
        "claims_per_s": rt.meters["claims"].rate(), "sims_total": int(sims_total),
        "capacity": capacity,
        "inference": {"provider": rt.settings.inference_provider if rt.settings.inference_enabled else "none",
                      "model": rt.settings.inference_model or "auto", **{k: int(v) for k, v in dict(llm).items()}},
        "ledger": {k: int(v) for k, v in dict(ledger).items()},
        "signing": {"mode": "ed25519" if rt.signer else "unsigned", "key_id": rt.signer.key_id if rt.signer else None,
                    "fleet": (rt.sim_host or {}).get("policy_signing")},
    }


__all__ = ["snapshot"]
