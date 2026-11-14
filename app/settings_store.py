"""Persistent UI settings (General host/security) stored under config/settings.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config

SETTINGS_FILE = config.CONFIG_DIR / "settings.json"

_DEFAULTS: dict[str, Any] = {
    "bind_address": "0.0.0.0",
    "port": 8787,
    "url_base": "",
    "allowed_hosts": "",
    "auth_mode": None,  # None = use env; "forms" | "none"
    "auth_username": None,  # None = use env
    "auth_required": True,
}


def _read_raw() -> dict[str, Any]:
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def load_settings() -> dict[str, Any]:
    raw = _read_raw()
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in _DEFAULTS or k in raw})
    # Resolve live auth values for API responses
    mode = raw.get("auth_mode")
    if mode not in ("forms", "none"):
        mode = (config.AUTH_MODE or "forms").strip().lower()
        if mode not in ("forms", "none"):
            mode = "forms"
    user = raw.get("auth_username")
    if not user:
        user = config.AUTH_USERNAME or "admin"
    port = raw.get("port")
    try:
        port = int(port) if port is not None else 8787
    except (TypeError, ValueError):
        port = 8787
    url_base = str(raw.get("url_base") or "").strip()
    if url_base and not url_base.startswith("/"):
        url_base = "/" + url_base
    if url_base == "/":
        url_base = ""
    allowed = raw.get("allowed_hosts")
    if allowed is None:
        allowed = ",".join(sorted(config.ALLOWED_HOSTS)) if config.ALLOWED_HOSTS else ""
    auth_required = raw.get("auth_required")
    if auth_required is None:
        auth_required = mode != "none"
    return {
        "bind_address": str(raw.get("bind_address") or "0.0.0.0"),
        "port": port,
        "url_base": url_base,
        "allowed_hosts": str(allowed or ""),
        "auth_mode": mode,
        "auth_username": str(user),
        "auth_required": bool(auth_required),
        "api_key": config.API_KEY,
        "torznab_path": "/api",
    }


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    raw = _read_raw()
    if "bind_address" in patch and patch["bind_address"] is not None:
        raw["bind_address"] = str(patch["bind_address"]).strip() or "0.0.0.0"
    if "port" in patch and patch["port"] is not None:
        try:
            p = int(patch["port"])
            if not (1 <= p <= 65535):
                raise ValueError("port out of range")
            raw["port"] = p
        except (TypeError, ValueError) as e:
            raise ValueError("Port must be an integer between 1 and 65535") from e
    if "url_base" in patch:
        ub = str(patch["url_base"] or "").strip()
        if ub and not ub.startswith("/"):
            ub = "/" + ub
        if ub == "/":
            ub = ""
        raw["url_base"] = ub
    if "allowed_hosts" in patch:
        raw["allowed_hosts"] = str(patch["allowed_hosts"] or "").strip()
    if "auth_mode" in patch and patch["auth_mode"] is not None:
        mode = str(patch["auth_mode"]).strip().lower()
        if mode not in ("forms", "none"):
            raise ValueError("auth_mode must be 'forms' or 'none'")
        raw["auth_mode"] = mode
        raw["auth_required"] = mode != "none"
    if "auth_required" in patch and patch["auth_required"] is not None:
        required = bool(patch["auth_required"])
        raw["auth_required"] = required
        raw["auth_mode"] = "forms" if required else "none"
    if "auth_username" in patch and patch["auth_username"] is not None:
        user = str(patch["auth_username"]).strip()
        if not user:
            raise ValueError("Username cannot be empty")
        if len(user) > 128:
            raise ValueError("Username too long")
        raw["auth_username"] = user
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    # Keep in-memory auth mirrors in sync for this process
    if "auth_mode" in raw:
        config.AUTH_MODE = raw["auth_mode"]
    if "auth_username" in raw:
        config.AUTH_USERNAME = raw["auth_username"]
    return load_settings()


def effective_auth_mode() -> str:
    s = load_settings()
    mode = (s.get("auth_mode") or "forms").lower()
    return mode if mode in ("forms", "none") else "forms"


def effective_username() -> str:
    s = load_settings()
    return str(s.get("auth_username") or config.AUTH_USERNAME or "admin")
