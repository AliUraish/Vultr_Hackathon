"""The live fleet loop's core, with the flight recorder built in.

LiveSim owns the state, queues external inputs, resolves chaos requests, and
for every tick returns exactly what must be stored to replay it later: the
inputs applied, the resulting state hash, the frame, and (every
SNAPSHOT_EVERY ticks) a full snapshot taken before the tick's inputs.
The network service on VM B wraps this; tests drive it directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .engine import make_frame, step
from .hashing import clone
from .policy import make_policy
from .scenarios import resolve
from .state import initial_state, state_hash
from .world import SNAPSHOT_EVERY


@dataclass
class TickRecord:
    tick: int                    # state tick after this step
    inputs: list[dict]           # applied at the start of the step (at tick - 1)
    hash: str                    # hash of the state after the step
    frame: dict
    snapshot: dict | None        # state at tick - 1, before inputs, on snapshot ticks


@dataclass
class ChaosRequest:
    id: str
    scenario: str
    expires: int
    hints: dict = field(default_factory=dict)
    created: int = 0

    def effective_hints(self, tick: int) -> dict:
        hints = dict(self.hints)
        fallback = hints.pop("fallback_zone", None)
        after = hints.pop("fallback_after", 0)
        if fallback and tick - self.created >= after:
            hints["zone"] = fallback
        return hints


class LiveSim:
    def __init__(self, seed: int, rules: list[str] | None = None, version: int = 1) -> None:
        self.state = initial_state(seed, make_policy(rules or [], version))
        self._queue: list[dict] = []
        self._chaos: list[ChaosRequest] = []
        self._seq = 0
        self.expired: list[ChaosRequest] = []

    @property
    def tick_no(self) -> int:
        return self.state["tick"]

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def submit(self, inp: dict) -> str:
        """Queue an external input for the next tick. Returns its id."""
        inp = dict(inp)
        inp.setdefault("id", self._next_id("in"))
        self._queue.append(inp)
        return inp["id"]

    def request_chaos(self, scenario: str, wait_ticks: int = 300, hints: dict | None = None) -> str:
        req = ChaosRequest(self._next_id("chaos"), scenario, self.tick_no + wait_ticks, dict(hints or {}),
                           self.tick_no)
        self._chaos.append(req)
        return req.id

    def tick(self) -> TickRecord:
        t = self.tick_no
        snapshot = clone(self.state) if t % SNAPSHOT_EVERY == 0 else None
        inputs, self._queue = self._queue, []
        pending: list[ChaosRequest] = []
        for req in self._chaos:
            got = resolve(self.state, req.scenario, req.effective_hints(t))
            if got:
                for g in got:
                    g["id"] = self._next_id("in")
                    g["req"] = req.id
                    inputs.append(g)
            elif t < req.expires:
                pending.append(req)
            else:
                self.expired.append(req)
        self._chaos = pending
        events = step(self.state, inputs)
        return TickRecord(self.tick_no, inputs, state_hash(self.state),
                          make_frame(self.state, events, inputs), snapshot)
