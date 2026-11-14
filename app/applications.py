"""Prowlarr-style Applications (Sonarr / Radarr / Lidarr / …) registry."""
from __future__ import annotations

import secrets
import uuid
from typing import Any
from urllib.parse import urljoin

import httpx

from .db import add_history, connect

APP_TYPES = {
    "sonarr": {"label": "Sonarr", "test_path": "/api/v3/system/status", "api_header": "X-Api-Key"},
    "radarr": {"label": "Radarr", "test_path": "/api/v3/system/status", "api_header": "X-Api-Key"},
    "lidarr": {"label": "Lidarr", "test_path": "/api/v1/system/status", "api_header": "X-Api-Key"},
    "readarr": {"label": "Readarr", "test_path": "/api/v1/system/status", "api_header": "X-Api-Key"},
    "whisparr": {"label": "Whisparr", "test_path": "/api/v3/system/status", "api_header": "X-Api-Key"},
    "lazylibrarian": {"label": "LazyLibrarian", "test_path": "/api", "api_header": "X-Api-Key"},
}

SYNC_LEVELS = ("addOnly", "fullSync")


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    return d


def list_applications() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM applications ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_application(app_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    return _row_to_dict(row) if row else None


def add_application(data: dict[str, Any]) -> dict[str, Any]:
    app_type = str(data.get("app_type") or "").strip().lower()
    if app_type not in APP_TYPES:
        raise ValueError(f"Unsupported app_type. Choose one of: {', '.join(APP_TYPES)}")
    name = str(data.get("name") or APP_TYPES[app_type]["label"]).strip()
    base_url = str(data.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("base_url is required")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    api_key = str(data.get("api_key") or "").strip()
    sync_level = str(data.get("sync_level") or "fullSync").strip()
    if sync_level not in SYNC_LEVELS:
        sync_level = "fullSync"
    enabled = 1 if data.get("enabled", True) else 0
    tags = str(data.get("tags") or "").strip()
    sync_profile_id = data.get("sync_profile_id") or "default"
    app_id = str(uuid.uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO applications(id,name,app_type,base_url,api_key,sync_level,"
            "sync_profile_id,enabled,tags) VALUES(?,?,?,?,?,?,?,?,?)",
            (app_id, name, app_type, base_url, api_key, sync_level, sync_profile_id, enabled, tags),
        )
    add_history("application", f"Added application {name} ({app_type})", base_url)
    return get_application(app_id)  # type: ignore


def update_application(app_id: str, data: dict[str, Any]) -> dict[str, Any]:
    existing = get_application(app_id)
    if not existing:
        raise KeyError("Application not found")
    name = str(data.get("name", existing["name"])).strip()
    base_url = str(data.get("base_url", existing["base_url"])).strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    api_key = data.get("api_key")
    if api_key is None:
        api_key = existing["api_key"]
    else:
        api_key = str(api_key).strip()
    sync_level = str(data.get("sync_level", existing["sync_level"])).strip()
    if sync_level not in SYNC_LEVELS:
        sync_level = existing["sync_level"]
    enabled = existing["enabled"]
    if "enabled" in data:
        enabled = 1 if data["enabled"] else 0
    tags = str(data.get("tags", existing.get("tags") or "")).strip()
    sync_profile_id = data.get("sync_profile_id", existing.get("sync_profile_id") or "default")
    app_type = existing["app_type"]
    if data.get("app_type"):
        at = str(data["app_type"]).strip().lower()
        if at in APP_TYPES:
            app_type = at
    with connect() as db:
        db.execute(
            "UPDATE applications SET name=?, app_type=?, base_url=?, api_key=?, sync_level=?,"
            "sync_profile_id=?, enabled=?, tags=? WHERE id=?",
            (name, app_type, base_url, api_key, sync_level, sync_profile_id, enabled, tags, app_id),
        )
    add_history("application", f"Updated application {name}", app_id)
    return get_application(app_id)  # type: ignore


def delete_application(app_id: str) -> bool:
    existing = get_application(app_id)
    if not existing:
        return False
    with connect() as db:
        db.execute("DELETE FROM applications WHERE id=?", (app_id,))
    add_history("application", f"Removed application {existing['name']}", app_id)
    return True


async def test_application(app_id: str | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Hit the *arr system/status endpoint to verify connectivity."""
    if app_id:
        app = get_application(app_id)
        if not app:
            raise KeyError("Application not found")
    else:
        app = data or {}
        if not app.get("base_url"):
            raise ValueError("base_url required for test")
        app = {
            "app_type": str(app.get("app_type") or "sonarr").lower(),
            "base_url": str(app["base_url"]).rstrip("/"),
            "api_key": str(app.get("api_key") or ""),
            "name": app.get("name") or "test",
        }
    meta = APP_TYPES.get(app["app_type"], APP_TYPES["sonarr"])
    url = urljoin(app["base_url"] + "/", meta["test_path"].lstrip("/"))
    headers = {meta["api_header"]: app.get("api_key") or ""}
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        ok = resp.status_code < 400
        body_preview = ""
        try:
            j = resp.json()
            body_preview = str(j.get("version") or j.get("appName") or j)[:200]
        except Exception:
            body_preview = (resp.text or "")[:200]
        status = "ok" if ok else f"HTTP {resp.status_code}"
        if app_id:
            with connect() as db:
                db.execute(
                    "UPDATE applications SET last_sync_at=CURRENT_TIMESTAMP, last_sync_status=? WHERE id=?",
                    (status if ok else f"fail:{resp.status_code}", app_id),
                )
        add_history(
            "application",
            f"Test {'succeeded' if ok else 'failed'} for {app.get('name')}",
            f"{url} → {resp.status_code}",
        )
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "message": body_preview or status,
            "url": url,
        }
    except Exception as e:
        if app_id:
            with connect() as db:
                db.execute(
                    "UPDATE applications SET last_sync_at=CURRENT_TIMESTAMP, last_sync_status=? WHERE id=?",
                    (f"error:{e}", app_id),
                )
        add_history("application", f"Test error for {app.get('name')}", str(e)[:300])
        return {"ok": False, "status_code": 0, "message": str(e), "url": url}


def list_sync_profiles() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM sync_profiles ORDER BY name COLLATE NOCASE").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["enable_rss"] = bool(d.get("enable_rss"))
        d["enable_automatic_search"] = bool(d.get("enable_automatic_search"))
        d["enable_interactive_search"] = bool(d.get("enable_interactive_search"))
        out.append(d)
    return out


def upsert_sync_profile(data: dict[str, Any], profile_id: str | None = None) -> dict[str, Any]:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    pid = profile_id or str(uuid.uuid4())
    enable_rss = 1 if data.get("enable_rss", True) else 0
    enable_auto = 1 if data.get("enable_automatic_search", True) else 0
    enable_inter = 1 if data.get("enable_interactive_search", True) else 0
    try:
        min_seed = max(0, int(data.get("minimum_seeders", 1)))
    except (TypeError, ValueError):
        min_seed = 1
    with connect() as db:
        existing = db.execute("SELECT id FROM sync_profiles WHERE id=?", (pid,)).fetchone()
        if existing:
            db.execute(
                "UPDATE sync_profiles SET name=?, enable_rss=?, enable_automatic_search=?,"
                "enable_interactive_search=?, minimum_seeders=? WHERE id=?",
                (name, enable_rss, enable_auto, enable_inter, min_seed, pid),
            )
        else:
            db.execute(
                "INSERT INTO sync_profiles(id,name,enable_rss,enable_automatic_search,"
                "enable_interactive_search,minimum_seeders) VALUES(?,?,?,?,?,?)",
                (pid, name, enable_rss, enable_auto, enable_inter, min_seed),
            )
    add_history("sync_profile", f"Saved sync profile {name}", pid)
    for p in list_sync_profiles():
        if p["id"] == pid:
            return p
    raise RuntimeError("profile missing after save")


def delete_sync_profile(profile_id: str) -> bool:
    if profile_id == "default":
        raise ValueError("Cannot delete the default sync profile")
    with connect() as db:
        cur = db.execute("DELETE FROM sync_profiles WHERE id=?", (profile_id,))
        # Re-point apps to default
        db.execute(
            "UPDATE applications SET sync_profile_id='default' WHERE sync_profile_id=?",
            (profile_id,),
        )
    if cur.rowcount:
        add_history("sync_profile", f"Deleted sync profile {profile_id}", profile_id)
    return cur.rowcount > 0


def catalog() -> list[dict[str, str]]:
    return [{"id": k, "label": v["label"]} for k, v in APP_TYPES.items()]
