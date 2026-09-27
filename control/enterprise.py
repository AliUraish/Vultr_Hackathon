"""The mine this deployment runs, and the load tickets that drive the haul fleet.

Each database gets its own mine, generated once: a fictional mining company and pit
(picked at random from a set of commodities and regions), the material at every dig
face (consistent with the face's class: ore, low-grade, waste or sand), the excavator
at each face, the destination at each dump point, offtake buyers, the truck asset
register, the crew and a cost model. The configured model writes it against a strict
JSON schema; the answer is validated and anything missing is filled in by a seeded
generator, which also produces the whole profile when there is no key or the call fails.

Load tickets are raised continuously by the dig faces (each excavator wants a truck
under it and one on the way), become haul loads for the trucks, and are closed by the
fleet's telemetry. An incident is priced from the load it hit and the cost model.
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

from replay_core.world import DESTINATION, ITEM_CLASSES, ROBOT_SPECS, SPARE_SPECS, W

from .config import Settings
from .db import log_event
from .usage import USAGE

log = logging.getLogger("replay.enterprise")

COMMODITIES = {
    "copper": ("copper sulphide ore", "% Cu", (0.6, 1.8)),
    "iron ore": ("hematite iron ore", "% Fe", (56.0, 64.0)),
    "gold": ("gold-bearing ore", "g/t Au", (0.8, 4.5)),
    "metallurgical coal": ("run-of-mine coking coal", "% ash", (8.0, 14.0)),
    "lithium": ("spodumene pegmatite ore", "% Li2O", (1.0, 1.6)),
    "zinc-lead": ("zinc-lead sulphide ore", "% Zn", (4.0, 9.0)),
}
REGIONS = ("Pilbara, Western Australia", "Bowen Basin, Queensland", "Atacama, Chile", "Elko County, Nevada",
           "Sudbury Basin, Ontario", "South Gobi, Mongolia", "Kalgoorlie, Western Australia", "Arizona Copper Belt",
           "Hunter Valley, New South Wales", "Carajás, Brazil")
TIERS = {"platinum": 4, "gold": 8, "standard": 24}
CLASS_HINT = {
    "ore": "the high-grade ore the plant is paid for (goes to the primary crusher)",
    "lowgrade": "low-grade ore stockpiled for later processing (goes to the ROM stockpile)",
    "waste": "overburden and waste rock with no value (goes to the waste dump)",
    "sand": "sand and gravel sold as construction aggregate (goes to the sand stockpile)",
}
DOCK_KIND = {"DK1": "primary crusher", "DK2": "ROM stockpile", "DK3": "waste dump", "DK4": "sand stockpile"}


def slot_spec() -> list[dict]:
    return [{"slot": s, "cls": W.slots[s]["cls"]} for s in sorted(W.slots)]


# ---------------------------------------------------------------- schema + prompt

def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": props,
            "required": required or list(props)}


SCHEMA = _obj({
    "facility": _obj({
        "company": {"type": "string"}, "name": {"type": "string"}, "code": {"type": "string"},
        "location": {"type": "string"}, "commodity": {"type": "string"}, "description": {"type": "string"},
        "shifts": {"type": "array", "items": {"type": "string"}},
    }),
    "catalog": {"type": "array", "items": _obj({
        "slot": {"type": "string"}, "sku": {"type": "string"}, "name": {"type": "string"},
        "category": {"type": "string"}, "grade": {"type": "string"},
        "unit_value": {"type": "number"}, "unit_weight": {"type": "number"},
    })},
    "excavators": {"type": "array", "items": _obj({
        "slot": {"type": "string"}, "id": {"type": "string"}, "model": {"type": "string"}, "bucket_m3": {"type": "number"},
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
        "id": {"type": "string", "enum": [r["id"] for r in ROBOT_SPECS + SPARE_SPECS]}, "model": {"type": "string"},
        "serial": {"type": "string"}, "firmware": {"type": "string"}, "commissioned": {"type": "string"},
        "payload_t": {"type": "integer"},
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


def prompt(commodity: str, region: str) -> str:
    faces = ", ".join(f"{s['slot']}={s['cls']}" for s in slot_spec())
    docks = ", ".join(f"{d}={DOCK_KIND[d]}" for d in sorted(W.docks))
    return f"""Create the operating profile of a fictional open-pit mine run by autonomous haul trucks.
Commodity: {commodity}. Region: {region}. Invent names; never use real company, mine or equipment brand names.

facility: company name, mine name (e.g. "<Name> Pit"), a short site code (like "PIL-3"), location, commodity,
one-line description, 2-3 shift names with hours.
catalog: exactly one material per dig face, {len(W.slots)} total, matching the face's class: {faces}.
Classes: {'; '.join(f'{k} = {v}' for k, v in CLASS_HINT.items())}.
Each material: a material code (sku) in a consistent format, a specific name, a category, a grade string with units
(e.g. "1.1% Cu" or "waste, 0.1% Cu"), value in USD per tonne (waste is 0) and tonnes per truck load (220-290).
excavators: one per face (slot A-D), an id like EX-01, an invented hydraulic excavator model, bucket size m3 (28-42).
customers: 4 offtake buyers for the saleable products (smelters, steel mills, traders, concrete producers) with
segment and tier (platinum, gold or standard; one platinum at most), plus "Mine operations (internal)" as standard.
carriers: one destination per dump point: {docks}; carrier = the destination's name (e.g. "Primary crusher CR-1"),
service = what it takes, cutoff = its throughput (e.g. "3,200 t/h").
robots: {', '.join(r['id'] for r in ROBOT_SPECS + SPARE_SPECS)} are identical autonomous ultra-class haul trucks
(payload 220-290 t; the last one is the standby spare); invent the model name, serials, firmware and commissioning dates.
associates: 7 people on the crew (shift supervisor, fleet dispatcher, maintenance fitter, tyre fitter, blast
coordinator, grade control geologist, safety officer). cost_model: realistic USD figures for this mine
(downtime per truck-minute, a truck collision, a misrouted load, a recordable safety incident, a late load)."""


# ---------------------------------------------------------------- model call

async def _ask(settings: Settings, text: str, transport: httpx.AsyncBaseTransport | None) -> tuple[dict, str]:
    from .diagnosis import _pick_model   # the same model choice as the investigator
    headers = {"Authorization": f"Bearer {settings.inference_key}"}
    async with httpx.AsyncClient(headers=headers, timeout=180.0, transport=transport) as http:
        model = await _pick_model(http, settings)
        body = {"model": model, "temperature": 0.8, "max_tokens": 4000,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": "Answer with one JSON object with the keys "
                              + ", ".join(SCHEMA["properties"]) + ". Keep every text field short."},
                             {"role": "user", "content": text}]}
        r = await http.post(f"{settings.inference_url}/chat/completions", json=body)
        if r.status_code == 400 and "response_format" in r.text:
            body.pop("response_format")
            r = await http.post(f"{settings.inference_url}/chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        u = data.get("usage") or {}
        USAGE.record("site_profile", int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))
        out = data["choices"][0]["message"]["content"] or ""
        return json.loads(out[out.find("{"):out.rfind("}") + 1]), f"{settings.inference_provider}:{model}"


# ---------------------------------------------------------------- fallback generator

_MATERIAL = {
    "ore": ("{c} (high grade)", "ore"), "lowgrade": ("{c} (low grade)", "low-grade ore"),
    "waste": ("Overburden waste rock", "waste"), "sand": ("Washed construction sand", "aggregate"),
}
_FIRST = ["Maya", "Luis", "Priya", "Jordan", "Aiko", "Tomás", "Nia", "Owen", "Farah", "Diego", "Hana", "Sam", "Rhys", "Kiri"]
_LAST = ["Okafor", "Reyes", "Nakamura", "Patel", "Lindqvist", "Moreau", "Haddad", "Kowalski", "Chen", "Adeyemi", "Walker"]
_ROLES = ("Shift supervisor", "Fleet dispatcher", "Maintenance fitter", "Tyre fitter", "Blast coordinator",
          "Grade control geologist", "Safety officer")


def generator(rng: random.Random, commodity: str, region: str) -> dict:
    """A complete profile without a model (random but plausible)."""
    mat, unit, (glo, ghi) = COMMODITIES.get(commodity, COMMODITIES["copper"])
    prefix = "".join(rng.choice("ABCDEFGHKMNPRSTVW") for _ in range(2))
    value = {"ore": rng.randint(60, 140), "lowgrade": rng.randint(14, 32), "waste": 0, "sand": rng.randint(8, 16)}
    catalog = []
    for s in slot_spec():
        name, cat = _MATERIAL[s["cls"]]
        g = rng.uniform(glo, ghi) * (1 if s["cls"] == "ore" else 0.45 if s["cls"] == "lowgrade" else 0.05)
        catalog.append({"slot": s["slot"], "sku": f"{prefix}-{s['cls'][:2].upper()}{rng.randint(10, 99)}",
                        "name": name.format(c=mat.capitalize()), "category": cat,
                        "grade": f"{g:.2f} {unit}" if s["cls"] != "sand" else "0-5 mm, washed",
                        "unit_value": float(value[s["cls"]]), "unit_weight": float(rng.randint(230, 285))})
    company = f"{rng.choice(['Red Ridge', 'Ironbark', 'Saltbush', 'Copperhead', 'Granite Peak', 'Mulga'])} " \
              f"{rng.choice(['Resources', 'Mining', 'Minerals', 'Metals'])}"
    pit = f"{rng.choice(['Kestrel', 'Blackwood', 'Yarrie', 'Condor', 'Ghost Gum', 'Emu Creek'])} Pit"
    return {
        "facility": {"company": company, "name": pit, "code": f"{region[:3].upper()}-{rng.randint(1, 9)}",
                     "location": region, "commodity": commodity,
                     "description": f"Open-pit {commodity} mine run by an autonomous haul fleet.",
                     "shifts": ["Day 06:00-18:00", "Night 18:00-06:00"]},
        "catalog": catalog,
        "excavators": [{"slot": s["slot"], "id": f"EX-0{i + 1}", "model": rng.choice(["HX-390 Face Shovel", "HX-450 Backhoe"]),
                        "bucket_m3": float(rng.randint(28, 42))} for i, s in enumerate(slot_spec())],
        "customers": [{"name": f"{rng.choice(['Pacific', 'Northern', 'Coastal', 'Summit', 'Harbour'])} "
                               f"{rng.choice(['Smelting', 'Steel', 'Metals Trading', 'Concrete'])} {i}",
                       "segment": rng.choice(["smelter", "steel mill", "trader", "construction"]),
                       "tier": "platinum" if i == 1 else rng.choice(["gold", "standard"])} for i in range(1, 5)]
                     + [{"name": "Mine operations (internal)", "segment": "internal", "tier": "standard"}],
        "carriers": [{"dock": d, "carrier": f"{DOCK_KIND[d].capitalize()} {d.replace('DK', '')}",
                      "service": DOCK_KIND[d], "cutoff": f"{rng.randint(18, 40) * 100:,} t/h"} for d in sorted(W.docks)],
        "robots": [{"id": r["id"], "model": "HX-930 Autonomous Haul Truck", "serial": f"SN{rng.randint(100000, 999999)}",
                    "firmware": f"7.{rng.randint(0, 9)}.{rng.randint(0, 20)}",
                    "commissioned": f"202{rng.randint(3, 6)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    "payload_t": 290} for r in ROBOT_SPECS + SPARE_SPECS],
        "associates": [{"name": f"{rng.choice(_FIRST)} {rng.choice(_LAST)}", "role": role, "shift": "Day"} for role in _ROLES],
        "cost_model": {"downtime_usd_per_min": rng.randint(90, 240), "collision_usd": rng.randint(80_000, 400_000),
                       "mispick_usd": rng.randint(4_000, 25_000), "safety_incident_usd": rng.randint(50_000, 250_000),
                       "late_order_usd": rng.randint(800, 4_000)},
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
    out["facility"] = {k: _txt(f.get(k), 160, ff[k]) for k in ("company", "name", "code", "location", "commodity", "description")}
    out["facility"]["code"] = re.sub(r"[^A-Z0-9-]", "", out["facility"]["code"].upper())[:10] or ff["code"]
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
        catalog.append({"slot": base["slot"], "sku": sku, "name": _txt(c.get("name"), 80, base["name"]),
                        "category": _txt(c.get("category"), 40, base["category"]), "cls": cls,
                        "grade": _txt(c.get("grade"), 40, base["grade"]), "uom": "t",
                        "unit_value": round(_num(c.get("unit_value"), 0, 5_000, base["unit_value"]), 2) if cls != "waste" else 0.0,
                        "unit_weight": round(_num(c.get("unit_weight"), 150, 320, base["unit_weight"]), 0)})
    out["catalog"] = catalog
    exc = {e.get("slot"): e for e in raw.get("excavators") or [] if isinstance(e, dict)}
    out["excavators"] = [{"slot": b["slot"], "id": _txt(exc.get(b["slot"], {}).get("id"), 12, b["id"]),
                          "model": _txt(exc.get(b["slot"], {}).get("model"), 40, b["model"]),
                          "bucket_m3": round(_num(exc.get(b["slot"], {}).get("bucket_m3"), 10, 60, b["bucket_m3"]), 0)}
                         for b in fill["excavators"]]

    custs = [c for c in raw.get("customers") or [] if isinstance(c, dict) and c.get("name")][:8] or fill["customers"]
    if not any("internal" in str(c.get("segment", "")).lower() or "internal" in str(c.get("name", "")).lower() for c in custs):
        custs = custs + [{"name": "Mine operations (internal)", "segment": "internal", "tier": "standard"}]
    out["customers"] = [{"id": f"C-{1001 + i}", "name": _txt(c.get("name"), 60, f"Buyer {i}"),
                         "segment": _txt(c.get("segment"), 60, "trader"),
                         "tier": c.get("tier") if c.get("tier") in TIERS else "standard"} for i, c in enumerate(custs)]
    for c in out["customers"]:
        c["sla_hours"] = TIERS[c["tier"]]

    carriers = {c.get("dock"): c for c in raw.get("carriers") or [] if isinstance(c, dict)}
    out["carriers"] = [{"dock": b["dock"], "carrier": _txt(carriers.get(b["dock"], {}).get("carrier"), 40, b["carrier"]),
                        "service": _txt(carriers.get(b["dock"], {}).get("service"), 30, b["service"]),
                        "cutoff": _txt(carriers.get(b["dock"], {}).get("cutoff"), 16, b["cutoff"])}
                       for b in fill["carriers"]]
    robots = {r.get("id"): r for r in raw.get("robots") or [] if isinstance(r, dict)}
    out["robots"] = [{"id": b["id"], **{k: _txt(robots.get(b["id"], {}).get(k), 40, b[k])
                                        for k in ("model", "serial", "firmware", "commissioned")},
                      "payload_t": int(_num(robots.get(b["id"], {}).get("payload_t"), 150, 400, b["payload_t"]))}
                     for b in fill["robots"]]
    people = [a for a in raw.get("associates") or [] if isinstance(a, dict) and a.get("name")][:9] or fill["associates"]
    out["associates"] = [{"name": _txt(a.get("name"), 40, "Crew member"), "role": _txt(a.get("role"), 40, "Operator"),
                          "shift": _txt(a.get("shift"), 30, "Day")} for a in people]
    cm, fc = raw.get("cost_model") or {}, fill["cost_model"]
    out["cost_model"] = {k: round(_num(cm.get(k), 1, 5_000_000, fc[k]), 2) for k in fc}
    return out


async def generate(settings: Settings, rng: random.Random | None = None,
                   transport: httpx.AsyncBaseTransport | None = None) -> tuple[dict, str, dict]:
    """(profile, source, theme)."""
    rng = rng or random.Random()
    theme = {"commodity": rng.choice(sorted(COMMODITIES)), "region": rng.choice(REGIONS)}
    fill = generator(rng, theme["commodity"], theme["region"])
    if settings.inference_enabled:
        try:
            raw, source = await _ask(settings, prompt(theme["commodity"], theme["region"]), transport)
            return validate(raw, fill), source, theme
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            log.warning("mine profile generation failed, using the generator: %s", exc)
    return validate(fill, fill), "generator", theme


# ---------------------------------------------------------------- persistence

async def ensure_site(pool: asyncpg.Pool, settings: Settings) -> dict:
    """The site row, creating it (and the materials and buyers) on first boot of a database."""
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT * FROM site")
    if row is None:
        profile, source, theme = await generate(settings)
        async with pool.acquire() as c, c.transaction():
            if await c.fetchval("SELECT 1 FROM site") is None:
                fac = profile["facility"]
                stored = {**{k: v for k, v in profile.items() if k not in ("catalog", "customers")},
                          "catalog_grades": [{"slot": x["slot"], "grade": x["grade"]} for x in profile["catalog"]],
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
                                                    "source": source, **theme, "faces": len(profile["catalog"]),
                                                    "buyers": len(profile["customers"])})
    return await load_site(pool)


async def load_site(pool: asyncpg.Pool) -> dict:
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT * FROM site")
        cat = await c.fetch("SELECT * FROM catalog ORDER BY slot")
        custs = await c.fetch("SELECT * FROM customers ORDER BY id")
    profile = row["profile"]
    grades = {x["slot"]: x.get("grade", "") for x in profile.get("catalog_grades", [])}
    site = {"code": row["code"], "name": row["name"], "source": row["source"], **profile,
            "catalog": {r["slot"]: {**dict(r), "unit_value": float(r["unit_value"]), "unit_weight": float(r["unit_weight"]),
                                    "grade": grades.get(r["slot"], "")} for r in cat},
            "customers": [dict(r) for r in custs]}
    return site


# ---------------------------------------------------------------- load tickets

def buyer_for(site: dict, cls: str, rng: random.Random) -> dict:
    """Who a load is for: an offtake buyer for saleable material, the mine itself for waste."""
    internal = [c for c in site["customers"] if c["segment"] == "internal" or "internal" in c["name"].lower()]
    buyers = [c for c in site["customers"] if c not in internal]
    if cls == "waste" or not buyers:
        return (internal or site["customers"])[0]
    return rng.choices(buyers, weights=[{"platinum": 1, "gold": 3, "standard": 6}.get(c["tier"], 3) for c in buyers])[0]


def make_order(site: dict, rng: random.Random, seq: int, slot: str) -> dict:
    """One load ticket: a truck load from a dig face to the destination its material goes to."""
    item = site["catalog"][slot]
    cls = W.slots[slot]["cls"]
    dock = DESTINATION[cls]
    cust = buyer_for(site, cls, rng)
    dest = next((c for c in site["carriers"] if c["dock"] == dock), {"carrier": dock})
    tonnes = int(item["unit_weight"]) - rng.randint(0, 12)
    now = dt.datetime.now(dt.timezone.utc)
    return {"id": f"LD-{seq}", "customer_id": cust["id"], "customer": cust["name"], "tier": cust["tier"],
            "dock": dock, "carrier": dest["carrier"],
            "lines": [{"slot": slot, "sku": item["sku"], "name": item["name"], "qty": tonnes, "unit_value": item["unit_value"]}],
            "value": round(tonnes * item["unit_value"], 2),
            "priority": "expedite" if cls == "ore" and rng.random() < 0.25 else "standard",
            "ship_by": now + dt.timedelta(minutes=10)}


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
        ai = await c.fetchrow("SELECT count(*) FILTER (WHERE type = 'ai.dispatch') AS dispatch, "
                              "count(*) FILTER (WHERE type = 'ai.traffic') AS traffic, "
                              "count(*) FILTER (WHERE type = 'service.step' AND payload->>'by' = 'copilot') AS service "
                              "FROM events WHERE type IN ('ai.dispatch', 'ai.traffic', 'service.step') "
                              "AND ts > now() - interval '1 hour'")
    frame = rt.frame or {"robots": []}
    trucks = [r for r in frame["robots"] if r.get("st") != "standby"]
    busy = sum(1 for r in trucks if r.get("st") not in ("idle", "fault", None))
    return {"orders": o["orders"], "shipped": o["shipped"], "open": o["open"], "problems": o["problems"],
            "value_shipped": float(o["value_shipped"]), "units_shipped": int(o["units_shipped"]),
            "orders_per_hour": o["shipped_15m"] * 4, "tonnes_per_hour": 0,
            "on_time_pct": round(100 * j["on_time"] / j["done"], 1) if j["done"] else None,
            "incidents_open": f["open"], "incidents_fixed": f["fixed"],
            "mttr_s": round(f["mttr_s"]) if f["mttr_s"] else None,
            "last_safety_incident": f["last_safety"], "fleet_busy": busy, "fleet_size": len(trucks),
            "ai_decisions_hour": int(ai["dispatch"] + ai["traffic"] + ai["service"]),
            "docks": {r["dock"]: {"orders": r["orders"], "units": int(r["units"])} for r in docks}}


# ---------------------------------------------------------------- incident impact

async def failure_order(c: asyncpg.Connection, f: Any) -> asyncpg.Record | None:
    """The load ticket the failing truck was working on, if any."""
    job = (f["detail"] or {}).get("job")
    if job is None:
        job = await c.fetchval("SELECT id FROM jobs WHERE robot_id = $1 AND run_id = $2 AND assigned_tick <= $3 "
                               "ORDER BY assigned_tick DESC LIMIT 1", f["robot_id"], f["run_id"], f["tick"])
    if job is None:
        return None
    return await c.fetchrow("SELECT o.*, c.name AS customer, c.tier, c.sla_hours FROM orders o "
                            "JOIN customers c ON c.id = o.customer_id WHERE o.job_id = $1", job)


async def business_context(c: asyncpg.Connection, site: dict | None, f: Any) -> dict | None:
    """What the incident means for the mine, for the investigator to weigh against throughput cost."""
    if not site:
        return None
    order = await failure_order(c, f)
    shipped = await c.fetchval("SELECT count(*) FROM orders WHERE status = 'shipped' "
                               "AND shipped_at > now() - interval '30 minutes'")
    imp = impact(site, f["type"], order)
    fac = site["facility"]
    return {"site": f"{fac['company']} · {site['name']} ({site['code']}), {fac.get('commodity', '')}",
            "load": ({"id": order["id"], "for": order["customer"], "value_usd": float(order["value"]),
                      "priority": order["priority"], "destination": order["carrier"],
                      "material": [f"{x['qty']} t {x['name']} ({x['sku']})" for x in order["lines"]]} if order else None),
            "estimated_incident_cost_usd": imp["estimated_cost_usd"], "cost_basis": imp["basis"],
            "loads_dumped_per_hour": shipped * 2,
            "downtime_usd_per_truck_minute": site["cost_model"]["downtime_usd_per_min"]}


def impact(site: dict, failure_type: str, order: dict | None) -> dict:
    """Estimated cost of one incident from the mine's cost model (labelled as an estimate in the UI)."""
    cm = site["cost_model"]
    base = {"collision": cm["collision_usd"] + 20 * cm["downtime_usd_per_min"],
            "wrong_item": cm["mispick_usd"] + (float(order["value"]) if order else 0.0),
            "zone_breach": cm["safety_incident_usd"],
            "task_overdue": cm["late_order_usd"] + 5 * cm["downtime_usd_per_min"],
            "stall": 5 * cm["downtime_usd_per_min"]}.get(failure_type, cm["downtime_usd_per_min"])
    why = {"collision": "truck repair + 20 min of downtime", "wrong_item": "misrouted load: rehandling + the load's value",
           "zone_breach": "recordable safety incident (blast exclusion zone)", "task_overdue": "late load + 5 min downtime",
           "stall": "5 min of downtime"}.get(failure_type, "downtime")
    return {"estimated_cost_usd": round(base, 2), "basis": why,
            "order": ({k: order[k] for k in ("id", "customer_id", "value", "priority", "status", "carrier", "dock")}
                      | {"value": float(order["value"])}) if order else None}


__all__ = ["CLASS_HINT", "ITEM_CLASSES", "SCHEMA", "business_context", "buyer_for", "ensure_site", "failure_order",
           "generate", "generator", "impact", "kpis", "load_site", "make_order", "prompt", "validate"]
