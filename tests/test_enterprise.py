"""Enterprise profile, orders and impact; audit ledger; signed policies; VM B witness."""
import asyncio
import datetime as dt
import json
import random

import httpx
import pytest

from control import enterprise, ledger
from replay_core.signing import SignatureError, Signer, Verifier
from replay_core.world import W
from simnode.app import Witness
from tests.test_services import settings


def test_generator_covers_every_slot_consistently():
    p = enterprise.generator(random.Random(1), "lab consumables", "Reno, NV")
    v = enterprise.validate(p, p)
    assert [c["slot"] for c in v["catalog"]] == sorted(W.slots)
    assert all(c["cls"] == W.slots[c["slot"]]["cls"] for c in v["catalog"])
    assert len({c["sku"] for c in v["catalog"]}) == len(W.slots)
    assert {c["dock"] for c in v["carriers"]} == set(W.docks)
    assert [r["id"] for r in v["robots"]] == ["R1", "R2", "R3", "R4"]
    assert all(c["sla_hours"] == enterprise.TIERS[c["tier"]] for c in v["customers"])


def test_validate_keeps_good_model_output_and_repairs_the_rest():
    fill = enterprise.generator(random.Random(2), "x", "Dallas, TX")
    raw = {"facility": {"company": "Halcyon Medical Supply", "name": "Dallas South DC", "code": "dal 7!",
                        "sq_ft": "not a number", "shifts": ["Day 06-14"]},
           "catalog": [{"slot": "A1", "sku": "hms-1001", "name": "Exam Gloves Nitrile M (100)", "category": "PPE",
                        "uom": "Box", "unit_value": 11.5, "unit_weight": 0.6},
                       {"slot": "A2", "sku": "HMS-1001", "name": "Duplicate SKU", "unit_value": -4},
                       {"slot": "Z9", "sku": "nope"}],
           "customers": [{"name": "Riverbend Clinics", "segment": "healthcare", "tier": "diamond"}],
           "cost_model": {"collision_usd": 9000, "mispick_usd": "abc"}}
    v = enterprise.validate(raw, fill)
    assert v["facility"]["company"] == "Halcyon Medical Supply" and v["facility"]["code"] == "DAL7"
    assert v["facility"]["sq_ft"] == fill["facility"]["sq_ft"]
    a1, a2 = v["catalog"][0], v["catalog"][1]
    assert a1["sku"] == "HMS-1001" and a1["uom"] == "box" and a1["unit_value"] == 11.5
    assert a2["sku"] == fill["catalog"][1]["sku"]            # duplicate SKU replaced
    assert a2["unit_value"] >= 0.05                          # negative value clamped
    assert len(v["catalog"]) == len(W.slots)                 # unknown slot ignored, missing ones filled
    assert v["customers"] == [{"id": "C-1001", "name": "Riverbend Clinics", "segment": "healthcare",
                               "tier": "standard", "sla_hours": 24}]
    assert v["cost_model"]["collision_usd"] == 9000 and v["cost_model"]["mispick_usd"] == fill["cost_model"]["mispick_usd"]


def test_model_writes_the_profile_with_a_strict_schema():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-x"}]})
        body = json.loads(req.content)
        seen.update(body)
        prof = enterprise.generator(random.Random(5), "y", "Memphis, TN")
        prof["facility"]["company"] = "Tallgrass Outfitters"
        return httpx.Response(200, json={"output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(prof)}]}], "usage": {"input_tokens": 900, "output_tokens": 4000}})

    s = settings("k", provider="openai", model="")
    prof, source, theme = asyncio.run(enterprise.generate(s, random.Random(3), httpx.MockTransport(handler)))
    assert source == "openai:gpt-x" and prof["facility"]["company"] == "Tallgrass Outfitters"
    fmt = seen["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] and fmt["schema"]["additionalProperties"] is False
    assert theme["industry"] in enterprise.INDUSTRIES and "A1=boxed" in seen["input"]


def test_model_failure_falls_back_to_the_generator():
    down = httpx.MockTransport(lambda req: httpx.Response(500, text="down"))
    prof, source, _ = asyncio.run(enterprise.generate(settings("k", provider="openai", model="m"),
                                                      random.Random(4), down))
    assert source == "generator" and len(prof["catalog"]) == len(W.slots)


def _site():
    p = enterprise.validate(enterprise.generator(random.Random(7), "x", "Reno, NV"),
                            enterprise.generator(random.Random(7), "x", "Reno, NV"))
    return {**p, "code": p["facility"]["code"], "name": p["facility"]["name"],
            "catalog": {c["slot"]: c for c in p["catalog"]}}


def test_orders_are_consistent_with_the_catalog():
    site, rng = _site(), random.Random(9)
    orders = [enterprise.make_order(site, rng, 104200 + i) for i in range(300)]
    for o in orders:
        assert 1 <= len(o["lines"]) <= 2 and o["dock"] in W.docks
        assert o["value"] == round(sum(x["qty"] * x["unit_value"] for x in o["lines"]), 2)
        assert o["ship_by"] > dt.datetime.now(dt.timezone.utc)
        for x in o["lines"]:
            assert site["catalog"][x["slot"]]["sku"] == x["sku"]
    heavy = sum(any(x["slot"].startswith("F") for x in o["lines"]) for o in orders)
    assert heavy < 30  # heavy picks are rare: only R4 can do them
    assert any(o["priority"] == "expedite" for o in orders)


def test_impact_prices_incidents_from_the_cost_model():
    site = _site()
    cm = site["cost_model"]
    order = {"id": "SO-1", "customer_id": "C-1001", "value": 812.5, "priority": "standard", "status": "picking",
             "carrier": "X", "dock": "DK1"}
    assert enterprise.impact(site, "wrong_item", order)["estimated_cost_usd"] == round(cm["mispick_usd"] + 812.5, 2)
    assert enterprise.impact(site, "zone_breach", None)["estimated_cost_usd"] == cm["safety_incident_usd"]
    assert enterprise.impact(site, "collision", order)["order"]["value"] == 812.5


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
