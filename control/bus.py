"""Fan-out of live updates to connected browsers. Slow clients drop old messages."""
from __future__ import annotations

import asyncio
import json
from typing import Any


class Hub:
    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._clients.discard(q)

    def publish(self, type_: str, data: Any) -> None:
        if not self._clients:
            return
        msg = json.dumps({"type": type_, "data": data}, default=str)
        for q in list(self._clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)
