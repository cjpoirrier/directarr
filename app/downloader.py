import asyncio
import errno
import ipaddress
import os
import shutil
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import gdrive
from .config import ALLOWED_HOSTS, DOWNLOAD_DIR, MAX_DOWNLOAD_BYTES, STAGING_DIR
from .db import connect


def safe_move(src: Path, dest: Path) -> Path:
    """Move src to dest, handling cross-device (Errno 18) by copy+verify+unlink.

    Staging and downloads often live on different Docker volume mounts, so
    os.rename / shutil.move can fail with EXDEV. Only remove the staging file
    after the destination has been written and size-verified.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        os.rename(src, dest)
        return dest
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    # Cross-device: copy, verify, then remove source.
    shutil.copy2(src, dest)
    try:
        src_size = src.stat().st_size
        dest_size = dest.stat().st_size
        if src_size != dest_size:
            dest.unlink(missing_ok=True)
            raise OSError(
                f"Cross-device copy size mismatch: source={src_size} dest={dest_size}"
            )
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    src.unlink(missing_ok=True)
    return dest


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


def _staging_path(job_id: str) -> Path:
    return STAGING_DIR / f"{job_id}.part"


def _part_is_nearly_complete(temp: Path, min_bytes: int = 1024 * 1024) -> bool:
    """True when a staging file looks finished (typical after a move-only failure)."""
    try:
        return temp.is_file() and temp.stat().st_size >= min_bytes
    except OSError:
        return False


def enqueue(title: str, url: str, drive_file_id: str | None = None) -> str:
    """Validate and insert a queued job. Caller schedules run_download."""
    cleaned_id = None
    if drive_file_id:
        cleaned_id = drive_file_id.strip()
        if not cleaned_id or not all(c.isalnum() or c in "-_" for c in cleaned_id):
            raise ValueError("Invalid drive_file_id.")
    else:
        validate_public_url(url)
    job_id = str(uuid.uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO downloads(id,title,url,status,drive_file_id) VALUES(?,?,?,'queued',?)",
            (job_id, title, url, cleaned_id),
        )
    return job_id


async def run_download(
    job_id: str,
    title: str,
    url: str,
    drive_file_id: str | None = None,
    *,
    finalize_only: bool = False,
):
    temp = _staging_path(job_id)
    target = DOWNLOAD_DIR / _safe_filename(title, job_id)
    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='downloading',progress=0,error=NULL,path=NULL WHERE id=?",
                (job_id,),
            )

        if finalize_only and _part_is_nearly_complete(temp):
            # Previous attempt already wrote the payload; only complete the move.
            with connect() as db:
                db.execute(
                    "UPDATE downloads SET progress=99 WHERE id=?",
                    (job_id,),
                )
            target = safe_move(temp, target)
        elif drive_file_id or gdrive.is_drive_url(url):
            target = await _download_drive(job_id, title, url, temp, drive_file_id)
        else:
            await _download_http(job_id, url, temp)
            target = safe_move(temp, target)

        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='completed',progress=100,path=?,error=NULL WHERE id=?",
                (str(target), job_id),
            )
    except Exception as exc:
        # Keep a near-complete .part so retry can finalize without re-downloading.
        # Drop small/partial staging files to avoid filling the volume.
        if temp.exists() and not _part_is_nearly_complete(temp):
            temp.unlink(missing_ok=True)
        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='failed',error=? WHERE id=?",
                (str(exc)[:1000], job_id),
            )


def prepare_retry(job_id: str) -> dict:
    """Reset a failed job for retry. Returns fields needed to re-run.

    If a substantial staging .part still exists (typical after a cross-device
    move failure), the next run finalizes only instead of re-downloading.
    """
    with connect() as db:
        row = db.execute("SELECT * FROM downloads WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("Job not found")
        job = dict(row)
        if job.get("status") != "failed":
            raise ValueError(
                f"Only failed jobs can be retried (status={job.get('status')})"
            )

        temp = _staging_path(job_id)
        finalize_only = _part_is_nearly_complete(temp)
        db.execute(
            """UPDATE downloads
               SET status='queued', progress=0, error=NULL, path=NULL
               WHERE id=?""",
            (job_id,),
        )
    job["finalize_only"] = finalize_only
    return job


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
    return safe_move(temp, target)
