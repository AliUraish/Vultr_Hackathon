"""One structured call to the configured model: instructions + JSON payload in, a schema-checked object out.

OpenAI goes through the Responses API with a strict JSON schema; any other OpenAI-compatible
endpoint (Vultr Serverless Inference, a CPU-hosted llama.cpp server...) through Chat Completions
in JSON mode. Callers always validate the answer and have a deterministic fallback.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Settings
from .diagnosis import _pick_model


class ModelError(RuntimeError):
    pass


async def structured(settings: Settings, instructions: str, payload: Any, schema: dict, name: str,
                     transport: httpx.AsyncBaseTransport | None = None, max_tokens: int = 2500) -> tuple[dict, str, int]:
    """(answer, source, tokens). Raises ModelError / httpx.HTTPError; callers fall back to rules."""
    if not settings.inference_enabled:
        raise ModelError("no model configured")
    headers = {"Authorization": f"Bearer {settings.inference_key}"}
    data = json.dumps(payload, default=str)
    async with httpx.AsyncClient(headers=headers, timeout=60.0, transport=transport) as http:
        model = await _pick_model(http, settings)
        if settings.inference_provider == "openai":
            body: dict[str, Any] = {"model": model, "instructions": instructions, "input": data,
                                    "max_output_tokens": max_tokens, "reasoning": {"effort": "low"},
                                    "text": {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}}}
            r = await http.post(f"{settings.inference_url}/responses", json=body)
            if r.status_code == 400 and "reasoning" in r.text:
                body.pop("reasoning")
                r = await http.post(f"{settings.inference_url}/responses", json=body)
            r.raise_for_status()
            out = r.json()
            text = "".join(c.get("text", "") for it in out.get("output") or [] if it.get("type") == "message"
                           for c in it.get("content") or [] if c.get("type") == "output_text")
            u = out.get("usage") or {}
            tokens = int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
        else:
            body = {"model": model, "temperature": 0.1, "max_tokens": min(max_tokens, 1200),
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": instructions + "\nAnswer with one JSON object with the keys "
                                  + ", ".join(schema["properties"]) + "."},
                                 {"role": "user", "content": data}]}
            r = await http.post(f"{settings.inference_url}/chat/completions", json=body)
            r.raise_for_status()
            out = r.json()
            text = out["choices"][0]["message"]["content"] or ""
            u = out.get("usage") or {}
            tokens = int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
    try:
        answer = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError as exc:
        raise ModelError(f"not JSON: {text[:120]}") from exc
    if not isinstance(answer, dict):
        raise ModelError("answer is not an object")
    return answer, f"{settings.inference_provider}:{model}", tokens


def obj(props: dict) -> dict:
    """A strict-mode object schema: every property required, nothing else allowed."""
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}


__all__ = ["ModelError", "obj", "structured"]
