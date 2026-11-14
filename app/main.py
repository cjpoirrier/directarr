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
from .db import connect, init_db
from .downloader import cancel_job, clear_downloads, enqueue, run_download
from .models import DownloadRequest, DriveAuthCodeRequest, DriveResolveRequest
from .parser import enrich as enrich_title, save_release
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
    init_db()


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
    apikey: str = Query(default=""),
    t: str = "caps",
    q: str = "",
    season: int | None = None,
    ep: int | None = None,
):
    require_key(apikey)
    if t == "caps":
        return torznab.caps()
    if t in ("search", "tvsearch"):
        _, adapters = load_sources()
        for _, adapter in adapters:
            try:
                for release in await adapter.search(q):
                    save_release(release)
            except Exception:
                continue
        return torznab.results(
            q,
            season if t == "tvsearch" else None,
            ep if t == "tvsearch" else None,
        )
    raise HTTPException(400, f"Unsupported Torznab function: {t}")


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
                _s, _e, res, codec, dual = enrich_title(release.title)
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


@app.get("/api/v1/settings/security")
def settings_security(request: Request, x_api_key: str | None = Header(default=None)):
    """Sonarr-style security settings: current API key for Torznab clients."""
    require_auth(request, x_api_key)
    return {
        "api_key": config.API_KEY,
        "torznab_path": "/api",
    }


@app.post("/api/v1/settings/api-key/regenerate")
def settings_regenerate_api_key(request: Request, x_api_key: str | None = Header(default=None)):
    """Generate a new API key and persist it under config/api_key.txt."""
    require_auth(request, x_api_key)
    import secrets
    new_key = secrets.token_hex(20)
    set_api_key(new_key)
    return {
        "api_key": config.API_KEY,
        "torznab_path": "/api",
        "message": "API key regenerated. Update Sonarr/Radarr indexers with the new key.",
    }


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
