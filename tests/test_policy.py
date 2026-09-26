import pytest

from replay_core.policy import PolicyError, compile_rules, make_policy, normalize, parse_rule
from replay_core.world import W


def test_normalizes_spacing_quotes_and_numbers():
    assert normalize(["speed_cap( racks , 0.50 )", "require_scan_confirm('loose_small')"]) == [
        "speed_cap(racks, 0.5)", "require_scan_confirm(loose_small)",
    ]
    assert str(parse_rule("min_clearance(1)")) == "min_clearance(1.0)"


@pytest.mark.parametrize("bad", [
    "rm_rf(/)",                           # not a fix type
    "speed_cap(nowhere, 0.5)",            # unknown zone
    "speed_cap(racks, 5)",                # faster than the robots can go
    "speed_cap(racks)",                   # missing argument
    "min_clearance(abc)",
    "reroute_avoid(c0_0)",                # a wall
    "reorder_steps(multi, random)",
    "require_scan_confirm(explosives)",
    "__import__('os').system('x')",
    "",
])
def test_rejects_anything_outside_the_dsl(bad):
    with pytest.raises(PolicyError):
        parse_rule(bad)


def test_compiles_to_integer_units():
    c = compile_rules([
        "speed_cap(racks, 0.5)", "speed_cap(racks, 0.8)",   # the stricter cap wins
        "min_clearance(0.4)",
        "reroute_avoid(c12_3)", "reroute_avoid(aisle_3W)",
        "require_scan_confirm(fragile)",
        "reorder_steps(multi, nearest_first)",
    ])
    assert c["caps"] == {"racks": 50}
    assert c["clearance"] == 400
    assert [12, 3] in c["avoid"] and len(c["avoid"]) == 1 + len(W.zones["aisle_3W"])
    assert c["scan"] == ["fragile"]
    assert c["reorder"] == {"multi": "nearest_first"}


def test_policy_hash_is_stable_and_order_sensitive():
    a = make_policy(["speed_cap(racks, 0.5)", "min_clearance(0.4)"], 2)
    b = make_policy(["speed_cap( racks,0.50)", "min_clearance(0.40)"], 7)
    c = make_policy(["min_clearance(0.4)", "speed_cap(racks, 0.5)"], 2)
    assert a["hash"] == b["hash"]
    assert a["hash"] != c["hash"]
