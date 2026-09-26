"""In-process state shared by the control plane's background loops and routes."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import asyncpg

from .bus import Hub
from .config import Settings
from .simnode_client import SimNode

if TYPE_CHECKING:
    from .orchestrator import Orchestrator


@dataclass
class Runtime:
    settings: Settings
    pool: asyncpg.Pool
    hub: Hub
    sim: SimNode
    orchestrator: "Orchestrator | None" = None
    run_id: str | None = None
    last_tick: int = 0
    frame: dict | None = None              # latest full frame from the live sim
    frame_at: float = 0.0                  # monotonic time it arrived
    sim_policy_version: int | None = None  # policy the live sim reports it is running
    auto_jobs: bool = True
    inflight: dict[str, float] = field(default_factory=dict)  # robot -> when we dispatched to it

    @property
    def sim_online(self) -> bool:
        return self.frame is not None and time.monotonic() - self.frame_at < 3.0

    def status(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "tick": self.last_tick, "sim_online": self.sim_online,
                "sim_policy_version": self.sim_policy_version, "auto_jobs": self.auto_jobs}
