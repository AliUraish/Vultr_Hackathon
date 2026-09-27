"""VM A -> VM B: the fleet API calls the control plane issues."""
from __future__ import annotations

import httpx


class SimNodeError(RuntimeError):
    pass


class SimNode:
    def __init__(self, base_url: str, token: str) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, headers={"X-Node-Token": token}, timeout=5.0)

    async def _post(self, path: str, body: dict) -> dict:
        try:
            r = await self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise SimNodeError(f"sim node unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise SimNodeError(f"sim node {r.status_code}: {r.text[:300]}")
        return r.json()

    async def command(self, robot: str, steps: list[dict], job: dict | None = None) -> dict:
        return await self._post(f"/fleet/robots/{robot}/commands", {"steps": steps, "job": job})

    async def cancel(self, robot: str) -> dict:
        return await self._post(f"/fleet/robots/{robot}/cancel", {})

    async def chaos(self, scenario: str, hints: dict | None = None, wait_ticks: int = 300) -> dict:
        return await self._post("/fleet/chaos", {"scenario": scenario, "hints": hints or {},
                                                 "wait_ticks": wait_ticks})

    async def policy(self, version: int, rules: list[str], signature: str | None = None) -> dict:
        return await self._post("/fleet/policy", {"version": version, "rules": rules, "signature": signature})

    async def service(self, robot: str, op: str, cell: list[int] | None = None, by: str = "") -> dict:
        return await self._post("/fleet/service", {"robot": robot, "op": op, "cell": cell, "by": by})

    async def witness(self, block: dict) -> dict:
        return await self._post("/fleet/witness", {k: block[k] for k in ("n", "hash", "merkle_root", "prev_hash",
                                                                        "events")})

    async def witnessed(self) -> list[dict]:
        try:
            r = await self._http.get("/fleet/witness")
        except httpx.HTTPError as exc:
            raise SimNodeError(f"sim node unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise SimNodeError(f"sim node {r.status_code}: {r.text[:300]}")
        return r.json()["blocks"]

    async def health(self) -> dict:
        try:
            r = await self._http.get("/health", timeout=2.0)
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SimNodeError(f"sim node unreachable: {exc}") from exc

    async def close(self) -> None:
        await self._http.aclose()
