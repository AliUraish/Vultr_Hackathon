"""Deterministic A* on the 4-connected road grid.

Search state is (cell, heading) so bends can be penalised (trucks slow down for
them), driving against a two-lane road's direction costs extra (keep left), and
turning around where the truck stands costs a lot. Ties break on insertion order,
never on hashing.
"""
from __future__ import annotations

import heapq

from .world import LANE_COST, REVERSE_COST, TURN_COST, W

Cell = tuple[int, int]
_DIRS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
_OPPOSITE = (1, 0, 3, 2)
_STEP = 10


def dir_index(d: tuple[int, int] | None) -> int:
    return _DIRS.index(d) if d in _DIRS else -1


def astar(start: Cell, goal: Cell, hard: set[Cell], soft: dict[Cell, int],
          start_dir: tuple[int, int] | None = None, no_turnaround: bool = False) -> list[list[int]] | None:
    """Cells from start to goal inclusive, or None if unreachable.

    `hard` cells are impassable (rock and excavators are always added); `soft` adds
    a cost for entering a cell. The start cell is always allowed. `start_dir` is the
    truck's current heading: leaving the opposite way is a turnaround, which a truck
    on the move can't do (`no_turnaround`).
    """
    if start == goal:
        return [[start[0], start[1]]]
    blocked = W.blocked | hard
    if goal in blocked:
        return None
    gx, gy = goal
    lanes = W.lanes

    def h(c: Cell) -> int:
        return _STEP * (abs(c[0] - gx) + abs(c[1] - gy))

    sdi = dir_index(start_dir)
    start_key = (start, -1)
    best: dict[tuple[Cell, int], int] = {start_key: 0}
    parent: dict[tuple[Cell, int], tuple[Cell, int]] = {}
    heap: list[tuple[int, int, int, Cell, int]] = [(h(start), 0, 0, start, -1)]
    seq = 0
    while heap:
        _, g, _, cell, di = heapq.heappop(heap)
        key = (cell, di)
        if g > best.get(key, g):
            continue
        if cell == goal:
            out = [[cell[0], cell[1]]]
            while key in parent:
                key = parent[key]
                out.append([key[0][0], key[0][1]])
            out.reverse()
            return out
        for ndi, (dx, dy) in enumerate(_DIRS):
            nc = (cell[0] + dx, cell[1] + dy)
            if nc in blocked or not W.passable(nc):
                continue
            prev = di if di != -1 else sdi
            if no_turnaround and di == -1 and sdi != -1 and _OPPOSITE[sdi] == ndi:
                continue
            ng = g + _STEP + soft.get(nc, 0)
            if prev != -1 and prev != ndi:
                ng += REVERSE_COST if _OPPOSITE[prev] == ndi else TURN_COST
            lane = lanes.get(nc)
            if lane is not None and lane[0] == -dx and lane[1] == -dy:
                ng += LANE_COST
            nkey = (nc, ndi)
            if ng < best.get(nkey, 1 << 60):
                best[nkey] = ng
                parent[nkey] = key
                seq += 1
                heapq.heappush(heap, (ng + h(nc), ng, seq, nc, ndi))
    return None


def path_length(path: list[list[int]] | None) -> int:
    """Road cells along a path (0 for a path that is already there, -1 for none)."""
    return -1 if path is None else len(path) - 1
