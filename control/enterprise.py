"""The enterprise this deployment runs, and the orders that drive the fleet.

Each database gets its own site, generated once: a fictional company and facility (picked at
random from a set of industries and cities), the SKU in every rack slot (consistent with the
slot's handling class), customers with service tiers, the carrier at each dock door, the robot
asset register, the shift roster and a cost model. The configured model writes it against a
strict JSON schema; the answer is validated and anything missing is filled in by a seeded
generator, which also produces the whole profile when there is no key or the call fails.

Orders are generated continuously from that profile (weighted customers, 1-2 lines, quantities by
handling class), become pick jobs for the robots, and are closed by the fleet's telemetry. An
incident is priced from the order it hit and the cost model.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import random
import re
from typing import Any

import asyncpg
import httpx

from replay_core.world import ITEM_CLASSES, ROBOT_SPECS, W

from .config import Settings
from .db import log_event

log = logging.getLogger("replay.enterprise")

INDUSTRIES = (
    "medical and surgical supplies distribution", "consumer electronics e-commerce fulfilment",
    "automotive aftermarket parts distribution", "specialty grocery and beverage distribution",
    "industrial MRO (maintenance, repair, operations) supplies", "pharmacy and health retail replenishment",
    "home improvement and hardware retail", "outdoor and sporting goods e-commerce",
    "laboratory and life-science consumables", "beauty and personal care fulfilment",
)
CITIES = ("Fremont, CA", "Reno, NV", "Tracy, CA", "Ontario, CA", "Phoenix, AZ", "Salt Lake City, UT",
          "Dallas, TX", "Columbus, OH", "Memphis, TN", "Allentown, PA", "Indianapolis, IN", "Savannah, GA")
TIERS = {"platinum": 4, "gold": 8, "standard": 24}
CLASS_HINT = {
    "boxed": "sealed cartons and boxed goods (sturdy, mid weight)",
    "loose_small": "small loose items that look alike (easy to mix up)",
    "fragile": "fragile items (glass, electronics, instruments)",
    "heavy": "heavy or bulky items (only the heavy-lift robot can carry them)",
}


def slot_spec() -> list[dict]:
    return [{"slot": s, "cls": W.slots[s]["cls"]} for s in sorted(W.slots)]


# ---------------------------------------------------------------- schema + prompt

def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": props,
            "required": required or list(props)}


SCHEMA = _obj({
    "facility": _obj({
        "company": {"type": "string"}, "name": {"type": "string"}, "code": {"type": "string"},
        "city": {"type": "string"}, "sq_ft": {"type": "integer"}, "description": {"type": "string"},
        "shifts": {"type": "array", "items": {"type": "string"}},
    }),
    "catalog": {"type": "array", "items": _obj({
        "slot": {"type": "string"}, "sku": {"type": "string"}, "name": {"type": "string"},
        "category": {"type": "string"}, "uom": {"type": "string"},
        "unit_value": {"type": "number"}, "unit_weight": {"type": "number"},
    })},
    "customers": {"type": "array", "items": _obj({
        "name": {"type": "string"}, "segment": {"type": "string"},
        "tier": {"type": "string", "enum": list(TIERS)},
    })},
    "carriers": {"type": "array", "items": _obj({
        "dock": {"type": "string", "enum": sorted(W.docks)}, "carrier": {"type": "string"},
        "service": {"type": "string"}, "cutoff": {"type": "string"},
    })},
    "robots": {"type": "array", "items": _obj({
        "id": {"type": "string", "enum": [r["id"] for r in ROBOT_SPECS]}, "model": {"type": "string"},
        "serial": {"type": "string"}, "firmware": {"type": "string"}, "commissioned": {"type": "string"},
        "payload_kg": {"type": "integer"},
    })},
    "associates": {"type": "array", "items": _obj({
        "name": {"type": "string"}, "role": {"type": "string"}, "shift": {"type": "string"},
    })},
    "cost_model": _obj({
        "downtime_usd_per_min": {"type": "number"}, "collision_usd": {"type": "number"},
        "mispick_usd": {"type": "number"}, "safety_incident_usd": {"type": "number"},
        "late_order_usd": {"type": "number"},
    }),
})


def prompt(industry: str, city: str) -> str:
    slots = ", ".join(f"{s['slot']}={s['cls']}" for s in slot_spec())
    return f"""Create the operating profile of a fictional company's robotic fulfilment centre.
Industry: {industry}. Location: {city}. Invent names; never use real company or brand names.

facility: company name, facility name, a short site code (like "FRE-2"), city, floor area, one-line
description, 2-3 shift names with hours.
catalog: exactly one product per rack slot, {len(W.slots)} total, matching the slot's handling class:
{slots}
Classes: {'; '.join(f'{k} = {v}' for k, v in CLASS_HINT.items())}.
Each product: an SKU code in a consistent house format, a specific realistic product name with size or
pack count, a category, a unit of measure (each, box, case, pack...), unit value in USD and unit weight in kg.
Neighbouring loose_small slots should hold look-alike items (e.g. the same part in two sizes).
customers: 10 business customers of this company with segment and tier (platinum, gold or standard;
few platinum). carriers: one per dock door {', '.join(sorted(W.docks))}, with carrier name, service
level and daily cutoff time. robots: {', '.join(r['id'] for r in ROBOT_SPECS)}; R1-R3 are standard
autonomous mobile robots (payload 60-150 kg), R4 is a heavy-lift model (payload 400-1000 kg); invent a
fleet vendor model name, serial, firmware version and commissioning date for each.
associates: 6 people on the floor team (shift lead, safety officer, inventory control, maintenance tech,
fleet engineer, dock supervisor). cost_model: realistic USD figures for this business."""


# ---------------------------------------------------------------- model call

async def _ask(settings: Settings, text: str, transport: httpx.AsyncBaseTransport | None) -> tuple[dict, str]:
    from .diagnosis import _pick_model   # the same model choice as the investigator
    headers = {"Authorization": f"Bearer {settings.inference_key}"}
    async with httpx.AsyncClient(headers=headers, timeout=180.0, transport=transport) as http:
        model = await _pick_model(http, settings)
        if settings.inference_provider == "openai":
            body: dict[str, Any] = {
                "model": model, "input": text, "max_output_tokens": 16000, "reasoning": {"effort": "low"},
                "text": {"format": {"type": "json_schema", "name": "site_profile", "schema": SCHEMA, "strict": True}}}
            r = await http.post(f"{settings.inference_url}/responses", json=body)
            if r.status_code == 400 and "reasoning" in r.text:
                body.pop("reasoning")
                r = await http.post(f"{settings.inference_url}/responses", json=body)
            r.raise_for_status()
            data = r.json()
            out = "".join(c.get("text", "") for item in data.get("output") or [] if item.get("type") == "message"
                          for c in item.get("content") or [] if c.get("type") == "output_text")
            usage = data.get("usage") or {}
            log.info("site profile from %s: %s in / %s out tokens", model, usage.get("input_tokens"),
                     usage.get("output_tokens"))
        else:
            body = {"model": model, "temperature": 0.9, "max_tokens": 6000,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": "Answer with one JSON object with the keys "
                                  + ", ".join(SCHEMA["properties"]) + "."},
                                 {"role": "user", "content": text}]}
            r = await http.post(f"{settings.inference_url}/chat/completions", json=body)
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]["content"] or ""
        return json.loads(out[out.find("{"):out.rfind("}") + 1]), f"{settings.inference_provider}:{model}"


# ---------------------------------------------------------------- fallback generator

_WORDS = {
    "boxed": (["Carton", "Case", "Kit", "Box", "Pack"], ["Shipping Labels", "Nitrile Gloves", "Filter Cartridges",
              "Cable Ties", "Printer Toner", "Hand Sanitizer", "Batteries AA", "Cleaning Wipes"]),
    "loose_small": (["M4", "M5", "M6", "3/8\"", "1/2\"", "10 mm", "12 mm"], ["Hex Bolt", "Lock Nut", "Hose Clamp",
                    "Fuse 10A", "Fuse 15A", "O-Ring", "Spacer", "Cable Gland"]),
    "fragile": (["Borosilicate", "Tempered", "Precision", "OLED", "Ceramic"], ["Beaker 500 ml", "Display Panel",
                "Sensor Module", "Glass Vial Tray", "Lens Assembly", "Thermometer", "Tablet", "Light Fixture"]),
    "heavy": (["Pallet", "Drum", "Bulk", "Industrial"], ["Motor Assembly", "Hydraulic Pump", "Coolant 55 gal",
              "Steel Plate", "Compressor", "Battery Pack 48V", "Gearbox", "Generator"]),
}
_VALUE = {"boxed": (8, 90), "loose_small": (0.4, 12), "fragile": (25, 480), "heavy": (180, 2600)}
_WEIGHT = {"boxed": (1, 14), "loose_small": (0.01, 0.4), "fragile": (0.3, 6), "heavy": (40, 380)}
_FIRST = ["Maya", "Luis", "Priya", "Jordan", "Aiko", "Tomás", "Nia", "Owen", "Farah", "Diego", "Hana", "Sam"]
_LAST = ["Okafor", "Reyes", "Nakamura", "Patel", "Lindqvist", "Moreau", "Haddad", "Kowalski", "Chen", "Adeyemi"]


def generator(rng: random.Random, industry: str, city: str) -> dict:
    """A complete profile without a model (random but plausible)."""
    prefix = "".join(rng.choice("ABCDEFGHJKLMNPRSTVWXZ") for _ in range(2))
    catalog = []
    for i, s in enumerate(slot_spec()):
        adj, nouns = _WORDS[s["cls"]]
        lo, hi = _VALUE[s["cls"]]
        wlo, whi = _WEIGHT[s["cls"]]
        catalog.append({"slot": s["slot"], "sku": f"{prefix}-{rng.randint(10, 99)}{i:03d}",
                        "name": f"{rng.choice(adj)} {nouns[i % len(nouns)]}", "category": s["cls"].replace("_", " "),
                        "uom": rng.choice(["each", "box", "case", "pack"]),
                        "unit_value": round(rng.uniform(lo, hi), 2), "unit_weight": round(rng.uniform(wlo, whi), 2)})
    company = f"{rng.choice(['Northgate', 'Bluestem', 'Harbor', 'Keystone', 'Summit', 'Ironwood'])} " \
              f"{rng.choice(['Supply', 'Distribution', 'Logistics', 'Commerce'])}"
    return {
        "facility": {"company": company, "name": f"{city.split(',')[0]} Fulfilment Center",
                     "code": f"{city[:3].upper()}-{rng.randint(1, 9)}", "city": city, "sq_ft": rng.randint(180, 900) * 1000,
                     "description": f"Robotic fulfilment for {industry}.", "shifts": ["Day 06:00-14:30", "Swing 14:30-23:00"]},
        "catalog": catalog,
        "customers": [{"name": f"{rng.choice(['Apex', 'Cedar', 'Lumen', 'Vista', 'Orion', 'Pioneer'])} "
                               f"{rng.choice(['Health', 'Retail', 'Labs', 'Industrial', 'Market', 'Systems'])} {i}",
                       "segment": rng.choice(["retail", "healthcare", "industrial", "e-commerce"]),
                       "tier": rng.choice(["platinum", "gold", "gold", "standard", "standard", "standard"])}
                      for i in range(1, 11)],
        "carriers": [{"dock": d, "carrier": rng.choice(["Coastline Freight", "Redline Parcel", "Meridian LTL"]),
                      "service": rng.choice(["Ground", "Next day", "LTL"]), "cutoff": f"{rng.randint(14, 18)}:30"}
                     for d in sorted(W.docks)],
        "robots": [{"id": r["id"], "model": "Atlas HL-800" if "heavy" in r["caps"] else "Swift AMR-120",
                    "serial": f"SN{rng.randint(100000, 999999)}", "firmware": f"4.{rng.randint(0, 9)}.{rng.randint(0, 20)}",
                    "commissioned": f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    "payload_kg": 800 if "heavy" in r["caps"] else 120} for r in ROBOT_SPECS],
        "associates": [{"name": f"{rng.choice(_FIRST)} {rng.choice(_LAST)}", "role": role, "shift": "Day"}
                       for role in ("Shift lead", "Safety officer", "Inventory control", "Maintenance tech",
                                    "Fleet engineer", "Dock supervisor")],
        "cost_model": {"downtime_usd_per_min": rng.randint(40, 160), "collision_usd": rng.randint(2500, 12000),
                       "mispick_usd": rng.randint(45, 180), "safety_incident_usd": rng.randint(8000, 40000),
                       "late_order_usd": rng.randint(25, 150)},
    }


# ---------------------------------------------------------------- validation

def _num(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, x)) if x == x else default


def _txt(v: Any, n: int, default: str) -> str:
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    return s[:n] if s else default


def validate(raw: dict, fill: dict) -> dict:
    """Keep what the model got right; take the rest from `fill` (a generator profile)."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    f, ff = raw.get("facility") or {}, fill["facility"]
    out["facility"] = {k: _txt(f.get(k), 160, ff[k]) for k in ("company", "name", "code", "city", "description")}
    out["facility"]["code"] = re.sub(r"[^A-Z0-9-]", "", out["facility"]["code"].upper())[:10] or ff["code"]
    out["facility"]["sq_ft"] = int(_num(f.get("sq_ft"), 50_000, 3_000_000, ff["sq_ft"]))
    shifts = [_txt(s, 60, "") for s in (f.get("shifts") or [])][:4]
    out["facility"]["shifts"] = [s for s in shifts if s] or ff["shifts"]

    by_slot = {c.get("slot"): c for c in raw.get("catalog") or [] if isinstance(c, dict)}
    seen: set[str] = set()
    catalog = []
    for base in fill["catalog"]:
        c = by_slot.get(base["slot"], {})
        sku = _txt(c.get("sku"), 24, base["sku"]).upper()
        if sku in seen:
            sku = base["sku"]
        seen.add(sku)
        cls = W.slots[base["slot"]]["cls"]
        lo, hi = _VALUE[cls]
        catalog.append({"slot": base["slot"], "sku": sku, "name": _txt(c.get("name"), 80, base["name"]),
                        "category": _txt(c.get("category"), 40, base["category"]), "cls": cls,
                        "uom": _txt(c.get("uom"), 12, base["uom"]).lower(),
                        "unit_value": round(_num(c.get("unit_value"), 0.05, 50_000, base["unit_value"]), 2),
                        "unit_weight": round(_num(c.get("unit_weight"), 0.001, 2_000, base["unit_weight"]), 2)})
    out["catalog"] = catalog

    custs = [c for c in raw.get("customers") or [] if isinstance(c, dict) and c.get("name")][:12] or fill["customers"]
    out["customers"] = [{"id": f"C-{1001 + i}", "name": _txt(c.get("name"), 60, f"Customer {i}"),
                         "segment": _txt(c.get("segment"), 60, "retail"),
                         "tier": c.get("tier") if c.get("tier") in TIERS else "standard"} for i, c in enumerate(custs)]
    for c in out["customers"]:
        c["sla_hours"] = TIERS[c["tier"]]

    carriers = {c.get("dock"): c for c in raw.get("carriers") or [] if isinstance(c, dict)}
    out["carriers"] = [{"dock": b["dock"], "carrier": _txt(carriers.get(b["dock"], {}).get("carrier"), 40, b["carrier"]),
                        "service": _txt(carriers.get(b["dock"], {}).get("service"), 30, b["service"]),
                        "cutoff": _txt(carriers.get(b["dock"], {}).get("cutoff"), 8, b["cutoff"])}
                       for b in fill["carriers"]]
    robots = {r.get("id"): r for r in raw.get("robots") or [] if isinstance(r, dict)}
    out["robots"] = [{"id": b["id"], **{k: _txt(robots.get(b["id"], {}).get(k), 40, b[k])
                                        for k in ("model", "serial", "firmware", "commissioned")},
                      "payload_kg": int(_num(robots.get(b["id"], {}).get("payload_kg"), 20, 2000, b["payload_kg"]))}
                     for b in fill["robots"]]
    people = [a for a in raw.get("associates") or [] if isinstance(a, dict) and a.get("name")][:8] or fill["associates"]
    out["associates"] = [{"name": _txt(a.get("name"), 40, "Associate"), "role": _txt(a.get("role"), 40, "Associate"),
                          "shift": _txt(a.get("shift"), 30, "Day")} for a in people]
    cm, fc = raw.get("cost_model") or {}, fill["cost_model"]
    out["cost_model"] = {k: round(_num(cm.get(k), 1, 1_000_000, fc[k]), 2) for k in fc}
    return out


async def generate(settings: Settings, rng: random.Random | None = None,
                   transport: httpx.AsyncBaseTransport | None = None) -> tuple[dict, str, dict]:
    """(profile, source, theme)."""
    rng = rng or random.Random()
    theme = {"industry": rng.choice(INDUSTRIES), "city": rng.choice(CITIES)}
    fill = generator(rng, theme["industry"], theme["city"])
    if settings.inference_enabled:
        try:
            raw, source = await _ask(settings, prompt(theme["industry"], theme["city"]), transport)
            return validate(raw, fill), source, theme
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            log.warning("site profile generation failed, using the generator: %s", exc)
    return validate(fill, fill), "generator", theme


# ---------------------------------------------------------------- persistence

async def ensure_site(pool: asyncpg.Pool, settings: Settings) -> dict:
    """The site row, creating it (and the catalog and customers) on first boot of a database."""
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT * FROM site")
    if row is None:
        profile, source, theme = await generate(settings)
        async with pool.acquire() as c, c.transaction():
            if await c.fetchval("SELECT 1 FROM site") is None:
                fac = profile["facility"]
                stored = {**{k: v for k, v in profile.items() if k not in ("catalog", "customers")},
                          "theme": theme}
                await c.execute("INSERT INTO site (code, name, profile, source) VALUES ($1, $2, $3, $4)",
                                fac["code"], fac["name"], stored, source)
                await c.executemany(
                    "INSERT INTO catalog (slot, sku, name, category, cls, uom, unit_value, unit_weight) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    [(x["slot"], x["sku"], x["name"], x["category"], x["cls"], x["uom"], x["unit_value"],
                      x["unit_weight"]) for x in profile["catalog"]])
                await c.executemany("INSERT INTO customers (id, name, segment, tier, sla_hours) VALUES ($1, $2, $3, $4, $5)",
                                    [(x["id"], x["name"], x["segment"], x["tier"], x["sla_hours"])
                                     for x in profile["customers"]])
                await log_event(c, "site.created", {"code": fac["code"], "name": fac["name"], "company": fac["company"],
                                                    "source": source, **theme, "skus": len(profile["catalog"]),
                                                    "customers": len(profile["customers"])})
    return await load_site(pool)


def gtin13(sku: str) -> str:
    """A stable GTIN-13 barcode number for an SKU (company prefix 0845211, check digit per GS1)."""
    import hashlib
    body = "0845211" + str(int(hashlib.sha256(sku.encode()).hexdigest(), 16) % 100000).zfill(5)
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


async def load_site(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT * FROM site")
        cat = await c.fetch("SELECT * FROM catalog ORDER BY slot")
        custs = await c.fetch("SELECT * FROM customers ORDER BY id")
    return {"code": row["code"], "name": row["name"], "source": row["source"], **row["profile"],
            "catalog": {r["slot"]: {**dict(r), "unit_value": float(r["unit_value"]), "unit_weight": float(r["unit_weight"]),
                                    "barcode": gtin13(r["sku"])} for r in cat},
            "customers": [dict(r) for r in custs]}


# ---------------------------------------------------------------- orders

_QTY = {"boxed": (1, 6), "loose_small": (2, 24), "fragile": (1, 3), "heavy": (1, 1)}
_WEIGHT_BY_TIER = {"platinum": 1, "gold": 3, "standard": 6}


def make_order(site: dict, rng: random.Random, seq: int) -> dict:
    """One order: a weighted customer, 1-2 lines (heavy lines are rare), the carrier at a dock."""
    custs = site["customers"]
    cust = rng.choices(custs, weights=[_WEIGHT_BY_TIER.get(c["tier"], 3) for c in custs])[0]
    pool = [s for s in sorted(site["catalog"]) if not s.startswith("F") or rng.random() < 0.08]
    slots = rng.sample(pool, rng.choice([1, 1, 1, 2]))
    lines = []
    for s in slots:
        item = site["catalog"][s]
        lo, hi = _QTY[item["cls"]]
        qty = rng.randint(lo, hi)
        lines.append({"slot": s, "sku": item["sku"], "name": item["name"], "qty": qty,
                      "unit_value": item["unit_value"]})
    carrier = rng.choice(site["carriers"])
    now = dt.datetime.now(dt.timezone.utc)
    return {"id": f"SO-{seq}", "customer_id": cust["id"], "customer": cust["name"], "tier": cust["tier"],
            "dock": carrier["dock"], "carrier": carrier["carrier"], "lines": lines,
            "value": round(sum(x["qty"] * x["unit_value"] for x in lines), 2),
            "priority": "expedite" if cust["tier"] == "platinum" or rng.random() < 0.1 else "standard",
            "ship_by": now + dt.timedelta(hours=cust["sla_hours"])}


# ---------------------------------------------------------------- KPIs

async def kpis(rt: Any) -> dict:
    async with rt.pool.acquire() as c:
        o = await c.fetchrow(
            "SELECT count(*) AS orders, count(*) FILTER (WHERE status = 'shipped') AS shipped, "
            "count(*) FILTER (WHERE status IN ('short_shipped', 'exception')) AS problems, "
            "count(*) FILTER (WHERE status IN ('released', 'picking')) AS open, "
            "COALESCE(sum(value) FILTER (WHERE status = 'shipped'), 0) AS value_shipped, "
            "COALESCE(sum((SELECT sum((l->>'qty')::int) FROM jsonb_array_elements(lines) l)) "
            "  FILTER (WHERE status = 'shipped'), 0) AS units_shipped, "
            "count(*) FILTER (WHERE status = 'shipped' AND shipped_at > now() - interval '15 minutes') AS shipped_15m "
            "FROM orders")
        docks = await c.fetch("SELECT dock, count(*) AS orders, "
                              "COALESCE(sum((SELECT sum((l->>'qty')::int) FROM jsonb_array_elements(lines) l)), 0) AS units "
                              "FROM orders WHERE status = 'shipped' GROUP BY dock")
        j = await c.fetchrow("SELECT count(*) FILTER (WHERE done_tick <= deadline_tick) AS on_time, count(*) AS done "
                             "FROM jobs WHERE status IN ('done', 'wrong_item') AND order_id IS NOT NULL")
        f = await c.fetchrow(
            "SELECT count(*) FILTER (WHERE status NOT IN ('fixed', 'dismissed', 'lost')) AS open, "
            "count(*) FILTER (WHERE status = 'fixed') AS fixed, "
            "max(created_at) FILTER (WHERE type IN ('collision', 'zone_breach', 'wrong_item')) AS last_safety, "
            "avg(extract(epoch FROM updated_at - created_at)) FILTER (WHERE status = 'fixed') AS mttr_s FROM failures")
    frame = rt.frame or {"robots": []}
    busy = sum(1 for r in frame["robots"] if r.get("st") not in ("idle", None))
    return {"orders": o["orders"], "shipped": o["shipped"], "open": o["open"], "problems": o["problems"],
            "value_shipped": float(o["value_shipped"]), "units_shipped": int(o["units_shipped"]),
            "orders_per_hour": o["shipped_15m"] * 4,
            "on_time_pct": round(100 * j["on_time"] / j["done"], 1) if j["done"] else None,
            "incidents_open": f["open"], "incidents_fixed": f["fixed"],
            "mttr_s": round(f["mttr_s"]) if f["mttr_s"] else None,
            "last_safety_incident": f["last_safety"], "fleet_busy": busy, "fleet_size": len(frame["robots"]),
            "docks": {r["dock"]: {"orders": r["orders"], "units": int(r["units"])} for r in docks}}


# ---------------------------------------------------------------- incident impact

async def failure_order(c: asyncpg.Connection, f: Any) -> asyncpg.Record | None:
    """The customer order the failing robot was working on, if any."""
    job = (f["detail"] or {}).get("job")
    if job is None:
        job = await c.fetchval("SELECT id FROM jobs WHERE robot_id = $1 AND run_id = $2 AND assigned_tick <= $3 "
                               "ORDER BY assigned_tick DESC LIMIT 1", f["robot_id"], f["run_id"], f["tick"])
    if job is None:
        return None
    return await c.fetchrow("SELECT o.*, c.name AS customer, c.tier, c.sla_hours FROM orders o "
                            "JOIN customers c ON c.id = o.customer_id WHERE o.job_id = $1", job)


async def business_context(c: asyncpg.Connection, site: dict | None, f: Any) -> dict | None:
    """What the incident means for the business, for the investigator to weigh against throughput cost."""
    if not site:
        return None
    order = await failure_order(c, f)
    shipped = await c.fetchval("SELECT count(*) FROM orders WHERE status = 'shipped' "
                               "AND shipped_at > now() - interval '30 minutes'")
    imp = impact(site, f["type"], order)
    return {"site": f"{site['facility']['company']} · {site['name']} ({site['code']})",
            "order": ({"id": order["id"], "customer": order["customer"], "tier": order["tier"],
                       "value_usd": float(order["value"]), "priority": order["priority"],
                       "lines": [f"{x['qty']} × {x['name']} ({x['sku']})" for x in order["lines"]]} if order else None),
            "estimated_incident_cost_usd": imp["estimated_cost_usd"], "cost_basis": imp["basis"],
            "orders_shipped_per_hour": shipped * 2,
            "downtime_usd_per_min": site["cost_model"]["downtime_usd_per_min"]}


def impact(site: dict, failure_type: str, order: dict | None) -> dict:
    """Estimated cost of one incident from the site's cost model (labelled as an estimate in the UI)."""
    cm = site["cost_model"]
    base = {"collision": cm["collision_usd"] + 5 * cm["downtime_usd_per_min"],
            "wrong_item": cm["mispick_usd"] + (float(order["value"]) if order else 0.0),
            "zone_breach": cm["safety_incident_usd"],
            "task_overdue": cm["late_order_usd"] + 2 * cm["downtime_usd_per_min"],
            "stall": 3 * cm["downtime_usd_per_min"]}.get(failure_type, cm["downtime_usd_per_min"])
    why = {"collision": "repair + 5 min of downtime", "wrong_item": "mispick handling + reshipping the order",
           "zone_breach": "recordable safety incident", "task_overdue": "late-order penalty + 2 min downtime",
           "stall": "3 min of downtime"}.get(failure_type, "downtime")
    return {"estimated_cost_usd": round(base, 2), "basis": why,
            "order": ({k: order[k] for k in ("id", "customer_id", "value", "priority", "status", "carrier", "dock")}
                      | {"value": float(order["value"])}) if order else None}


__all__ = ["CLASS_HINT", "ITEM_CLASSES", "SCHEMA", "business_context", "ensure_site", "failure_order", "generate",
           "generator", "impact", "kpis", "load_site", "make_order", "prompt", "validate"]
