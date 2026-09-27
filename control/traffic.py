"""The traffic AI: right of way when two haul trucks meet head-on.

The sim reports a `standoff` the moment two trucks wait on each other (a one-lane bench road
or cut, a pocket, a junction). The traffic desk then asks the model who goes first, with what
it needs to judge like a pit controller: which truck is loaded (on a mine the loaded truck has
right of way), where each one could pull aside or which other route it has and how long that
is, who is queued behind each, and faults. The ruling goes to VM B as a recorded `traffic`
input (the yielding truck re-routes or pulls aside), so replays stay exact. The rules check it
(both trucks must still be in that standoff) and rule on their own when the model is off,
slow or failing; the sim's own fallback rule is the last safety net.
"""
from __future__ import annotations

import asyncio
import logging
import time

from replay_core.nav import astar
from replay_core.world import CELL, W

from .db import log_event
from .fleet import cname, zone_name
from .llm import ModelError, obj, structured
from .runtime import Runtime
from .simnode_client import SimNodeError

log = logging.getLogger("replay.traffic")

AI_TIMEOUT_S = 6.0      # the sim's fallback rule fires after 8 s of standoff
REPEAT_S = 15.0         # one ruling per pair of trucks this often

RULING = obj({"first": {"type": "string"}, "yield": {"type": "string"},
              "how": {"type": "string", "enum": ["pull_aside", "reroute"]}, "reason": {"type": "string"}})

I_TRAFFIC = """You are the pit traffic controller for an open-pit mine run by autonomous haul trucks. Two trucks have
met head-on and are both holding: neither can pass. Decide who goes first; the other yields (it re-routes if it has
another route, else pulls aside at the nearest free spot and lets the first one pass). Mine traffic rules: a loaded
truck has right of way over an empty one; a truck that can pull aside or re-route cheaply yields to one that
can't; don't make a truck with others queued behind it back up if the other one can move instead; a faulted truck
under remote control goes first. Answer: first (truck id), yield (the other id), how (pull_aside or reroute), and
reason: one short sentence a shift supervisor would accept, with the facts that decided it."""


def _aside_cells(frame: dict, me: dict, other: dict) -> int | None:
    """How many segments `me` would have to move to get off `other`'s way (a pull-aside spot), or None."""
    blocked = W.blocked | {tuple(p["cell"]) for p in frame.get("pallets", [])}
    avoid = {tuple(c) for c in other.get("res", [])}
    theirs = {tuple(c) for c in other.get("path", [])}
    taken = {tuple(c) for r in frame["robots"] if r["id"] not in (me["id"], other["id"]) for c in r.get("res", [])}
    start = (me["x"] // CELL, me["y"] // CELL)
    seen, frontier, n = {start}, [start], 0
    while frontier and n < 8:
        n += 1
        nxt = []
        for c in frontier:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                q = (c[0] + dx, c[1] + dy)
                if q in seen or q in blocked or q in avoid or q in taken or not W.passable(q):
                    continue
                seen.add(q)
                if q not in theirs:
                    return n
                nxt.append(q)
        frontier = nxt
    return None


def _detour_m(frame: dict, me: dict, other: dict) -> int | None:
    """Extra metres for `me` to reach its goal without going through `other`, or None if there is no other way."""
    if not me.get("goal"):
        return None
    start, goal = (me["x"] // CELL, me["y"] // CELL), tuple(me["goal"])
    hard = {tuple(p["cell"]) for p in frame.get("pallets", [])}
    direct = astar(start, goal, hard - {start}, {})
    around = astar(start, goal, (hard | {tuple(c) for c in other.get("res", [])}) - {start, goal}, {})
    if around is None or direct is None:
        return None
    return (len(around) - len(direct)) * CELL // 1000


def describe(frame: dict, me: dict, other: dict, site: dict | None) -> dict:
    here = (me["x"] // CELL, me["y"] // CELL)
    cat = (site or {}).get("catalog", {})
    load = [cat.get(s.replace("MAT-", ""), {}).get("name", s) for s in me.get("carry", [])]
    return {"id": me["id"], "loaded": bool(load), "load": load, "at": f"{cname(here)} ({zone_name(here)})",
            "heading": me.get("dir"), "going_to": cname(me["goal"]) if me.get("goal") else None,
            "queued_behind_it": [r["id"] for r in frame["robots"] if r.get("wait_on") == me["id"] and r["id"] != other["id"]],
            "pull_aside_segments": _aside_cells(frame, me, other), "reroute_extra_m": _detour_m(frame, me, other),
            "fault": (me.get("fault") or {}).get("type"), "remote_control": me.get("svc") == "remote"}


def rule(a: dict, b: dict) -> tuple[str, str, str, str]:
    """(first, yield, how, reason) by the mine's rules, when there is no model."""
    def cost(x: dict) -> float:   # how hard it is for x to get out of the way
        aside = x["pull_aside_segments"]
        detour = x["reroute_extra_m"]
        return min(aside * 20 if aside is not None else 1e6, detour if detour is not None else 1e6)
    if a["remote_control"] != b["remote_control"]:
        first, other = (a, b) if a["remote_control"] else (b, a)
        why = f"{first['id']} is a faulted truck under remote control"
    elif a["loaded"] != b["loaded"]:
        first, other = (a, b) if a["loaded"] else (b, a)
        why = f"{first['id']} is loaded and has right of way; {other['id']} is empty"
    elif cost(a) != cost(b):
        other, first = (a, b) if cost(a) < cost(b) else (b, a)
        why = f"{other['id']} can clear the way more easily"
    else:
        first, other = (a, b) if a["id"] < b["id"] else (b, a)
        why = "equal standing: the higher truck number yields"
    how = "reroute" if other["reroute_extra_m"] is not None and (other["pull_aside_segments"] is None
                                                                  or other["reroute_extra_m"] <= 60) else "pull_aside"
    return first["id"], other["id"], how, why


class TrafficDesk:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.recent: dict[tuple[str, str], float] = {}
        self.busy: set[tuple[str, str]] = set()
        self.fails = 0
        self.cooldown_until = 0.0

    def observe(self, frame: dict) -> None:
        """Called for every telemetry tick: a new standoff gets a ruling."""
        for e in frame.get("ev", []):
            if e.get("type") != "standoff":
                continue
            pair = tuple(sorted((e["robot"], e["with"])))
            now = time.monotonic()
            if pair in self.busy or now - self.recent.get(pair, 0) < REPEAT_S:
                continue
            self.busy.add(pair)
            self.recent[pair] = now
            asyncio.create_task(self._rule(pair, e))

    async def _rule(self, pair: tuple[str, str], ev: dict) -> None:
        rt = self.rt
        try:
            frame = rt.frame
            if frame is None:
                return
            by = {r["id"]: r for r in frame["robots"]}
            if pair[0] not in by or pair[1] not in by:
                return
            a = describe(frame, by[pair[0]], by[pair[1]], rt.site)
            b = describe(frame, by[pair[1]], by[pair[0]], rt.site)
            cell = ev.get("cell") or [by[pair[0]]["x"] // CELL, by[pair[0]]["y"] // CELL]
            rt.decide({"kind": "traffic", "phase": "ask", "robots": list(pair), "cell": cell})
            source, tokens, t0 = "rules", 0, time.monotonic()
            first = yielder = how = reason = None
            if rt.settings.inference_enabled and time.monotonic() >= self.cooldown_until:
                try:
                    ans, source, tokens = await structured(
                        rt.settings, I_TRAFFIC, {"standoff_at": f"{cname(cell)} ({zone_name(tuple(cell))})", "trucks": [a, b]},
                        RULING, "right_of_way", max_tokens=300, timeout=AI_TIMEOUT_S)
                    first, yielder = str(ans.get("first", "")), str(ans.get("yield", ""))
                    if {first, yielder} != set(pair):
                        raise ModelError(f"ruling names {first}/{yielder}, not {pair}")
                    how = ans.get("how") if ans.get("how") in ("pull_aside", "reroute") else "pull_aside"
                    reason = str(ans.get("reason") or "")[:240]
                    self.fails = 0
                except Exception as exc:
                    self.fails += 1
                    if self.fails >= 3:
                        self.cooldown_until = time.monotonic() + 60
                    log.warning("traffic AI unavailable (%s); ruling by the rules", str(exc)[:160])
                    source, first = "rules", None
            if first is None:
                first, yielder, how, reason = rule(a, b)
            ms = int((time.monotonic() - t0) * 1000)
            # VM B checks the ruling is still current (the yielder must still be holding for `first`)
            by_ = f"{'ai' if source != 'rules' else 'rules'}:{source}"
            try:
                await rt.sim.traffic(yielder, first, reason, by_)
            except SimNodeError as exc:
                log.warning("traffic ruling not delivered: %s", exc)
                return
            payload = {"first": first, "yield": yielder, "how": how, "reason": reason, "cell": cell,
                       "by": "ai" if source != "rules" else "rules", "source": source, "ms": ms, "tokens": tokens,
                       "trucks": {a["id"]: {k: a[k] for k in ("loaded", "pull_aside_segments", "reroute_extra_m", "queued_behind_it")},
                                  b["id"]: {k: b[k] for k in ("loaded", "pull_aside_segments", "reroute_extra_m", "queued_behind_it")}}}
            async with rt.pool.acquire() as c:
                await log_event(c, "ai.traffic" if source != "rules" else "traffic.rule", payload,
                                run_id=rt.run_id, tick=rt.last_tick, robot_id=yielder)
            rt.decide({"kind": "traffic", "phase": "done", **payload})
        except Exception:
            log.exception("traffic ruling %s", pair)
        finally:
            self.busy.discard(pair)


__all__ = ["I_TRAFFIC", "RULING", "TrafficDesk", "describe", "rule"]

