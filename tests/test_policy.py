import pytest

from replay_core.policy import PolicyError, compile_rules, make_policy, normalize, parse_rule
from replay_core.world import W


def test_normalizes_spacing_quotes_and_numbers():
    assert normalize(["speed_cap( benches , 20.0 )", "grade_check('ore')"]) == [
        "speed_cap(benches, 20)", "grade_check(ore)",
    ]
    assert str(parse_rule("min_clearance(6.50)")) == "min_clearance(6.5)"


@pytest.mark.parametrize("bad", [
    "rm_rf(/)",                           # not a fix type
    "speed_cap(nowhere, 20)",             # unknown zone
    "speed_cap(benches, 90)",             # faster than a haul truck can go
    "speed_cap(benches)",                 # missing argument
    "min_clearance(abc)",
    "min_clearance(0.2)",                 # below the 1 m floor
    "reroute_avoid(c0_0)",                # rock
    "reorder_steps(multi, random)",
    "grade_check(gold_nuggets)",
    "__import__('os').system('x')",
    "",
])
def test_rejects_anything_outside_the_dsl(bad):
    with pytest.raises(PolicyError):
        parse_rule(bad)


def test_compiles_to_integer_units():
    c = compile_rules([
        "speed_cap(benches, 20)", "speed_cap(benches, 30)",   # the stricter cap wins
        "min_clearance(6)",
        "reroute_avoid(c14_10)", "reroute_avoid(bench_upper_w)",
        "grade_check(ore)",
    ])
    assert c["caps"] == {"benches": 555}                     # 20 km/h = 5.55 m/s = 555 mm per tick
    assert c["clearance"] == 6000
    assert [14, 10] in c["avoid"] and len(c["avoid"]) == 1 + len(W.zones["bench_upper_w"])
    assert c["scan"] == ["ore"]


def test_policy_hash_is_stable_and_order_sensitive():
    a = make_policy(["speed_cap(benches, 20)", "min_clearance(6)"], 2)
    b = make_policy(["speed_cap( benches,20.0)", "min_clearance(6.00)"], 7)
    c = make_policy(["min_clearance(6)", "speed_cap(benches, 20)"], 2)
    assert a["hash"] == b["hash"]
    assert a["hash"] != c["hash"]
