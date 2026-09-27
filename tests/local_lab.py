"""In-process stand-in for control.lab.Lab: same instruments and result shapes, no Postgres or workers."""
from __future__ import annotations

from replay_core import capsule as caps
from replay_core import experiments as xp
from replay_core.policy import normalize

MIN_ROBUSTNESS = 0.95


class LocalLab:
    def __init__(self, capsules: dict[int, dict]) -> None:
        self.capsules = capsules
        self.calls: list[tuple[str, dict]] = []
        self._eid = 0
        self._base: dict[tuple, list[dict]] = {}

    def _next(self, kind: str, **params) -> int:
        self._eid += 1
        self.calls.append((kind, params))
        return self._eid

    async def reproduce(self, cid: int, times: int = 3, inv: int | None = None) -> dict:
        cap = self.capsules[cid]
        runs = [caps.run(cap, keep_frames=False) for _ in range(times)]
        same = len({r["trajectory_hash"] for r in runs}) == 1
        ok = all(r["matches_live"] and r["outcome"] == "reproduced" for r in runs)
        return {"experiment": self._next("reproduce", times=times), "sims": times, "identical": same,
                "matches_live": ok, "reproduced": ok, "hash": runs[0]["trajectory_hash"],
                "summary": f"{times}/{times} reproduced" if same and ok else "not deterministic"}

    async def isolate(self, cid: int, budget: int = 90, inv: int | None = None) -> dict:
        res = xp.isolate(self.capsules[cid], budget)
        return {"experiment": self._next("isolate"), "sims": res["tested"], **res}

    async def what_if(self, cid: int, rules: list[str], inv: int | None = None) -> dict:
        cap = self.capsules[cid]
        extra = normalize(rules)
        recorded = caps.policy_at(cap, cap["failure"]["tick"])
        res = caps.run(cap, recorded + extra, keep_frames=False)
        first = next((f for f in res["failures"] if f["type"] == cap["failure"]["type"]), None)
        return {"experiment": self._next("what_if", rules=extra), "sims": 2, "rules": extra,
                "outcome": res["outcome"], "first_failure": first, "new_failures": res["new_failures"],
                "warnings": res["warnings"], "summary": f"with {' + '.join(extra)}: {res['outcome']}"}

    def _baseline(self, cid: int, n: int, seed: int) -> tuple[list[dict], list[dict], int]:
        cap = self.capsules[cid]
        variants = xp.make_variants(cap, n, seed)
        key = (cid, n, seed)
        if key in self._base:
            return variants, self._base[key], 0
        self._base[key] = [xp.run_variant(cap, xp.baseline_rules(cap), v) for v in variants]
        return variants, self._base[key], n

    def _stress(self, cid: int, fix: str, n: int, seed: int) -> tuple[dict, int]:
        cap = self.capsules[cid]
        variants, base, sims = self._baseline(cid, n, seed)
        rules = xp.baseline_rules(cap)
        cand = [xp.run_variant(cap, rules + [fix], v) for v in variants]
        return xp.aggregate(variants, base, cand), sims + n

    async def stress(self, cid: int, fix: str, n: int | None = None, seed: int = 0, inv: int | None = None) -> dict:
        n = max(6, min(int(n or 30), 60))
        fix = normalize([fix])[0]
        agg, sims = self._stress(cid, fix, n, seed)
        return {"experiment": self._next("stress", fix=fix, n=n), "sims": n, "baseline_sims": sims - n, "fix": fix, "n": n,
                "passes": (agg["robustness"] or 0) >= MIN_ROBUSTNESS, **agg,
                "summary": f"{fix}: robustness {agg['robustness']}, cost {agg['cost_pct']}%"}

    async def tune(self, cid: int, fix: str, values: list[float] | None = None, n: int | None = None,
                   seed: int = 0, inv: int | None = None) -> dict:
        n = max(6, min(int(n or 30), 60))
        points, sims = [], 0
        for p in xp.sweep(fix, values):
            agg, s = self._stress(cid, p, n, seed)
            sims += s
            points.append({"fix": p, "setting": xp.setting(p), **agg})
        best = xp.pick_setting(points, MIN_ROBUSTNESS)
        return {"experiment": self._next("tune", fix=fix), "sims": sims, "points": points,
                "best": best["fix"] if best else None, "baseline_robustness": points[0]["baseline_robustness"],
                "summary": f"best {best['fix'] if best else None}"}


class MemoryRecorder:
    def __init__(self) -> None:
        self.inv = 1
        self.steps: dict[int, dict] = {}
        self.notes: list[str] = []

    async def step(self, n: int, tool: str, args: dict, why: str) -> None:
        assert n not in self.steps, f"step {n} recorded twice"
        self.steps[n] = {"tool": tool, "args": args, "why": why, "status": "running"}

    async def step_done(self, n: int, status: str, summary: str, experiment: int | None) -> None:
        self.steps[n].update(status=status, summary=summary, experiment=experiment)

    async def note(self, text: str) -> None:
        self.notes.append(text)
