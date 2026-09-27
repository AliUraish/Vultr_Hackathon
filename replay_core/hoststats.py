"""Host load, memory and CPU count for the platform page (Linux /proc; partial elsewhere)."""
from __future__ import annotations

import os
from pathlib import Path


def host_stats() -> dict:
    out: dict = {"cpus": os.cpu_count()}
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
        mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        total, avail = int(mem["MemTotal"].split()[0]), int(mem["MemAvailable"].split()[0])
        out["mem_used_pct"] = round((1 - avail / total) * 100, 1)
        out["mem_total_mb"] = total // 1024
        out["uptime_s"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, KeyError, ValueError, IndexError):
        pass
    return out
