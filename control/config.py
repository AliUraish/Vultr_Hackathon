"""All configuration comes from the environment (see infra/env/control.env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _need(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    node_token: str
    simnode_url: str
    session_secret: str
    admin_user: str
    admin_password: str
    inference_provider: str   # always "vultr": Vultr Serverless Inference
    inference_key: str
    inference_url: str
    inference_model: str
    auto_jobs: bool
    cookie_secure: bool
    retention_hours: int
    policy_key_file: str = ""

    @property
    def inference_enabled(self) -> bool:
        return bool(self.inference_key)


VULTR_INFERENCE_URL = "https://api.vultrinference.com/v1"


def _inference(env: os._Environ[str]) -> tuple[str, str, str, str]:
    """(provider, key, url, model). Every model call goes to Vultr Serverless Inference."""
    provider = env.get("INFERENCE_PROVIDER") or "vultr"
    if provider != "vultr":
        raise RuntimeError(f"INFERENCE_PROVIDER={provider!r}: only Vultr Serverless Inference (vultr) is supported")
    key = env.get("INFERENCE_KEY") or env.get("VULTR_INFERENCE_KEY", "")
    url = env.get("INFERENCE_URL") or env.get("VULTR_INFERENCE_URL") or VULTR_INFERENCE_URL
    model = env.get("INFERENCE_MODEL") or env.get("VULTR_INFERENCE_MODEL", "")
    return provider, key, url.rstrip("/"), model


def load() -> Settings:
    env = os.environ
    provider, key, url, model = _inference(env)
    return Settings(
        database_url=_need("DATABASE_URL"),
        node_token=_need("NODE_TOKEN"),
        simnode_url=_need("SIMNODE_URL").rstrip("/"),
        session_secret=_need("SESSION_SECRET"),
        admin_user=env.get("ADMIN_USER", "ops"),
        admin_password=_need("ADMIN_PASSWORD"),
        inference_provider=provider,
        inference_key=key,
        inference_url=url,
        inference_model=model,
        auto_jobs=env.get("AUTO_JOBS", "1") == "1",
        cookie_secure=env.get("COOKIE_SECURE", "0") == "1",
        retention_hours=int(env.get("TICK_RETENTION_HOURS", "12")),
        policy_key_file=env.get("POLICY_SIGNING_KEY_FILE", ""),
    )
