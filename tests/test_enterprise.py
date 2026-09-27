"""Enterprise profile, orders and impact; audit ledger; signed policies; VM B witness."""
import asyncio
import datetime as dt
import json
import random

import httpx
import pytest

from control import enterprise, ledger
from replay_core.signing import SignatureError, Signer, Verifier
from replay_core.world import DESTINATION, ROBOT_SPECS, SPARE_SPECS, W
from simnode.app import Witness
from tests.test_services import settings


def test_generator_covers_every_face_consistently():
    p = enterprise.generator(random.Random(1), "copper", "Atacama, Chile")
    v = enterprise.validate(p, p)
    assert [c["slot"] for c in v["catalog"]] == sorted(W.slots)
    assert all(c["cls"] == W.slots[c["slot"]]["cls"] for c in v["catalog"])
    assert len({c["sku"] for c in v["catalog"]}) == len(W.slots)
    assert next(c for c in v["catalog"] if c["cls"] == "waste")["unit_value"] == 0
    assert {c["dock"] for c in v["carriers"]} == set(W.docks)
    assert [r["id"] for r in v["robots"]] == [s["id"] for s in ROBOT_SPECS + SPARE_SPECS]
    assert [e["slot"] for e in v["excavators"]] == sorted(W.slots)
    assert any(c["segment"] == "internal" for c in v["customers"])
    assert all(c["sla_hours"] == enterprise.TIERS[c["tier"]] for c in v["customers"])


def test_validate_keeps_good_model_output_and_repairs_the_rest():
    fill = enterprise.generator(random.Random(2), "iron ore", "Pilbara, Western Australia")
    raw = {"facility": {"company": "Red Ochre Minerals", "name": "Yandi North Pit", "code": "pil 7!",
                        "commodity": "iron ore", "shifts": ["Day 06-18"]},
           "catalog": [{"slot": "A", "sku": "roh-fe62", "name": "Hematite fines ore 62% Fe", "category": "ore",
                        "grade": "62.1% Fe", "unit_value": 96.5, "unit_weight": 262},
                       {"slot": "B", "sku": "ROH-FE62", "name": "Duplicate code", "unit_value": -4, "unit_weight": 9000},
                       {"slot": "Z", "sku": "nope"}],
           "customers": [{"name": "Kobe Steelworks", "segment": "steel mill", "tier": "diamond"}],
           "cost_model": {"collision_usd": 250000, "mispick_usd": "abc"}}
    v = enterprise.validate(raw, fill)
    assert v["facility"]["company"] == "Red Ochre Minerals" and v["facility"]["code"] == "PIL7"
    assert v["facility"]["location"] == fill["facility"]["location"]
    a, b = v["catalog"][0], v["catalog"][1]
    assert a["sku"] == "ROH-FE62" and a["uom"] == "t" and a["unit_value"] == 96.5 and a["grade"] == "62.1% Fe"
    assert b["sku"] == fill["catalog"][1]["sku"]              # duplicate code replaced
    assert b["unit_value"] >= 0 and b["unit_weight"] <= 320  # nonsense clamped
    assert len(v["catalog"]) == len(W.slots)                 # unknown face ignored, missing ones filled
    assert v["customers"][0] == {"id": "C-1001", "name": "Kobe Steelworks", "segment": "steel mill",
                                 "tier": "standard", "sla_hours": 24}
    assert v["customers"][-1]["segment"] == "internal"       # waste loads always have somewhere to go
    assert v["cost_model"]["collision_usd"] == 250000 and v["cost_model"]["mispick_usd"] == fill["cost_model"]["mispick_usd"]


def test_vultr_profile_uses_json_mode():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        prof = enterprise.generator(random.Random(6), "copper", "Arizona Copper Belt")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(prof)}}]})

    prof, source, _ = asyncio.run(enterprise.generate(settings("k", provider="vultr", model="m"), random.Random(3),
                                                      httpx.MockTransport(handler)))
    assert source == "vultr:m" and seen["response_format"] == {"type": "json_object"} and len(prof["catalog"]) == len(W.slots)


def test_model_failure_falls_back_to_the_generator():
    down = httpx.MockTransport(lambda req: httpx.Response(500, text="down"))
    prof, source, _ = asyncio.run(enterprise.generate(settings("k", provider="vultr", model="m"),
                                                      random.Random(4), down))
    assert source == "generator" and len(prof["catalog"]) == len(W.slots)


def _site():
    p = enterprise.validate(enterprise.generator(random.Random(7), "copper", "Atacama, Chile"),
                            enterprise.generator(random.Random(7), "copper", "Atacama, Chile"))
    return {**p, "code": p["facility"]["code"], "name": p["facility"]["name"],
            "catalog": {c["slot"]: c for c in p["catalog"]}}


def test_load_tickets_follow_the_material():
    site, rng = _site(), random.Random(9)
    for i, slot in enumerate(sorted(W.slots) * 50):
        o = enterprise.make_order(site, rng, 104200 + i, slot)
        assert len(o["lines"]) == 1 and o["lines"][0]["slot"] == slot
        assert o["dock"] == DESTINATION[W.slots[slot]["cls"]]
        assert 150 <= o["lines"][0]["qty"] <= 320
        assert o["value"] == round(o["lines"][0]["qty"] * o["lines"][0]["unit_value"], 2)
        assert o["ship_by"] > dt.datetime.now(dt.timezone.utc)
        if W.slots[slot]["cls"] == "waste":
            assert o["value"] == 0 and "internal" in o["customer"].lower()


def test_impact_prices_incidents_from_the_cost_model():
    site = _site()
    cm = site["cost_model"]
    order = {"id": "LD-1", "customer_id": "C-1001", "value": 24812.5, "priority": "standard", "status": "picking",
             "carrier": "Primary crusher 1", "dock": "DK1"}
    assert enterprise.impact(site, "wrong_item", order)["estimated_cost_usd"] == round(cm["mispick_usd"] + 24812.5, 2)
    assert enterprise.impact(site, "zone_breach", None)["estimated_cost_usd"] == cm["safety_incident_usd"]
    assert enterprise.impact(site, "collision", order)["order"]["value"] == 24812.5


# ---------------------------------------------------------------- audit ledger

def _events(n):
    base = dt.datetime(2026, 9, 27, tzinfo=dt.timezone.utc)
    return [{"id": i, "ts": base + dt.timedelta(seconds=i), "run_id": "run-1", "tick": i * 10, "robot_id": "R1",
             "type": "sim.pick", "payload": {"slot": "A1", "v": 1.5, "n": i}} for i in range(1, n + 1)]


def test_merkle_proofs_verify_every_leaf_and_catch_changes():
    for n in (1, 2, 3, 7, 16, 33):
        leaves = [ledger.leaf(e) for e in _events(n)]
        root = ledger.merkle_root(leaves)
        for i in range(n):
            assert ledger.check_proof(leaves[i], ledger.merkle_proof(leaves, i), root)
        forged = ledger.leaf({**_events(n)[0], "payload": {"slot": "A2"}})
        assert not ledger.check_proof(forged, ledger.merkle_proof(leaves, 0), root)


def test_leaf_is_canonical_and_content_sensitive():
    e = _events(1)[0]
    shuffled = {**e, "payload": dict(reversed(list(e["payload"].items())))}
    assert ledger.leaf(e) == ledger.leaf(shuffled)
    assert ledger.leaf(e) != ledger.leaf({**e, "tick": 11})
    assert ledger.leaf(e) != ledger.leaf({**e, "payload": {**e["payload"], "v": 1.6}})


def test_block_chain_links():
    h1 = ledger.block_hash(1, 1, 10, 10, "r1", ledger.GENESIS)
    h2 = ledger.block_hash(2, 11, 20, 10, "r2", h1)
    assert h2 != ledger.block_hash(2, 11, 20, 10, "r2", ledger.block_hash(1, 1, 10, 10, "rX", ledger.GENESIS))


def test_witness_refuses_to_rewrite_history(tmp_path):
    path = tmp_path / "witness.jsonl"
    w = Witness(str(path))
    assert w.add({"n": 1, "hash": "aa"}) and not w.add({"n": 1, "hash": "aa"})
    with pytest.raises(ValueError):
        w.add({"n": 1, "hash": "bb"})
    assert Witness(str(path)).blocks[1]["hash"] == "aa"   # survives a restart of the sim node


# ---------------------------------------------------------------- signed policies

def test_policy_signatures():
    signer = Signer.generate()
    ver = Verifier(signer.public_pem())
    assert ver.key_id == signer.key_id
    rules = ["speed_cap(racks, 0.6)"]
    sig = signer.sign(4, rules)
    ver.verify(4, rules, sig)
    for version, rs, s in ((5, rules, sig), (4, ["speed_cap(racks, 1.2)"], sig), (4, rules, None), (4, rules, "AAAA")):
        with pytest.raises(SignatureError):
            ver.verify(version, rs, s)
    other = Signer.generate()
    with pytest.raises(SignatureError):
        ver.verify(4, rules, other.sign(4, rules))
