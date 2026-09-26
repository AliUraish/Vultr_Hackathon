"""Deterministic A* on the 4-connected grid.

Search state is (cell, heading) so turns can be penalised: robots stop to turn,
so staircase paths are slow. Ties break on insertion order, never on hashing.
"""
from __future__ import annotations

import heapq

from .world import TURN_COST, W

Cell = tuple[int, int]
_DIRS: tuple[tuple[int, int], ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
_STEP = 10


def astar(start: Cell, goal: Cell, hard: set[Cell], soft: dict[Cell, int]) -> list[list[int]] | None:
    """Cells from start to goal inclusive, or None if unreachable.

    `hard` cells are impassable (walls and racks are always added); `soft` adds
    a cost for entering a cell. The start cell is always allowed.
    """
    if start == goal:
        return [[start[0], start[1]]]
    blocked = W.blocked | hard
    if goal in blocked:
        return None
    gx, gy = goal

    def h(c: Cell) -> int:
        return _STEP * (abs(c[0] - gx) + abs(c[1] - gy))

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
            ng = g + _STEP + soft.get(nc, 0) + (TURN_COST if di not in (-1, ndi) else 0)
            nkey = (nc, ndi)
            if ng < best.get(nkey, 1 << 60):
                best[nkey] = ng
                parent[nkey] = key
                seq += 1
                heapq.heappush(heap, (ng + h(nc), ng, seq, nc, ndi))
    return None
