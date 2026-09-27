"""In-process state shared by the control plane's background loops and routes."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import asyncpg

from .bus import Hub
from .config import Settings
from .simnode_client import SimNode

if TYPE_CHECKING:
    from .lab import Lab
    from .orchestrator import Orchestrator


class Meter:
    """Counts per second over a sliding window (telemetry ticks, WebSocket messages...)."""

    def __init__(self, window: float = 60.0) -> None:
        self.window = window
        self.q: deque[tuple[float, int]] = deque()
        self.total = 0

    def add(self, n: int = 1) -> None:
        now = time.monotonic()
        self.q.append((now, n))
        self.total += n
        while self.q and self.q[0][0] < now - self.window:
            self.q.popleft()

    def rate(self) -> float:
        now = time.monotonic()
        while self.q and self.q[0][0] < now - self.window:
            self.q.popleft()
        return round(sum(n for _, n in self.q) / self.window, 2)


@dataclass
class Runtime:
    settings: Settings
    pool: asyncpg.Pool
    hub: Hub
    sim: SimNode
    orchestrator: "Orchestrator | None" = None
    lab: "Lab | None" = None
    site: dict | None = None               # the enterprise profile (catalog, customers, cost model)
    service: Any = None                    # control.service.ServiceDesk
    signer: Any = None                     # replay_core.signing.Signer when a policy signing key is configured
    sim_host: dict = field(default_factory=dict)   # VM B host + tick-loop stats from telemetry
    workers: dict[str, dict] = field(default_factory=dict)  # worker id -> {last_claim, jobs, busy_ms}
    meters: dict[str, Meter] = field(default_factory=lambda: {"ticks": Meter(), "claims": Meter()})
    started: float = field(default_factory=time.time)
    run_id: str | None = None
    last_tick: int = 0
    frame: dict | None = None              # latest full frame from the live sim
    frame_at: float = 0.0                  # monotonic time it arrived
    sim_policy_version: int | None = None  # policy the live sim reports it is running
    auto_jobs: bool = True
    inflight: dict[str, float] = field(default_factory=dict)  # robot -> when we dispatched to it
    dispatcher: Any = None                 # control.fleet.Dispatcher
    traffic: Any = None                    # control.traffic.TrafficDesk
    faces: dict[str, dict] = field(default_factory=dict)      # dig face -> {trucks coming, seconds alone}
    decisions: deque = field(default_factory=lambda: deque(maxlen=120))  # recent AI / rule decisions for the UI
    aiops: Any = None                      # control.aiops.AIOps: AI drivers, auditor, road crew, supervisor...
    decision_seq: int = 0

    def decide(self, d: dict) -> None:
        """Record a decision (dispatch, traffic, service, roads, hazard, autopilot...), stream it to the browsers
        and queue it for the auditor model."""
        self.decision_seq += 1
        d = {"id": self.decision_seq, "at": time.time(), "tick": self.last_tick, **d}
        self.decisions.append(d)
        self.hub.publish("decision", d)
        if self.aiops is not None:
            self.aiops.auditor.submit(d)

    @property
    def sim_online(self) -> bool:
        return self.frame is not None and time.monotonic() - self.frame_at < 10.0

    def status(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "tick": self.last_tick, "sim_online": self.sim_online,
                "sim_policy_version": self.sim_policy_version, "auto_jobs": self.auto_jobs}
