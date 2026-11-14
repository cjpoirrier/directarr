"""Sonarr-style auth: session login for UI, API key for external clients."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from fastapi import Header, HTTPException, Request

from . import config

SESSION_COOKIE = "directarr_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def _session_secret() -> str:
    path = Path(config.SESSION_SECRET_FILE)
    try:
        if path.is_file():
            secret = path.read_text(encoding="utf-8").strip()
            if secret:
                return secret
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret + "\n", encoding="utf-8")
    except OSError:
        pass
    return secret


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 200_000
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations_s, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations_s)
        check = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
        ).hex()
        return hmac.compare_digest(check, digest)
    except Exception:
        return False


def get_password_hash() -> str:
    path = Path(config.AUTH_PASSWORD_HASH_FILE)
    try:
        if path.is_file():
            stored = path.read_text(encoding="utf-8").strip()
            if stored:
                return stored
    except OSError:
        pass
    hashed = hash_password(config.AUTH_PASSWORD)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(hashed + "\n", encoding="utf-8")
    except OSError:
        pass
    return hashed


def set_password(new_password: str) -> None:
    if len(new_password) < 4:
        raise ValueError("Password must be at least 4 characters")
    path = Path(config.AUTH_PASSWORD_HASH_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hash_password(new_password) + "\n", encoding="utf-8")


def auth_mode() -> str:
    mode = (config.AUTH_MODE or "forms").lower()
    return mode if mode in {"forms", "none"} else "forms"


def username() -> str:
    return config.AUTH_USERNAME or "admin"


def create_session_token(user: str) -> str:
    payload = json.dumps(
        {"u": user, "exp": int(time.time()) + SESSION_MAX_AGE},
        separators=(",", ":"),
    ).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    sig = hmac.new(
        _session_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{body}.{sig}"


def read_session_token(token: str) -> str | None:
    try:
        body, sig = token.rsplit(".", 1)
        expect = hmac.new(
            _session_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        pad = "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body + pad).decode("utf-8"))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        user = data.get("u")
        return user if user else None
    except Exception:
        return None


def session_user(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return read_session_token(token)


def check_password_login(user: str, password: str) -> bool:
    expected = username()
    # compare_digest requires equal length
    if len(user) != len(expected) or not hmac.compare_digest(user, expected):
        # still run a dummy hash to reduce timing leaks a bit
        verify_password(password, get_password_hash())
        return False
    return verify_password(password, get_password_hash())


def require_auth(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> None:
    """Allow valid session cookie OR API key. AUTH_MODE=none skips checks."""
    if auth_mode() == "none":
        return
    if x_api_key and hmac.compare_digest(x_api_key, config.API_KEY):
        return
    if session_user(request):
        return
    raise HTTPException(401, "Authentication required")
