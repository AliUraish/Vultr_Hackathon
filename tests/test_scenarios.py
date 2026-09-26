"""The three canned failures: each reproduces 3/3, the right fix avoids it, a wrong one doesn't."""
from dataclasses import dataclass

import pytest

from replay_core import capsule
from replay_core.world import W
from tests.driver import Driver


@dataclass
class Case:
    scenario: str
    failure: str
    seed: int
    wrong_fix: list[str]


CASES = [
    Case("pallet_drop", "collision", 1, ["min_clearance(0.4)"]),
    Case("mislabel_bin", "wrong_item", 2, ["speed_cap(racks, 0.5)"]),
    Case("worker_in_aisle", "zone_breach", 1, ["speed_cap(racks, 0.5)"]),
]


def right_fix(case: Case, failure: dict, d: Driver) -> list[str]:
    if case.scenario == "pallet_drop":
        return ["speed_cap(racks, 0.5)"]
    if case.scenario == "worker_in_aisle":
        return [f"reroute_avoid({failure['detail']['zone']})"]
    slot = next(e["slot"] for e in d.events if e["type"] == "bin_mislabeled")
    return [f"require_scan_confirm({W.slots[slot]['cls']})"]


@pytest.fixture(scope="module", params=CASES, ids=[c.scenario for c in CASES])
def run(request):
    case: Case = request.param
    d = Driver(seed=case.seed, job_seed=case.seed)
    d.run(300)
    d.sim.request_chaos(case.scenario, wait_ticks=600)
    found = d.run(2500, stop=lambda fs: any(f["type"] == case.failure for f in fs))
    failure = next((f for f in found if f["type"] == case.failure), None)
    assert failure is not None, f"{case.scenario} never produced a {case.failure}"
    cap = d.cut(failure)
    return case, d, failure, cap


def test_reproduces_three_of_three(run):
    _, _, _, cap = run
    results = [capsule.run(cap, keep_frames=False) for _ in range(3)]
    assert [r["outcome"] for r in results] == ["reproduced"] * 3
    assert all(r["matches_live"] for r in results)
    assert len({r["trajectory_hash"] for r in results}) == 1


def test_cause_is_inside_the_capsule(run):
    _, _, _, cap = run
    assert any(i["kind"] == "chaos" for _, ins in cap["inputs"] for i in ins)


def test_right_fix_avoids_the_failure(run):
    case, d, failure, cap = run
    result = capsule.run(cap, right_fix(case, failure, d), 2, keep_frames=False)
    assert result["outcome"] == "avoided", result["new_failures"] or result["failures"]


def test_wrong_fix_does_not(run):
    case, _, _, cap = run
    assert capsule.run(cap, case.wrong_fix, 2, keep_frames=False)["outcome"] == "reproduced"
