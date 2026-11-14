import hmac
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import gdrive, goindex, torznab
from . import config
from .auth import (
    SESSION_COOKIE,
    auth_mode,
    check_password_login,
    create_session_token,
    require_auth,
    session_user,
    set_password,
    username as auth_username,
)
from .config import set_api_key
from . import applications as apps_mod
from .db import add_history, connect, ensure_schema, init_db, list_history
from .downloader import (
    cancel_job,
    clear_downloads,
    delete_job_files,
    enqueue,
    remove_jobs_from_list,
    run_download,
)
from .models import DownloadRequest, DriveAuthCodeRequest, DriveResolveRequest
from .parser import build_search_queries, enrich as enrich_title, save_release
from .settings_store import load_settings, save_settings
from .sources import (
    add_source,
    delete_source,
    update_source_settings,
    list_adapter_catalog,
    list_sources_public,
    load_sources,
    set_source_enabled,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Directarr",
    version="0.1.0",
    description="Anime direct-download indexer and internal queue manager",
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def startup():
    # Do not take the whole process down if the config volume is not writable yet;
    # /health must still answer so Docker does not mark the container unhealthy forever.
    try:
        init_db()
    except Exception as exc:
        import logging

        logging.getLogger("directarr").exception(
            "Database init failed (check ./config permissions): %s", exc
        )
        # Retry once after a short delay in case the volume was not ready.
        try:
            import time

            time.sleep(0.5)
            init_db()
        except Exception as exc2:
            logging.getLogger("directarr").exception(
                "Database init retry failed: %s", exc2
            )


def require_key(key):
    if not key or not hmac.compare_digest(key, config.API_KEY):
        raise HTTPException(401, "Invalid API key")


@app.get("/")
def ui_root():
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(404, "UI not found")
    return FileResponse(index)


@app.get("/health")
def health():
    return {"status": "ok", "service": "directarr"}


@app.get("/api/v1/auth/status")
def auth_status(request: Request):
    """Public: report auth mode and whether this browser is logged in."""
    user = session_user(request)
    return {
        "mode": auth_mode(),
        "authenticated": bool(user) or auth_mode() == "none",
        "user": user if user else (auth_username() if auth_mode() == "none" else None),
        "username": auth_username() if auth_mode() == "forms" else None,
    }


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


@app.post("/api/v1/auth/login")
def auth_login(body: LoginRequest, response: Response):
    if auth_mode() == "none":
        return {"ok": True, "user": auth_username(), "mode": "none"}
    if not check_password_login(body.username.strip(), body.password):
        raise HTTPException(401, "Invalid username or password")
    token = create_session_token(body.username.strip())
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 14,
        path="/",
    )
    return {"ok": True, "user": body.username.strip()}


@app.post("/api/v1/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=4, max_length=256)


@app.post("/api/v1/auth/password")
def auth_change_password(
    body: PasswordChangeRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    if not check_password_login(auth_username(), body.current_password):
        raise HTTPException(400, "Current password is incorrect")
    set_password(body.new_password)
    return {"ok": True, "message": "Password updated"}



@app.get("/api")
async def torznab_api(
    request: Request,
    apikey: str = Query(default=""),
    t: str = "caps",
    q: str = "",
    season: int | None = None,
    ep: int | None = None,
):
    """Torznab endpoint for Sonarr/Prowlarr.

    tvsearch runs several query variants (show, SxxExx, Season N, …) against
    each adapter, indexes hits, then filters by season/episode for Sonarr.
    """
    require_key(apikey)
    if t == "caps":
        return torznab.caps()
    if t in ("search", "tvsearch"):
        want_season = season if t == "tvsearch" else None
        want_ep = ep if t == "tvsearch" else None
        queries = build_search_queries(q, season=want_season, episode=want_ep)
        # Always include bare series name first; variants follow
        if not queries and q:
            queries = [q.strip()]
        # Cap variants so we do not hammer upstream sites
        queries = queries[:4]
        _, adapters = load_sources()
        seen_urls: set[str] = set()
        for query in queries:
            for _, adapter in adapters:
                try:
                    for release in await adapter.search(query):
                        key = (release.url or "") + "|" + (release.title or "")
                        if key in seen_urls:
                            continue
                        seen_urls.add(key)
                        save_release(release)
                except Exception:
                    continue
        # Build absolute grab URLs so Sonarr can reach the enclosure
        base = str(request.base_url).rstrip("/")
        return torznab.results(q, want_season, want_ep, base_url=base)
    raise HTTPException(400, f"Unsupported Torznab function: {t}")


@app.get("/api/v1/grab/{release_id}")
async def grab_release(
    release_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    apikey: str = Query(default=""),
    drive_file_id: str = Query(default=""),
    title: str = Query(default=""),
    x_api_key: str | None = Header(default=None),
):
    """Torznab grab handoff: queues a Directarr download job when Sonarr fetches the enclosure.

    Optional drive_file_id + title select a single resolved file from a Drive folder.
    Returns a minimal .torrent so Sonarr treats the grab as successful; the real
    file is downloaded by Directarr into the shared downloads folder.
    """
    key = apikey or x_api_key or ""
    require_key(key)
    with connect() as db:
        row = db.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Release not found")
    job_title = (title or row["title"] or "Unknown").strip()
    url = row["url"] or ""
    fid = (drive_file_id or "").strip() or None
    try:
        job_id = enqueue(job_title, url, drive_file_id=fid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    background_tasks.add_task(run_download, job_id, job_title, url, fid)
    add_history("grab", f"Sonarr/Radarr grab queued job {job_id}", details=job_title)

    # Minimal valid-looking torrent response so Sonarr accepts the grab.
    # The actual media is downloaded by Directarr into the downloads volume.
    dummy = (
        b"d8:announce0:4:info"
        b"d4:name" + str(len(job_title)).encode() + b":" + job_title.encode() +
        b"12:piece lengthi16384e6:pieces0:ee"
    )
    return Response(
        content=dummy,
        media_type="application/x-bittorrent",
        headers={"Content-Disposition": f'attachment; filename="{job_id}.torrent"'},
    )


@app.get("/api/v1/search")
async def json_search(request: Request, 
    q: str = Query(min_length=1),
    x_api_key: str | None = Header(default=None),
):
    """JSON search used by the web UI (also indexes into SQLite)."""
    require_auth(request, x_api_key)
    saved = []
    _, adapters = load_sources()
    for _, adapter in adapters:
        try:
            for release in await adapter.search(q):
                rid = save_release(release)
                parsed = enrich_title(release.title)
                _s, _e, res, codec, dual = parsed[0], parsed[1], parsed[2], parsed[3], parsed[4]
                saved.append(
                    {
                        "id": rid,
                        "title": release.title,
                        "url": release.url,
                        "source": release.source,
                        "size": release.size,
                        "season": release.season if release.season is not None else _s,
                        "episode": release.episode if release.episode is not None else _e,
                        "resolution": release.resolution or res,
                        "codec": release.codec or codec,
                        "dual_audio": dual,
                    }
                )
        except Exception:
            continue
    return {"query": q, "count": len(saved), "releases": saved}


@app.get("/api/v1/adapters")
def list_adapters(request: Request, x_api_key: str | None = Header(default=None)):
    """Catalog of adapter types the user can add (Prowlarr-style)."""
    require_auth(request, x_api_key)
    return list_adapter_catalog()


@app.get("/api/v1/sources/{source_id}")
def get_source(request: Request, source_id: str, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    for s in list_sources_public():
        if s.get("id") == source_id:
            return s
    raise HTTPException(404, f"Source not found: {source_id}")


@app.get("/api/v1/sources")
def list_sources(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return list_sources_public()


class SourcePatch(BaseModel):
    enabled: bool | None = None
    settings: dict | None = None


class SourceCreate(BaseModel):
    adapter: str = Field(min_length=1, max_length=64)
    settings: dict = Field(default_factory=dict)
    enabled: bool = True


@app.post("/api/v1/sources", status_code=201)
def create_source(request: Request, body: SourceCreate, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    try:
        return add_source(body.adapter, body.settings, enabled=body.enabled)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"Invalid source config: {exc}")


@app.patch("/api/v1/sources/{source_id}")
def patch_source(request: Request, 
    source_id: str,
    body: SourcePatch,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    try:
        result = None
        if body.settings is not None:
            result = update_source_settings(source_id, body.settings)
        if body.enabled is not None:
            result = set_source_enabled(source_id, body.enabled)
        if result is None:
            raise HTTPException(400, "Provide enabled and/or settings")
        return result
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/v1/sources/{source_id}")
def remove_source(request: Request, source_id: str, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    try:
        delete_source(source_id)
        return {"ok": True, "id": source_id}
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/v1/sources/reload")
def reload_sources(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    raw, _ = load_sources()
    return {
        "loaded": len(raw),
        "enabled": sum(bool(s.get("enabled")) for s in raw),
    }


@app.get("/api/v1/releases")
def list_releases(request: Request, limit: int = 100, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM releases ORDER BY created_at DESC LIMIT ?",
            (min(max(limit, 1), 500),),
        ).fetchall()
    return [dict(row) for row in rows]


class BulkDownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    files: list[dict] = Field(default_factory=list, max_length=500)


@app.post("/api/v1/downloads", status_code=202)
async def add_download(
    body: DownloadRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    try:
        job_id = enqueue(
            body.title, str(body.url), drive_file_id=body.drive_file_id
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    background_tasks.add_task(
        run_download,
        job_id,
        body.title,
        str(body.url),
        body.drive_file_id,
    )
    return {"id": job_id, "status": "queued"}


@app.post("/api/v1/downloads/bulk", status_code=202)
async def add_downloads_bulk(request: Request, 
    body: BulkDownloadRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str | None = Header(default=None),
):
    """Queue many Drive files from one share link (after resolve)."""
    require_auth(request, x_api_key)
    queued = []
    errors = []
    skipped = []
    existing: set[str] = set()
    with connect() as db:
        rows = db.execute(
            "SELECT drive_file_id FROM downloads "
            "WHERE drive_file_id IS NOT NULL AND status IN ('queued','downloading','completed')"
        ).fetchall()
        for row in rows:
            fid = row["drive_file_id"] if hasattr(row, "keys") else row[0]
            if fid:
                existing.add(str(fid))

    for item in body.files:
        title = (item.get("title") or item.get("name") or "").strip()
        file_id = (item.get("drive_file_id") or item.get("id") or "").strip()
        if not title or not file_id:
            errors.append({"item": item, "error": "title and drive_file_id required"})
            continue
        if file_id in existing:
            skipped.append({"title": title, "drive_file_id": file_id})
            continue
        try:
            job_id = enqueue(title, body.url, drive_file_id=file_id)
            background_tasks.add_task(run_download, job_id, title, body.url, file_id)
            queued.append({"id": job_id, "title": title, "drive_file_id": file_id})
            existing.add(file_id)
        except ValueError as exc:
            errors.append({"title": title, "error": str(exc)})
    return {
        "queued": queued,
        "errors": errors,
        "skipped": skipped,
        "count": len(queued),
        "skipped_count": len(skipped),
    }


@app.get("/api/v1/downloads")
def list_downloads(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM downloads ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/v1/downloads/{job_id}")
def download_status(request: Request, job_id: str, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    with connect() as db:
        row = db.execute("SELECT * FROM downloads WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Job not found")
    return dict(row)


@app.post("/api/v1/downloads/{job_id}/cancel")
def cancel_download(
    request: Request, job_id: str, x_api_key: str | None = Header(default=None)
):
    """Cancel a queued or in-progress download. Completed jobs are left alone."""
    require_auth(request, x_api_key)
    row = cancel_job(job_id)
    if row is None:
        raise HTTPException(404, "Job not found")
    return row


@app.delete("/api/v1/downloads/{job_id}")
def delete_download(
    request: Request, job_id: str, x_api_key: str | None = Header(default=None)
):
    """Cancel if still active, then remove the job from the list."""
    require_auth(request, x_api_key)
    cancel_job(job_id)
    with connect() as db:
        cur = db.execute("DELETE FROM downloads WHERE id=?", (job_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Job not found")
    return {"ok": True, "id": job_id}


class ClearDownloadsRequest(BaseModel):
    finished_only: bool = True


class BulkIdsRequest(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=500)


@app.post("/api/v1/downloads/clear")
def clear_download_list(
    request: Request,
    body: ClearDownloadsRequest | None = None,
    x_api_key: str | None = Header(default=None),
):
    """Remove finished jobs from the queue list. finished_only=false clears everything."""
    require_auth(request, x_api_key)
    finished_only = True if body is None else bool(body.finished_only)
    result = clear_downloads(finished_only=finished_only)
    return {"ok": True, **result}


@app.post("/api/v1/downloads/bulk-remove")
def bulk_remove_downloads(
    request: Request,
    body: BulkIdsRequest,
    x_api_key: str | None = Header(default=None),
):
    """Remove multiple jobs from the list only (does not delete files on disk)."""
    require_auth(request, x_api_key)
    if not body.ids:
        raise HTTPException(400, "ids required")
    result = remove_jobs_from_list(body.ids)
    return {"ok": True, **result}


@app.post("/api/v1/downloads/delete-files")
def bulk_delete_download_files(
    request: Request,
    body: BulkIdsRequest,
    x_api_key: str | None = Header(default=None),
):
    """Delete physical files for the given jobs. Leaves the list entries in place."""
    require_auth(request, x_api_key)
    if not body.ids:
        raise HTTPException(400, "ids required")
    result = delete_job_files(body.ids)
    return {"ok": True, **result}


@app.get("/api/v1/settings/security")
def settings_security(request: Request, x_api_key: str | None = Header(default=None)):
    """Sonarr-style security / general settings (host, auth, API key)."""
    require_auth(request, x_api_key)
    return load_settings()


class GeneralSettingsPatch(BaseModel):
    bind_address: str | None = None
    port: int | None = None
    url_base: str | None = None
    allowed_hosts: str | None = None
    auth_mode: str | None = None
    auth_required: bool | None = None
    auth_username: str | None = None
    current_password: str | None = None
    new_password: str | None = None


@app.put("/api/v1/settings/general")
def settings_general_update(
    body: GeneralSettingsPatch,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    """Update Host + Security fields from the General settings tab."""
    require_auth(request, x_api_key)
    patch = body.model_dump(exclude_unset=True)
    # Password change is optional alongside other fields
    new_pw = patch.pop("new_password", None)
    cur_pw = patch.pop("current_password", None)
    try:
        result = save_settings(patch) if patch else load_settings()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if new_pw is not None:
        if not cur_pw or not check_password_login(auth_username(), cur_pw):
            raise HTTPException(400, "Current password is incorrect")
        try:
            set_password(new_pw)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        add_history("settings", "Password changed")
    if patch:
        add_history("settings", "General settings updated", ", ".join(sorted(patch.keys())))
    return result


@app.get("/api/v1/stats")
def stats_overview(request: Request, x_api_key: str | None = Header(default=None)):
    """Aggregate download and release stats for the Stats page."""
    require_auth(request, x_api_key)
    with connect() as db:
        downloads_total = db.execute("SELECT COUNT(*) AS c FROM downloads").fetchone()["c"]
        releases_total = db.execute("SELECT COUNT(*) AS c FROM releases").fetchone()["c"]
        status_rows = db.execute(
            "SELECT status, COUNT(*) AS c FROM downloads GROUP BY status"
        ).fetchall()
        source_rows = db.execute(
            "SELECT source, COUNT(*) AS c FROM releases GROUP BY source ORDER BY c DESC LIMIT 50"
        ).fetchall()
        recent = db.execute(
            "SELECT id, title, status, created_at FROM downloads ORDER BY created_at DESC LIMIT 25"
        ).fetchall()
    downloads_by_status = {str(r["status"] or "unknown"): int(r["c"]) for r in status_rows}
    releases_by_source = [
        {"source": str(r["source"] or "unknown"), "count": int(r["c"])} for r in source_rows
    ]
    recent_downloads = [
        {
            "id": r["id"],
            "title": r["title"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in recent
    ]
    return {
        "downloads_total": int(downloads_total),
        "releases_total": int(releases_total),
        "downloads_by_status": downloads_by_status,
        "releases_by_source": releases_by_source,
        "recent_downloads": recent_downloads,
    }


@app.get("/api/v1/system/status")
def system_status(request: Request, x_api_key: str | None = Header(default=None)):
    """Sonarr/Prowlarr-style system status for the System tab."""
    require_auth(request, x_api_key)
    import platform
    import sys

    raw, adapters = load_sources()
    gstat = gdrive.credentials_status()
    with connect() as db:
        releases = db.execute("SELECT COUNT(*) AS c FROM releases").fetchone()["c"]
        downloads = db.execute("SELECT COUNT(*) AS c FROM downloads").fetchone()["c"]
        active = db.execute(
            "SELECT COUNT(*) AS c FROM downloads WHERE status IN ('queued','downloading')"
        ).fetchone()["c"]
    return {
        "app": "Directarr",
        "version": app.version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "auth_mode": config.AUTH_MODE,
        "config_dir": str(config.CONFIG_DIR),
        "download_dir": str(config.DOWNLOAD_DIR),
        "staging_dir": str(config.STAGING_DIR),
        "max_download_mb": int(config.MAX_DOWNLOAD_BYTES / (1024 * 1024)),
        "download_connections": config.DOWNLOAD_CONNECTIONS,
        "indexers_total": len(raw),
        "indexers_enabled": len(adapters),
        "releases": releases,
        "downloads": downloads,
        "downloads_active": active,
        "gdrive_authorized": bool(gstat.get("authorized")),
        "torznab_path": "/api",
        "health": "ok",
    }


@app.post("/api/v1/settings/api-key/regenerate")
def settings_regenerate_api_key(request: Request, x_api_key: str | None = Header(default=None)):
    """Generate a new API key and persist it under config/api_key.txt."""
    require_auth(request, x_api_key)
    import secrets
    new_key = secrets.token_hex(20)
    set_api_key(new_key)
    add_history("settings", "API key regenerated")
    return {
        "api_key": config.API_KEY,
        "torznab_path": "/api",
        "message": "API key regenerated. Update Sonarr/Radarr indexers with the new key.",
    }


# ── Applications (Sonarr / Radarr / …) ──────────────────────────────────────


@app.get("/api/v1/applications/catalog")
def applications_catalog(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return {"items": apps_mod.catalog()}


@app.get("/api/v1/applications")
def applications_list(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return {"items": apps_mod.list_applications()}


class ApplicationBody(BaseModel):
    name: str | None = None
    app_type: str = "sonarr"
    base_url: str
    api_key: str = ""
    sync_level: str = "fullSync"
    sync_profile_id: str | None = "default"
    enabled: bool = True
    tags: str = ""


@app.post("/api/v1/applications", status_code=201)
def applications_create(
    body: ApplicationBody,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    try:
        return apps_mod.add_application(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        import logging
        logging.getLogger("directarr").exception("applications_create failed")
        raise HTTPException(500, f"Failed to save application: {e}") from e


@app.get("/api/v1/applications/{app_id}")
def applications_get(
    app_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    app = apps_mod.get_application(app_id)
    if not app:
        raise HTTPException(404, "Application not found")
    return app


@app.put("/api/v1/applications/{app_id}")
def applications_update(
    app_id: str,
    body: ApplicationBody,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    try:
        return apps_mod.update_application(app_id, body.model_dump())
    except KeyError:
        raise HTTPException(404, "Application not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/v1/applications/{app_id}")
def applications_delete(
    app_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    if not apps_mod.delete_application(app_id):
        raise HTTPException(404, "Application not found")
    return {"ok": True}


@app.post("/api/v1/applications/{app_id}/test")
async def applications_test(
    app_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    try:
        return await apps_mod.test_application(app_id=app_id)
    except KeyError:
        raise HTTPException(404, "Application not found") from None


@app.post("/api/v1/applications/test")
async def applications_test_body(
    body: ApplicationBody,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    """Test connection before saving."""
    require_auth(request, x_api_key)
    try:
        return await apps_mod.test_application(data=body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ── Sync profiles ───────────────────────────────────────────────────────────


@app.get("/api/v1/sync-profiles")
def sync_profiles_list(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return {"items": apps_mod.list_sync_profiles()}


class SyncProfileBody(BaseModel):
    name: str
    enable_rss: bool = True
    enable_automatic_search: bool = True
    enable_interactive_search: bool = True
    minimum_seeders: int = 1


@app.post("/api/v1/sync-profiles", status_code=201)
def sync_profiles_create(
    body: SyncProfileBody,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    try:
        return apps_mod.upsert_sync_profile(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.put("/api/v1/sync-profiles/{profile_id}")
def sync_profiles_update(
    profile_id: str,
    body: SyncProfileBody,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    try:
        return apps_mod.upsert_sync_profile(body.model_dump(), profile_id=profile_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/v1/sync-profiles/{profile_id}")
def sync_profiles_delete(
    profile_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    try:
        if not apps_mod.delete_sync_profile(profile_id):
            raise HTTPException(404, "Profile not found")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


# ── History ─────────────────────────────────────────────────────────────────


@app.get("/api/v1/history")
def history_list(
    request: Request,
    x_api_key: str | None = Header(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    event_type: str | None = Query(default=None),
):
    require_auth(request, x_api_key)
    return {"items": list_history(limit=limit, event_type=event_type)}


@app.delete("/api/v1/history")
def history_clear(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    ensure_schema()
    with connect() as db:
        db.execute("DELETE FROM history")
    add_history("system", "History cleared")
    return {"ok": True}


@app.get("/api/v1/gdrive/status")
def gdrive_status(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    return gdrive.credentials_status()


@app.get("/api/v1/gdrive/auth-url")
def gdrive_auth_url(request: Request, x_api_key: str | None = Header(default=None)):
    require_auth(request, x_api_key)
    try:
        return gdrive.build_auth_url()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/gdrive/auth-code")
def gdrive_auth_code(request: Request, 
    body: DriveAuthCodeRequest,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    try:
        return gdrive.exchange_auth_code(body.code)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/gdrive/resolve")
def gdrive_resolve(request: Request, 
    body: DriveResolveRequest,
    x_api_key: str | None = Header(default=None),
):
    require_auth(request, x_api_key)
    try:
        gdrive.parse_drive_url(str(body.url))
        return gdrive.resolve_url(str(body.url))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Drive API error: {exc}")


@app.post("/api/v1/resolve")
def resolve_share_url(request: Request, 
    body: DriveResolveRequest,
    x_api_key: str | None = Header(default=None),
):
    """Resolve Google Drive, GoIndex folders, or open-interstitial links."""
    require_auth(request, x_api_key)
    url = str(body.url)
    try:
        from .open_redirect import is_open_interstitial, resolve_interstitial, is_drive_url

        if is_open_interstitial(url):
            url = resolve_interstitial(url)
        if goindex.is_goindex_url(url):
            return goindex.resolve_url(url)
        if is_drive_url(url) or "drive.google.com" in url or "docs.google.com" in url:
            gdrive.parse_drive_url(url)
            return gdrive.resolve_url(url)
        raise ValueError(
            "Unsupported share URL. Expected Google Drive or a GoIndex folder "
            "(e.g. drive.kayoani.online)."
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Resolve error: {exc}")
