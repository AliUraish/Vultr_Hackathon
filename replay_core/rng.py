"""SplitMix64 RNG whose whole state is one hex string inside the sim state.

Python's `random` would work in-process, but its state doesn't round-trip through
JSON cleanly; this one does, so a restored snapshot draws the same numbers.
"""
from __future__ import annotations

MASK64 = (1 << 64) - 1


def seed_state(seed: int) -> str:
    return format(seed & MASK64, "016x")


def _next(state_hex: str) -> tuple[str, int]:
    s = (int(state_hex, 16) + 0x9E3779B97F4A7C15) & MASK64
    z = s
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
    return format(s, "016x"), z ^ (z >> 31)


def randint(state: dict, lo: int, hi: int) -> int:
    """Inclusive random int drawn from (and advancing) state["rng"]."""
    state["rng"], z = _next(state["rng"])
    return lo + z % (hi - lo + 1)
