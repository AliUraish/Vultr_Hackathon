"""The investigator's instruments: variants, stress tests, tuning and cause isolation."""
import pytest

from replay_core import experiments as xp
from replay_core.policy import PolicyError
from simnode.worker import run_job
from tests.driver import Driver
from tests.test_scenarios import CASES, right_fix

N = 12


@pytest.fixture(scope="module")
def cases():
    out = []
    for case in CASES:
        d = Driver(seed=case.seed, job_seed=case.seed)
        d.run(400)
        d.sim.request_chaos(case.scenario, wait_ticks=1200)
        found = d.run(3500, stop=lambda fs, c=case: any(f["type"] == c.failure for f in fs))
        failure = next(f for f in found if f["type"] == case.failure)
        cap = d.cut(failure)
        variants = xp.make_variants(cap, N, 0)
        base = [xp.run_variant(cap, xp.baseline_rules(cap), v) for v in variants]
        out.append((case, cap, d, variants, base))
    return out


def test_variants_are_deterministic_and_start_with_the_incident(cases):
    for case, cap, _, variants, _ in cases:
        assert variants == xp.make_variants(cap, N, 0)
        assert variants != xp.make_variants(cap, N, 1)
        assert variants[0]["label"] == "the incident, re-created" and variants[0]["reseed"] is None
        assert variants[0]["hazard"]["scenario"] == xp.HAZARD[case.failure]
        assert all(v["label"] for v in variants)


def test_variant_runs_are_reproducible(cases):
    _, cap, _, variants, base = cases[0]
    again = xp.run_variant(cap, xp.baseline_rules(cap), variants[3])
    assert again == base[3]


def test_the_incident_recreated_fails_under_todays_policy(cases):
    for case, _, _, _, base in cases:
        assert base[0]["fired"] and base[0]["target"], case.scenario
        # the hazard is not a fluke: it causes the failure in most variants
        placed = [b for b in base if b["fired"]]
        assert len(placed) >= N / 2 and sum(b["target"] for b in placed) >= len(placed) * 0.6, case.scenario


def test_right_fix_is_robust_and_wrong_fix_is_not(cases):
    for case, cap, d, variants, base in cases:
        rules = xp.baseline_rules(cap)
        fix = right_fix(case, cap["failure"], d)[0]
        good = xp.aggregate(variants, base, [xp.run_variant(cap, rules + [fix], v) for v in variants])
        bad = xp.aggregate(variants, base, [xp.run_variant(cap, rules + case.wrong_fix, v) for v in variants])
        assert good["robustness"] >= 0.9 and good["prevented"] >= good["baseline_failed"] - 1, (case.scenario, good)
        assert bad["robustness"] <= 0.5 and bad["prevented"] == 0, (case.scenario, bad)
        assert good["baseline_robustness"] == bad["baseline_robustness"] <= 0.4
        assert {t["outcome"] for t in good["tiles"]} <= {"prevented", "clean", "still_fails", "introduced", "not_placed"}
        assert len(good["tiles"]) == N


def test_speed_caps_cost_throughput(cases):
    case, cap, _, variants, base = cases[0]
    rules = xp.baseline_rules(cap)
    slow = xp.aggregate(variants, base, [xp.run_variant(cap, rules + ["speed_cap(benches, 10)"], v) for v in variants])
    fast = xp.aggregate(variants, base, [xp.run_variant(cap, rules + ["speed_cap(benches, 35)"], v) for v in variants])
    assert slow["cost_pct"] > fast["cost_pct"] > 0
    assert slow["robustness"] > fast["robustness"]


def test_sweep_and_pick():
    assert xp.sweep("speed_cap(benches, 20)", [15, 25, 25]) == ["speed_cap(benches, 15)", "speed_cap(benches, 25)"]
    assert len(xp.sweep("min_clearance(6)")) >= 5
    assert xp.setting("speed_cap(benches, 30)") == 30 and xp.setting("respect_closures(*)") is None
    with pytest.raises(PolicyError):
        xp.sweep("respect_closures(benches)")
    with pytest.raises(PolicyError):
        xp.sweep("speed_cap(benches, 20)", [90.0])
    pts = [{"fix": "a", "robustness": 1.0, "cost_pct": 30.0}, {"fix": "b", "robustness": 0.96, "cost_pct": 12.0},
           {"fix": "c", "robustness": 0.8, "cost_pct": 5.0}]
    assert xp.pick_setting(pts, 0.95)["fix"] == "b"
    assert xp.pick_setting(pts, 1.1) is None


def test_isolate_finds_the_minimal_cause(cases):
    for case, cap, _, _, _ in cases:
        res = xp.isolate(cap)
        assert res["ok"] and res["tested"] <= 90 and res["summary"]
        kinds = {m["kind"] for m in res["minimal"]}
        assert "chaos" in kinds, case.scenario  # the injected hazard is always part of the cause
        if case.scenario != "rockfall":  # a grade mix-up or a closed road needs just one load to meet it
            assert len(res["minimal"]) < res["total"]


def test_worker_runs_experiment_jobs(cases):
    case, cap, d, variants, base = cases[1]
    rules = xp.baseline_rules(cap)
    out = run_job({"kind": "variants", "spec": {"rules": rules, "variants": variants[:3]}}, cap)
    assert out["status"] == "done" and out["result"]["runs"] == base[:3] and out["result"]["sims"] == 3
    iso = run_job({"kind": "isolate", "spec": {"budget": 40}}, cap)
    assert iso["status"] == "done" and iso["result"]["ok"]
    rend = run_job({"kind": "render", "spec": {"rules": rules, "variant": variants[0]}}, cap)
    assert rend["status"] == "done" and rend["frames"] and rend["result"]["run"]["target"]
    whatif = run_job({"kind": "whatif", "policy_rules": rules + right_fix(case, cap["failure"], d),
                      "policy_version": None, "control_failures": None, "control_rules": None}, cap)
    assert whatif["outcome"] == "avoided" and whatif["frames"] and whatif["result"]["first_failure"] is None
    broken = run_job({"kind": "variants", "spec": {"rules": ["nonsense(1)"], "variants": variants[:1]}}, cap)
    assert broken["status"] == "error" and "PolicyError" in broken["error"]
