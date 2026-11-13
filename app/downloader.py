"""Download queue workers.

HTTPS direct links use multi-connection ranged downloads (aria2-style) when the
server supports Accept-Ranges. Google Drive stays on the official Drive API.
"""

from __future__ import annotations

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
from .open_redirect import is_open_interstitial, resolve_interstitial, is_drive_url as open_is_drive
from .config import (
    ALLOWED_HOSTS,
    DOWNLOAD_CONNECTIONS,
    DOWNLOAD_DIR,
    DOWNLOAD_MULTI_MIN_MB,
    MAX_DOWNLOAD_BYTES,
    STAGING_DIR,
)
from .db import connect

USER_AGENT = "Directarr/0.2"


def _reject_non_file_response(response: httpx.Response, url: str) -> None:
    """Block saving HTML/JSON error pages as completed media files."""
    ct = (response.headers.get("content-type") or "").lower()
    if any(x in ct for x in ("text/html", "application/xhtml", "text/xml", "application/json")):
        raise ValueError(
            f"URL returned {ct or 'non-file'} instead of a downloadable file. "
            "This is usually a web page or interstitial, not the video. "
            "Use a source with 'Follow posts' enabled so search returns Drive/open links, "
            f"not the post page. URL: {url[:180]}"
        )


def safe_move(src: Path, dest: Path) -> Path:
    """Move src to dest; on cross-device (EXDEV) copy + verify + unlink."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        os.rename(src, dest)
        return dest
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    shutil.copy2(src, dest)
    try:
        if src.stat().st_size != dest.stat().st_size:
            dest.unlink(missing_ok=True)
            raise OSError("Cross-device copy size mismatch")
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


def _set_progress(job_id: str, progress: int) -> None:
    with connect() as db:
        db.execute(
            "UPDATE downloads SET progress=? WHERE id=?",
            (min(99, max(0, int(progress))), job_id),
        )


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
):
    temp = STAGING_DIR / f"{job_id}.part"
    target = DOWNLOAD_DIR / _safe_filename(title, job_id)
    segment_paths: list[Path] = []
    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='downloading',progress=0,error=NULL WHERE id=?",
                (job_id,),
            )

        # AnimeKaizen-style /open/?id= pages embed the real Drive URL
        if not drive_file_id and is_open_interstitial(url):
            try:
                dest = await asyncio.to_thread(resolve_interstitial, url)
                url = dest
            except Exception as exc:
                raise ValueError(f"Could not resolve open/redirect link: {exc}") from exc

        if drive_file_id or gdrive.is_drive_url(url) or open_is_drive(url):
            target = await _download_drive(job_id, title, url, temp, drive_file_id)
        else:
            segment_paths = await _download_http(job_id, url, temp)
            safe_move(temp, target)

        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='completed',progress=100,path=? WHERE id=?",
                (str(target), job_id),
            )
    except Exception as exc:
        temp.unlink(missing_ok=True)
        for p in segment_paths:
            p.unlink(missing_ok=True)
        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='failed',error=? WHERE id=?",
                (str(exc)[:1000], job_id),
            )


async def _probe_http(client: httpx.AsyncClient, url: str) -> tuple[str, int | None, bool]:
    """Return (final_url, content_length or None, accept_ranges)."""
    headers = {"User-Agent": USER_AGENT}
    # Prefer HEAD; some hosts block it — fall back to a 1-byte range GET.
    try:
        head = await client.head(url, headers=headers, follow_redirects=True)
        if head.status_code < 400:
            final = str(head.url)
            validate_public_url(final)
            cl = head.headers.get("content-length")
            size = int(cl) if cl and cl.isdigit() else None
            ar = "bytes" in (head.headers.get("accept-ranges") or "").lower()
            if size is not None or ar:
                return final, size, ar
    except Exception:
        pass

    # Range probe
    probe = await client.get(
        url,
        headers={**headers, "Range": "bytes=0-0"},
        follow_redirects=True,
    )
    final = str(probe.url)
    validate_public_url(final)
    if probe.status_code in (200, 206):
        cr = probe.headers.get("content-range") or ""
        # Content-Range: bytes 0-0/12345
        size = None
        if "/" in cr:
            total = cr.rsplit("/", 1)[-1].strip()
            if total.isdigit():
                size = int(total)
        if size is None:
            cl = probe.headers.get("content-length")
            if probe.status_code == 200 and cl and cl.isdigit():
                size = int(cl)
        ar = probe.status_code == 206 or "bytes" in (probe.headers.get("accept-ranges") or "").lower()
        return final, size, ar
    probe.raise_for_status()
    return final, None, False


async def _download_http(job_id: str, url: str, temp: Path) -> list[Path]:
    """Download via single or multi-connection HTTP. Returns segment paths to clean up."""
    validate_public_url(url)
    limits = httpx.Limits(max_connections=DOWNLOAD_CONNECTIONS + 2, max_keepalive_connections=DOWNLOAD_CONNECTIONS)
    async with httpx.AsyncClient(timeout=None, follow_redirects=True, limits=limits) as client:
        final_url, size, accept_ranges = await _probe_http(client, url)
        if size is not None and size > MAX_DOWNLOAD_BYTES:
            raise ValueError("Configured maximum download size exceeded.")

        min_multi = DOWNLOAD_MULTI_MIN_MB * 1024 * 1024
        use_multi = (
            DOWNLOAD_CONNECTIONS > 1
            and accept_ranges
            and size is not None
            and size >= min_multi
        )

        if use_multi:
            try:
                return await _download_http_multi(client, job_id, final_url, temp, size)
            except Exception:
                for i in range(DOWNLOAD_CONNECTIONS + 4):
                    (STAGING_DIR / f"{job_id}.part.{i}").unlink(missing_ok=True)
                temp.unlink(missing_ok=True)
        await _download_http_single(client, job_id, final_url, temp, size)
        return []


async def _download_http_single(
    client: httpx.AsyncClient,
    job_id: str,
    url: str,
    temp: Path,
    known_size: int | None,
) -> None:
    """Single-stream download with resume if a partial .part already exists."""
    headers = {"User-Agent": USER_AGENT}
    existing = 0
    if temp.is_file():
        existing = temp.stat().st_size
        if known_size and existing >= known_size:
            _set_progress(job_id, 99)
            return
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"

    mode = "ab" if existing else "wb"
    async with client.stream("GET", url, headers=headers) as response:
        if existing and response.status_code == 200:
            # Server ignored Range — restart
            existing = 0
            mode = "wb"
        elif existing and response.status_code not in (200, 206):
            response.raise_for_status()
        elif not existing:
            response.raise_for_status()

        if not existing:
            _reject_non_file_response(response, url)

        if known_size is None:
            cl = response.headers.get("content-length")
            if cl and cl.isdigit():
                # If resumed, content-length is remaining bytes
                known_size = int(cl) + existing

        total = existing
        with temp.open(mode) as output:
            async for chunk in response.aiter_bytes(1024 * 256):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Configured maximum download size exceeded.")
                output.write(chunk)
                if known_size and known_size > 0:
                    _set_progress(job_id, total * 100 // known_size)
                else:
                    _set_progress(job_id, min(99, total * 100 // MAX_DOWNLOAD_BYTES))


async def _download_http_multi(
    client: httpx.AsyncClient,
    job_id: str,
    url: str,
    temp: Path,
    size: int,
) -> list[Path]:
    """Split the file into parallel Range requests, then assemble."""
    n = DOWNLOAD_CONNECTIONS
    max_parts = max(1, size // (1024 * 1024))
    n = max(1, min(n, max_parts))

    part_size = size // n
    ranges: list[tuple[int, int]] = []
    for i in range(n):
        start_b = i * part_size
        end_b = size - 1 if i == n - 1 else (start_b + part_size - 1)
        ranges.append((start_b, end_b))

    segment_paths = [STAGING_DIR / f"{job_id}.part.{i}" for i in range(n)]
    done_bytes = [0] * n
    lock = asyncio.Lock()

    async def report() -> None:
        total = sum(done_bytes)
        if size > 0:
            _set_progress(job_id, total * 100 // size)

    async def fetch_part(idx: int, start_b: int, end_b: int, seg: Path) -> None:
        expected = end_b - start_b + 1
        if seg.is_file() and seg.stat().st_size >= expected:
            done_bytes[idx] = expected
            async with lock:
                await report()
            return

        have = seg.stat().st_size if seg.is_file() else 0
        if have > expected:
            seg.unlink(missing_ok=True)
            have = 0
        headers = {
            "User-Agent": USER_AGENT,
            "Range": f"bytes={start_b + have}-{end_b}",
        }
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 200:
                raise ValueError("Server ignored Range; multi-connection not supported")
            if response.status_code != 206:
                response.raise_for_status()
            mode = "ab" if have else "wb"
            written = have
            with seg.open(mode) as output:
                async for chunk in response.aiter_bytes(1024 * 256):
                    output.write(chunk)
                    written += len(chunk)
                    done_bytes[idx] = min(written, expected)
                    async with lock:
                        await report()
        got = seg.stat().st_size
        if got < expected:
            raise ValueError(f"Segment {idx} incomplete: got {got}, expected {expected}")
        done_bytes[idx] = expected
        async with lock:
            await report()

    await asyncio.gather(
        *[
            fetch_part(i, s, e, p)
            for i, ((s, e), p) in enumerate(zip(ranges, segment_paths))
        ]
    )

    with temp.open("wb") as out:
        for seg in segment_paths:
            with seg.open("rb") as inp:
                shutil.copyfileobj(inp, out, length=1024 * 1024)
            seg.unlink(missing_ok=True)

    if temp.stat().st_size < int(size * 0.99):
        raise ValueError(
            f"Assembled file incomplete: got {temp.stat().st_size}, expected {size}"
        )
    _set_progress(job_id, 99)
    return []


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
                "Use Queue/Resolve to expand the folder and pick files."
            )
        file_id = meta.id
        if meta.name:
            dest_name = _safe_filename(meta.name, job_id)

    def progress_cb(pct: int):
        _set_progress(job_id, pct)

    await asyncio.to_thread(gdrive.download_file_to_path, file_id, temp, progress_cb)
    target = DOWNLOAD_DIR / dest_name
    return safe_move(temp, target)
