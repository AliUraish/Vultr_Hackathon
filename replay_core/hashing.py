"""Canonical JSON and hashes. Every stored or compared hash goes through here."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def clone(obj: Any) -> Any:
    """Deep copy through JSON, so the copy is exactly what a stored snapshot reloads as."""
    return json.loads(canonical(obj))


def chain_hash(hashes: list[str]) -> str:
    """One hash over a per-tick hash trajectory."""
    h = hashlib.sha256()
    for x in hashes:
        h.update(x.encode())
    return h.hexdigest()
