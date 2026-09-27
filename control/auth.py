"""Operator login (signed session cookie) and node-to-node token auth."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, WebSocket

COOKIE = "replay_session"
SESSION_SECONDS = 7 * 24 * 3600


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)).split("$")[2], digest)


def make_session(username: str, secret: str) -> str:
    payload = f"{username}|{int(time.time()) + SESSION_SECONDS}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def read_session(token: str | None, secret: str) -> str | None:
    if not token:
        return None
    try:
        username, expires, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except (ValueError, UnicodeDecodeError):
        return None
    want = hmac.new(secret.encode(), f"{username}|{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig) or int(expires) < time.time():
        return None
    return username


def current_user(request: Request) -> str:
    """The operator UI is open. A session cookie still names the user when one is present."""
    return read_session(request.cookies.get(COOKIE), request.app.state.settings.session_secret) or "ops"


def ws_user(ws: WebSocket) -> str | None:
    return read_session(ws.cookies.get(COOKIE), ws.app.state.settings.session_secret) or "ops"


def require_node(request: Request) -> None:
    token = request.headers.get("x-node-token", "")
    if not hmac.compare_digest(token, request.app.state.settings.node_token):
        raise HTTPException(401, "bad node token")
