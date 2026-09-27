"""Test stand-in for the control plane: keeps jobs flowing and records telemetry."""
from __future__ import annotations

import random

from replay_core import capsule
from replay_core.detector import detect
from replay_core.dispatch import choose_robot, make_cmd, make_job
from replay_core.engine import make_frame
from replay_core.live import LiveSim
from replay_core.world import DESTINATION, W


class Driver:
    def __init__(self, seed: int, rules: list[str] | None = None, job_seed: int = 1,
                 per_face: int = 2) -> None:
        self.sim = LiveSim(seed, rules)
        self.rng = random.Random(job_seed)
        self.per_face = per_face
        self.records: list[dict] = []
        self.snapshots: dict[int, dict] = {}
        self.pending: list[dict] = []
        self.open: set[str] = set()
        self.job_start: dict[str, int] = {}
        self.failures: list[dict] = []
        self.events: list[dict] = []
        self.frame = make_frame(self.sim.state, [], [])
        self._n = 0

    def _new_job(self, slot: str) -> dict:
        self._n += 1
        return make_job(f"J{self._n}", [slot], DESTINATION[W.slots[slot]["cls"]], self.sim.tick_no)

    def dispatch(self) -> None:
        """Every dig face keeps up to `per_face` load tickets open (one loading, one on its way)."""
        face = {j: s for j, s in self.face.items()} if hasattr(self, "face") else {}
        self.face = face
        for slot in sorted(W.slots):
            while sum(1 for j in list(self.open) + [p["id"] for p in self.pending] if face.get(j) == slot) < self.per_face:
                job = self._new_job(slot)
                face[job["id"]] = slot
                self.pending.append(job)
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
