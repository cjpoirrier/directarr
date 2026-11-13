import hmac
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import config, gdrive, torznab
from .config import AUTH_PASSWORD, AUTH_USERNAME, SESSION_SECRET, set_api_key
from .db import connect, init_db
from .downloader import enqueue, prepare_retry, run_download
from .models import DownloadRequest, DriveAuthCodeRequest, DriveResolveRequest
from .parser import save_release
from .sources import load_sources, set_source_enabled

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Directarr",
    version="0.1.1",
    description="Anime direct-download indexer and internal queue manager",
)

# Cookie-based session for the web UI (Sonarr/Radarr-style forms login).
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="directarr_session",
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 24 * 14,  # 14 days
)

def _static_file(name: str, media_type: str) -> FileResponse:
    path = STATIC_DIR / name
    if not path.is_file():
        raise HTTPException(404, f"Static file missing: {name} (looked in {STATIC_DIR})")
    return FileResponse(path, media_type=media_type)


@app.on_event("startup")
def startup():
    init_db()


def _api_key_ok(key: str | None) -> bool:
    return bool(key) and hmac.compare_digest(key, config.API_KEY)


def _session_ok(request: Request) -> bool:
    """True when UI auth is disabled or the session is logged in."""
    if not AUTH_PASSWORD:
        return True
    return bool(request.session.get("user") == AUTH_USERNAME)


def require_key(key: str | None):
    """Torznab / external clients: API key only."""
    if not _api_key_ok(key):
        raise HTTPException(401, "Invalid API key")


def require_auth(
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    """Web UI and /api/v1: accept either a valid session or API key."""
    if _session_ok(request) or _api_key_ok(x_api_key):
        return
    raise HTTPException(401, "Authentication required")


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


@app.get("/")
def ui_root():
    return _static_file("index.html", "text/html; charset=utf-8")


@app.get("/static/styles.css")
def static_css():
    return _static_file("styles.css", "text/css; charset=utf-8")


@app.get("/static/app.js")
def static_js():
    return _static_file("app.js", "application/javascript; charset=utf-8")


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
def health():
    files = sorted(p.name for p in STATIC_DIR.iterdir() if p.is_file()) if STATIC_DIR.is_dir() else []
    return {
        "status": "ok",
        "service": "directarr",
        "static_dir": str(STATIC_DIR),
        "static_files": files,
    }


@app.get("/api/v1/auth/status")
def auth_status(request: Request):
    """Tell the UI whether forms login is required and if the session is valid."""
    auth_enabled = bool(AUTH_PASSWORD)
    return {
        "auth_enabled": auth_enabled,
        "authenticated": _session_ok(request),
        "username": AUTH_USERNAME if (auth_enabled and _session_ok(request)) else None,
    }


@app.post("/api/v1/auth/login")
def auth_login(body: LoginBody, request: Request):
    if not AUTH_PASSWORD:
        request.session["user"] = AUTH_USERNAME
        return {"ok": True, "auth_enabled": False}
    user_ok = hmac.compare_digest(body.username, AUTH_USERNAME)
    pass_ok = hmac.compare_digest(body.password, AUTH_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Invalid username or password")
    request.session["user"] = AUTH_USERNAME
    return {"ok": True, "username": AUTH_USERNAME}


@app.post("/api/v1/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/v1/settings/security")
def settings_security(_auth=Depends(require_auth)):
    """Sonarr-style security settings: current API key for Torznab clients."""
    return {
        "api_key": config.API_KEY,
        "torznab_path": "/api",
        "auth_enabled": bool(AUTH_PASSWORD),
        "username": AUTH_USERNAME,
    }


@app.post("/api/v1/settings/api-key/regenerate")
def settings_regenerate_api_key(_auth=Depends(require_auth)):
    """Generate a new API key and persist it under config/api_key.txt."""
    import secrets
    new_key = secrets.token_hex(20)
    set_api_key(new_key)
    return {
        "api_key": config.API_KEY,
        "torznab_path": "/api",
        "message": "API key regenerated. Update Sonarr/Radarr indexers with the new key.",
    }


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
async def json_search(
    q: str = Query(min_length=1),
    _auth=Depends(require_auth),
):
    """JSON search used by the web UI (also indexes into SQLite)."""
    saved = []
    _, adapters = load_sources()
    for _, adapter in adapters:
        try:
            for release in await adapter.search(q):
                rid = save_release(release)
                saved.append(
                    {
                        "id": rid,
                        "title": release.title,
                        "url": release.url,
                        "source": release.source,
                        "size": release.size,
                        "season": release.season,
                        "episode": release.episode,
                        "resolution": release.resolution,
                        "codec": release.codec,
                    }
                )
        except Exception:
            continue
    return {"query": q, "count": len(saved), "releases": saved}


@app.get("/api/v1/sources")
def list_sources(_auth=Depends(require_auth)):
    raw, _ = load_sources()
    return [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "enabled": s.get("enabled", False),
            "adapter": s.get("adapter"),
        }
        for s in raw
    ]


class SourcePatch(BaseModel):
    enabled: bool = Field(...)


@app.patch("/api/v1/sources/{source_id}")
def patch_source(
    source_id: str,
    body: SourcePatch,
    _auth=Depends(require_auth),
):
    try:
        return set_source_enabled(source_id, body.enabled)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/v1/sources/reload")
def reload_sources(_auth=Depends(require_auth)):
    raw, _ = load_sources()
    return {
        "loaded": len(raw),
        "enabled": sum(bool(s.get("enabled")) for s in raw),
    }


@app.get("/api/v1/releases")
def list_releases(limit: int = 100, _auth=Depends(require_auth)):
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM releases ORDER BY created_at DESC LIMIT ?",
            (min(max(limit, 1), 500),),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/v1/downloads", status_code=202)
async def add_download(
    request: DownloadRequest,
    background_tasks: BackgroundTasks,
    _auth=Depends(require_auth),
):
    try:
        job_id = enqueue(
            request.title, str(request.url), drive_file_id=request.drive_file_id
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    background_tasks.add_task(
        run_download,
        job_id,
        request.title,
        str(request.url),
        request.drive_file_id,
    )
    return {"id": job_id, "status": "queued"}


class BulkDownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    files: list[dict] = Field(default_factory=list, max_length=500)


@app.post("/api/v1/downloads/bulk", status_code=202)
async def add_downloads_bulk(
    body: BulkDownloadRequest,
    background_tasks: BackgroundTasks,
    _auth=Depends(require_auth),
):
    """Queue multiple Drive files from a share link in one request.

    Skips files that already have an active (queued/downloading) or completed
    job with the same drive_file_id to avoid accidental duplicates.
    """
    queued = []
    errors = []
    skipped = []
    existing_ids: set[str] = set()
    with connect() as db:
        rows = db.execute(
            "SELECT drive_file_id, status FROM downloads "
            "WHERE drive_file_id IS NOT NULL AND status IN ('queued','downloading','completed')"
        ).fetchall()
        for row in rows:
            fid = row["drive_file_id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
            if fid:
                existing_ids.add(str(fid))

    for item in body.files:
        title = (item.get("title") or item.get("name") or "").strip()
        file_id = (item.get("drive_file_id") or item.get("id") or "").strip()
        if not title or not file_id:
            errors.append({"item": item, "error": "title and drive_file_id required"})
            continue
        if file_id in existing_ids:
            skipped.append({"title": title, "drive_file_id": file_id, "reason": "already queued or completed"})
            continue
        try:
            job_id = enqueue(title, body.url, drive_file_id=file_id)
            background_tasks.add_task(run_download, job_id, title, body.url, file_id)
            queued.append({"id": job_id, "title": title, "drive_file_id": file_id})
            existing_ids.add(file_id)
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
def list_downloads(_auth=Depends(require_auth)):
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM downloads ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/v1/downloads/{job_id}")
def download_status(job_id: str, _auth=Depends(require_auth)):
    with connect() as db:
        row = db.execute("SELECT * FROM downloads WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Job not found")
    return dict(row)


@app.post("/api/v1/downloads/{job_id}/retry", status_code=202)
async def retry_download(
    job_id: str,
    background_tasks: BackgroundTasks,
    _auth=Depends(require_auth),
):
    """Re-queue a failed job. Uses existing staging .part when nearly complete."""
    try:
        job = prepare_retry(job_id)
    except KeyError:
        raise HTTPException(404, "Job not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    background_tasks.add_task(
        run_download,
        job["id"],
        job["title"],
        job["url"],
        job.get("drive_file_id"),
        finalize_only=bool(job.get("finalize_only")),
    )
    return {
        "id": job["id"],
        "status": "queued",
        "finalize_only": bool(job.get("finalize_only")),
    }


@app.get("/api/v1/gdrive/status")
def gdrive_status(_auth=Depends(require_auth)):
    return gdrive.credentials_status()


@app.post("/api/v1/gdrive/disconnect")
def gdrive_disconnect(_auth=Depends(require_auth)):
    """Clear stored Drive token so the account can be reauthorized."""
    return gdrive.disconnect()


@app.get("/api/v1/gdrive/auth-url")
def gdrive_auth_url(_auth=Depends(require_auth)):
    try:
        return gdrive.build_auth_url()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/v1/gdrive/callback")
def gdrive_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Google redirects here after consent. Backend exchanges the code — no paste step.

    Does not require session auth: the OAuth `state` parameter is the CSRF check.
    """
    from .config import PUBLIC_BASE_URL

    ui = (PUBLIC_BASE_URL or "").rstrip("/") or ""
    # Fall back to relative redirect into the SPA.
    success_path = f"{ui}/?gdrive=connected#gdrive" if ui else "/?gdrive=connected"
    error_path = f"{ui}/?gdrive=error" if ui else "/?gdrive=error"

    if error:
        return RedirectResponse(f"{error_path}&message={error}", status_code=302)
    if not code:
        return RedirectResponse(f"{error_path}&message=missing_code", status_code=302)
    try:
        gdrive.exchange_auth_code(code, state=state)
        return RedirectResponse(success_path, status_code=302)
    except Exception as exc:
        from urllib.parse import quote
        return RedirectResponse(
            f"{error_path}&message={quote(str(exc)[:200])}",
            status_code=302,
        )


@app.post("/api/v1/gdrive/auth-code")
def gdrive_auth_code(
    body: DriveAuthCodeRequest,
    _auth=Depends(require_auth),
):
    """Manual paste fallback when server callback is not configured."""
    try:
        return gdrive.exchange_auth_code(body.code)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/gdrive/resolve")
def gdrive_resolve(
    body: DriveResolveRequest,
    _auth=Depends(require_auth),
):
    try:
        gdrive.parse_drive_url(str(body.url))
        return gdrive.resolve_url(str(body.url), recursive=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Drive API error: {exc}")
