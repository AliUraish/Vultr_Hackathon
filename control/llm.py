"""One structured call to Vultr Serverless Inference: instructions + JSON payload in, a schema-checked object out.

Chat Completions in JSON mode, with a skeleton of the schema in the system prompt. Callers always
validate the answer and have a deterministic fallback.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .config import Settings
from .diagnosis import _pick_model
from .usage import USAGE


class ModelError(RuntimeError):
    pass


async def structured(settings: Settings, instructions: str, payload: Any, schema: dict, name: str,
                     transport: httpx.AsyncBaseTransport | None = None, max_tokens: int = 2500,
                     timeout: float = 60.0, model: str | None = None) -> tuple[dict, str, int]:
    """(answer, source, tokens). Raises ModelError / httpx.HTTPError; callers fall back to rules.
    Every call is metered under `name` (tokens and cost, see usage.py)."""
    if not settings.inference_enabled:
        raise ModelError("no model configured")
    headers = {"Authorization": f"Bearer {settings.inference_key}"}
    data = payload if isinstance(payload, str) else json.dumps(payload, default=str, separators=(",", ":"))
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(headers=headers, timeout=timeout, transport=transport) as http:
            model = model or await _pick_model(http, settings)
            body = {"model": model, "temperature": 0.1, "max_tokens": min(max_tokens, 4000),
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": instructions + "\nAnswer with only one JSON object shaped like "
                                  + json.dumps(_example(schema)) + "."},
                                 {"role": "user", "content": data}]}
            r = await http.post(f"{settings.inference_url}/chat/completions", json=body)
            if r.status_code == 400 and "response_format" in r.text:   # an endpoint without JSON mode
                body.pop("response_format")
                r = await http.post(f"{settings.inference_url}/chat/completions", json=body)
            r.raise_for_status()
            out = r.json()
            text = out["choices"][0]["message"]["content"] or ""
            u = out.get("usage") or {}
            tin, tout = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    except Exception:
        USAGE.failed(name)
        raise
    USAGE.record(name, tin, tout, ms=int((time.monotonic() - t0) * 1000), model=model)
    try:
        answer = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError as exc:
        raise ModelError(f"not JSON: {text[:120]}") from exc
    if not isinstance(answer, dict):
        raise ModelError("answer is not an object")
    return answer, f"{settings.inference_provider}:{model}", tin + tout


def _example(schema: dict) -> Any:
    """A skeleton of the schema for models without schema-constrained output: keys and value types."""
    t = schema.get("type")
    if t == "object":
        return {k: _example(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return [_example(schema.get("items", {}))]
    if "enum" in schema:
        return "|".join(str(x) for x in schema["enum"])
    return {"string": "...", "number": 0, "integer": 0, "boolean": False}.get(t, "...")


def obj(props: dict) -> dict:
    """A strict-mode object schema: every property required, nothing else allowed."""
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}


__all__ = ["ModelError", "obj", "structured"]
