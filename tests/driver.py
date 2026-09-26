"""Test stand-in for the control plane: keeps jobs flowing and records telemetry."""
from __future__ import annotations

import random

from replay_core import capsule
from replay_core.detector import detect
from replay_core.dispatch import choose_robot, make_cmd, make_job
from replay_core.engine import make_frame
from replay_core.live import LiveSim
from replay_core.world import W


class Driver:
    def __init__(self, seed: int, rules: list[str] | None = None, job_seed: int = 1,
                 max_open: int = 4) -> None:
        self.sim = LiveSim(seed, rules)
        self.rng = random.Random(job_seed)
        self.max_open = max_open
        self.records: list[dict] = []
        self.snapshots: dict[int, dict] = {}
        self.pending: list[dict] = []
        self.open: set[str] = set()
        self.job_start: dict[str, int] = {}
        self.failures: list[dict] = []
        self.events: list[dict] = []
        self.frame = make_frame(self.sim.state, [], [])
        self._n = 0

    def _new_job(self) -> dict:
        self._n += 1
        # heavy (rack F) picks are rare: only one robot can lift them
        pool = [s for s in sorted(W.slots) if not s.startswith("F") or self.rng.random() < 0.08]
        slots = self.rng.sample(pool, self.rng.choice([1, 1, 1, 2]))
        return make_job(f"J{self._n}", slots, self.rng.choice(sorted(W.docks)), self.sim.tick_no)

    def dispatch(self) -> None:
        while len(self.pending) + len(self.open) < self.max_open:
            self.pending.append(self._new_job())
        taken: set[str] = set()
        for job in list(self.pending):
            rid = choose_robot(self.frame, job, taken)
            if rid:
                self.sim.submit(make_cmd(rid, job))
                self.pending.remove(job)
                self.open.add(job["id"])
                taken.add(rid)

    def run(self, ticks: int, stop=None) -> list[dict]:
        for _ in range(ticks):
            if self.sim.tick_no % 10 == 0:
                self.dispatch()
            rec = self.sim.tick()
            self.frame = rec.frame
            self.records.append({"tick": rec.tick, "inputs": rec.inputs, "hash": rec.hash,
                                 "frame": rec.frame})
            if rec.snapshot is not None:
                self.snapshots[rec.snapshot["tick"]] = rec.snapshot
            for inp in rec.inputs:
                if inp["kind"] == "cmd" and inp.get("job"):
                    self.job_start[inp["job"]["id"]] = rec.tick - 1
            for e in rec.frame["ev"]:
                self.events.append({"t": rec.tick, **e})
                if e["type"] in ("job_done", "job_exception"):
                    self.open.discard(e["job"])
            found = detect(rec.frame)
            self.failures.extend(found)
            if stop is not None and stop(found):
                return found
        return []

    def cut(self, failure: dict) -> dict:
        """Cut a capsule the way the control plane will. Runs the sim to T+2 s if needed."""
        fr = next(r for r in self.records if r["tick"] == failure["tick"])["frame"]
        job = next((r["job"] for r in fr["robots"] if r["id"] == failure["robot"]), None)
        job = job or failure["detail"].get("job")
        start, end = capsule.window(failure["tick"], self.job_start.get(job))
        if self.sim.tick_no < end:
            self.run(end - self.sim.tick_no)
        snap = capsule.pick_snapshot(sorted(self.snapshots), start)
        recs = [r for r in self.records if snap < r["tick"] <= end]
        return capsule.build("test-run", self.snapshots[snap], recs, failure)
