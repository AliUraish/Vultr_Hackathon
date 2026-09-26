"""Fleet policy and the fix DSL.

A policy is an ordered list of rules. Each rule is one fix from a closed set:

    speed_cap(<zone>, <m/s>)            max speed inside a zone
    min_clearance(<m>)                  distance kept to sensed obstacles
    reroute_avoid(<zone> | c<x>_<y>)    path planner avoids these cells when it can
    reorder_steps(<job kind>, <strategy>)  pick order for jobs of a kind
    require_scan_confirm(<item class> | *) scan the bin before picking
    respect_closures(<zone> | *)        re-plan when a zone closes; never drive into a closed zone

Rules from the diagnosis agent are parsed and validated here before anything
runs them; nothing in a rule is ever evaluated as code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .hashing import sha256
from .world import DEFAULT_CLEARANCE, ITEM_CLASSES, MAX_SPEED, TICK_HZ, W

FIX_TYPES: tuple[str, ...] = (
    "speed_cap", "min_clearance", "reroute_avoid", "reorder_steps", "require_scan_confirm",
    "respect_closures",
)
JOB_KINDS: tuple[str, ...] = ("single", "multi", "*")
STRATEGIES: tuple[str, ...] = ("nearest_first", "farthest_first", "as_given")

_RULE_RE = re.compile(r"^\s*([a-z_]+)\s*\((.*)\)\s*$")
_CELL_RE = re.compile(r"^c(\d+)_(\d+)$")

MIN_SPEED_MPS = Decimal("0.1")
MAX_SPEED_MPS = Decimal(MAX_SPEED * TICK_HZ) / 1000
MIN_CLEARANCE_M = Decimal("0.05")
MAX_CLEARANCE_M = Decimal("1.0")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    name: str
    args: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.name}({', '.join(self.args)})"


def _number(text: str, lo: Decimal, hi: Decimal, what: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise PolicyError(f"{what} must be a number, got {text!r}") from None
    if not (lo <= value <= hi):
        raise PolicyError(f"{what} must be between {lo} and {hi}, got {text}")
    return value


def _fmt(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." in text else f"{text}.0"


def _target_cells(arg: str) -> list[list[int]]:
    """Cells named by a zone or a single cell reference like c12_3."""
    if arg in W.zones:
        return W.zones[arg]
    m = _CELL_RE.match(arg)
    if m:
        c = (int(m.group(1)), int(m.group(2)))
        if not W.passable(c):
            raise PolicyError(f"cell {arg} is not a floor cell")
        return [[c[0], c[1]]]
    raise PolicyError(f"unknown zone or cell {arg!r}; zones: {', '.join(sorted(W.zones))}")


def parse_rule(text: str) -> Rule:
    """Parse and validate one rule; returns it in normalized form."""
    m = _RULE_RE.match(text or "")
    if not m:
        raise PolicyError(f"not a rule: {text!r}")
    name = m.group(1)
    raw = m.group(2).strip()
    args = tuple(a.strip().strip("'\"") for a in raw.split(",")) if raw else ()
    if name not in FIX_TYPES:
        raise PolicyError(f"unknown fix {name!r}; allowed: {', '.join(FIX_TYPES)}")

    def need(n: int) -> None:
        if len(args) != n:
            raise PolicyError(f"{name} takes {n} argument(s), got {len(args)}")

    if name == "speed_cap":
        need(2)
        if args[0] not in W.zones:
            raise PolicyError(f"unknown zone {args[0]!r}; zones: {', '.join(sorted(W.zones))}")
        v = _number(args[1], MIN_SPEED_MPS, MAX_SPEED_MPS, "speed (m/s)")
        return Rule(name, (args[0], _fmt(v)))
    if name == "min_clearance":
        need(1)
        v = _number(args[0], MIN_CLEARANCE_M, MAX_CLEARANCE_M, "clearance (m)")
        return Rule(name, (_fmt(v),))
    if name == "reroute_avoid":
        need(1)
        _target_cells(args[0])
        return Rule(name, (args[0],))
    if name == "reorder_steps":
        need(2)
        if args[0] not in JOB_KINDS:
            raise PolicyError(f"job kind must be one of {', '.join(JOB_KINDS)}")
        if args[1] not in STRATEGIES:
            raise PolicyError(f"strategy must be one of {', '.join(STRATEGIES)}")
        return Rule(name, args)
    if name == "respect_closures":
        need(1)
        if args[0] != "*" and args[0] not in W.zones:
            raise PolicyError(f"zone must be * or one of {', '.join(sorted(W.zones))}")
        return Rule(name, args)
    need(1)  # require_scan_confirm
    if args[0] != "*" and args[0] not in ITEM_CLASSES:
        raise PolicyError(f"item class must be * or one of {', '.join(ITEM_CLASSES)}")
    return Rule(name, args)


def normalize(rules: list[str]) -> list[str]:
    return [str(parse_rule(r)) for r in rules]


def compile_rules(rules: list[str]) -> dict:
    """Integer-only form the engine reads every tick."""
    caps: dict[str, int] = {}
    clearance = DEFAULT_CLEARANCE
    avoid: set[tuple[int, int]] = set()
    scan: set[str] = set()
    reorder: dict[str, str] = {}
    closures: set[str] = set()
    for text in rules:
        rule = parse_rule(text)
        a = rule.args
        if rule.name == "speed_cap":
            mm_per_tick = int(Decimal(a[1]) * 1000 / TICK_HZ)
            caps[a[0]] = min(caps.get(a[0], MAX_SPEED), mm_per_tick)
        elif rule.name == "min_clearance":
            clearance = max(clearance, int(Decimal(a[0]) * 1000))
        elif rule.name == "reroute_avoid":
            avoid |= {(c[0], c[1]) for c in _target_cells(a[0])}
        elif rule.name == "reorder_steps":
            reorder[a[0]] = a[1]
        elif rule.name == "respect_closures":
            closures.add(a[0])
        else:
            scan.add(a[0])
    out = {
        "caps": dict(sorted(caps.items())),
        "clearance": clearance,
        "avoid": [list(c) for c in sorted(avoid)],
        "scan": sorted(scan),
        "reorder": dict(sorted(reorder.items())),
    }
    if closures:  # only present when used, so policies without it hash exactly as before
        out["closures"] = sorted(closures)
    return out


def make_policy(rules: list[str], version: int) -> dict:
    """A policy as it lives inside sim state: normalized rules, hash, compiled form."""
    norm = normalize(rules)
    return {"version": version, "rules": norm, "hash": sha256(norm), "c": compile_rules(norm)}
