import hmac
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import gdrive, torznab
from .config import API_KEY
from .db import connect, init_db
from .downloader import enqueue, run_download
from .models import DownloadRequest, DriveAuthCodeRequest, DriveResolveRequest
from .parser import save_release
from .sources import load_sources, set_source_enabled

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
    if not key or not hmac.compare_digest(key, API_KEY):
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
    x_api_key: str | None = Header(default=None),
):
    """JSON search used by the web UI (also indexes into SQLite)."""
    require_key(x_api_key)
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
def list_sources(x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
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
    x_api_key: str | None = Header(default=None),
):
    require_key(x_api_key)
    try:
        return set_source_enabled(source_id, body.enabled)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/v1/sources/reload")
def reload_sources(x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    raw, _ = load_sources()
    return {
        "loaded": len(raw),
        "enabled": sum(bool(s.get("enabled")) for s in raw),
    }


@app.get("/api/v1/releases")
def list_releases(limit: int = 100, x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
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
    request: DownloadRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str | None = Header(default=None),
):
    require_key(x_api_key)
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


@app.post("/api/v1/downloads/bulk", status_code=202)
async def add_downloads_bulk(
    body: BulkDownloadRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str | None = Header(default=None),
):
    """Queue many Drive files from one share link (after resolve)."""
    require_key(x_api_key)
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
def list_downloads(x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM downloads ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/v1/downloads/{job_id}")
def download_status(job_id: str, x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    with connect() as db:
        row = db.execute("SELECT * FROM downloads WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Job not found")
    return dict(row)


@app.get("/api/v1/gdrive/status")
def gdrive_status(x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    return gdrive.credentials_status()


@app.get("/api/v1/gdrive/auth-url")
def gdrive_auth_url(x_api_key: str | None = Header(default=None)):
    require_key(x_api_key)
    try:
        return gdrive.build_auth_url()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/gdrive/auth-code")
def gdrive_auth_code(
    body: DriveAuthCodeRequest,
    x_api_key: str | None = Header(default=None),
):
    require_key(x_api_key)
    try:
        return gdrive.exchange_auth_code(body.code)
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/v1/gdrive/resolve")
def gdrive_resolve(
    body: DriveResolveRequest,
    x_api_key: str | None = Header(default=None),
):
    require_key(x_api_key)
    try:
        gdrive.parse_drive_url(str(body.url))
        return gdrive.resolve_url(str(body.url))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"Drive API error: {exc}")
