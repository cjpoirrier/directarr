import asyncio
import ipaddress
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import gdrive
from .config import ALLOWED_HOSTS, DOWNLOAD_DIR, MAX_DOWNLOAD_BYTES, STAGING_DIR
from .db import connect


def validate_public_url(url: str) -> str:
    """Validate HTTPS URLs. Drive hosts are allowed and handled by the Drive API path."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Only HTTPS download URLs are accepted.")
    host = parsed.hostname.lower()
    if host in {"drive.google.com", "docs.google.com"}:
        return host
    if ALLOWED_HOSTS and host not in ALLOWED_HOSTS:
        raise ValueError("Host is not in DIRECTARR_ALLOWED_HOSTS.")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise ValueError("Private, loopback, and link-local destinations are blocked.")
    except socket.gaierror:
        raise ValueError("Hostname could not be resolved.")
    return host


def _safe_filename(title: str, job_id: str) -> str:
    safe = "".join(c for c in title if c.isalnum() or c in " ._-()[]")[:180].strip()
    return safe or job_id


def enqueue(title: str, url: str, drive_file_id: str | None = None) -> str:
    """Validate and insert a queued job. Caller schedules run_download."""
    if drive_file_id:
        cleaned = drive_file_id.strip()
        if not cleaned or not all(c.isalnum() or c in "-_" for c in cleaned):
            raise ValueError("Invalid drive_file_id.")
    else:
        validate_public_url(url)
    job_id = str(uuid.uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO downloads(id,title,url,status) VALUES(?,?,?,'queued')",
            (job_id, title, url),
        )
    return job_id


async def run_download(
    job_id: str,
    title: str,
    url: str,
    drive_file_id: str | None = None,
):
    temp = STAGING_DIR / f"{job_id}.part"
    target = DOWNLOAD_DIR / _safe_filename(title, job_id)
    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='downloading',progress=0 WHERE id=?",
                (job_id,),
            )

        if drive_file_id or gdrive.is_drive_url(url):
            target = await _download_drive(job_id, title, url, temp, drive_file_id)
        else:
            await _download_http(job_id, url, temp)
            temp.replace(target)

        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='completed',progress=100,path=? WHERE id=?",
                (str(target), job_id),
            )
    except Exception as exc:
        temp.unlink(missing_ok=True)
        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='failed',error=? WHERE id=?",
                (str(exc)[:1000], job_id),
            )


async def _download_http(job_id: str, url: str, temp: Path) -> None:
    validate_public_url(url)
    total = 0
    content_length = None
    async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
        async with client.stream(
            "GET", url, headers={"User-Agent": "Directarr/0.1"}
        ) as response:
            if response.is_redirect:
                raise ValueError(
                    "Redirects are disabled. Resolve and approve the final HTTPS URL first."
                )
            response.raise_for_status()
            cl = response.headers.get("content-length")
            if cl and cl.isdigit():
                content_length = int(cl)
            with temp.open("wb") as output:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("Configured maximum download size exceeded.")
                    output.write(chunk)
                    if content_length and content_length > 0:
                        progress = min(99, total * 100 // content_length)
                    else:
                        progress = min(99, total * 100 // MAX_DOWNLOAD_BYTES)
                    with connect() as db:
                        db.execute(
                            "UPDATE downloads SET progress=? WHERE id=?",
                            (progress, job_id),
                        )


async def _download_drive(
    job_id: str,
    title: str,
    url: str,
    temp: Path,
    drive_file_id: str | None,
) -> Path:
    """Authorized Drive API download. Returns the final target path."""
    file_id = (drive_file_id or "").strip() or None
    dest_name = _safe_filename(title, job_id)

    if not file_id:
        ref = gdrive.parse_drive_url(url)
        meta = gdrive.get_item_metadata(ref.file_id)
        if meta.is_folder:
            raise ValueError(
                "URL points to a Google Drive folder. "
                "POST /api/v1/gdrive/resolve, pick a file, then POST /api/v1/downloads "
                "with drive_file_id set."
            )
        file_id = meta.id
        if meta.name:
            dest_name = _safe_filename(meta.name, job_id)

    def progress_cb(pct: int):
        with connect() as db:
            db.execute(
                "UPDATE downloads SET progress=? WHERE id=?",
                (min(99, max(0, int(pct))), job_id),
            )

    await asyncio.to_thread(gdrive.download_file_to_path, file_id, temp, progress_cb)
    target = DOWNLOAD_DIR / dest_name
    temp.replace(target)
    return target
