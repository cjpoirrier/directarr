"""HLS / M3U8 stream detection and FFmpeg-based download for Directarr.

Standalone: works without Sonarr. Detects master/variant playlists, lets the
user pick a quality, and downloads via FFmpeg into the existing queue.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .config import DOWNLOAD_DIR, STAGING_DIR
from .db import connect

USER_AGENT = "Directarr/0.2 StreamDetect"


def _safe_name(title: str, job_id: str) -> str:
    base = re.sub(r"[^\w\s\-_.]", "_", (title or "stream").strip())[:180]
    return f"{base or 'stream'}_{job_id[:8]}"


async def detect_streams(url: str, headers: dict | None = None) -> dict[str, Any]:
    """Fetch a page or M3U8 and return detected variants.

    Returns:
      {
        "input": url,
        "type": "master" | "media" | "page" | "unknown",
        "variants": [
          {"url": ..., "resolution": "1080p", "bandwidth": 5000000, "codecs": "...", "name": "..."}
        ],
        "error": optional
      }
    """
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    result: dict[str, Any] = {"input": url, "type": "unknown", "variants": [], "error": None}

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            text = r.text
            final_url = str(r.url)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    # Direct M3U8?
    if "#EXTM3U" in text or url.lower().endswith(".m3u8") or "m3u8" in final_url.lower():
        return _parse_m3u8(text, final_url)

    # Page: look for m3u8 URLs
    m3u8_urls = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', text, re.I)
    if not m3u8_urls:
        # relative
        rel = re.findall(r'["\']([^"\']+\.m3u8[^"\']*)["\']', text, re.I)
        m3u8_urls = [urljoin(final_url, u) for u in rel]

    if not m3u8_urls:
        result["type"] = "page"
        result["error"] = "No .m3u8 playlists found on the page. Paste an M3U8 URL directly."
        return result

    # Prefer the first master-looking one; fetch and parse
    for murl in m3u8_urls[:3]:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                mr = await client.get(murl, headers=headers)
                mr.raise_for_status()
                parsed = _parse_m3u8(mr.text, str(mr.url))
                if parsed["variants"]:
                    parsed["type"] = "master" if parsed["type"] == "master" else "media"
                    return parsed
        except Exception:
            continue

    result["type"] = "page"
    result["error"] = "Found .m3u8 links but could not parse variants."
    return result


def _parse_m3u8(text: str, base_url: str) -> dict[str, Any]:
    """Parse master or media playlist into variants."""
    out: dict[str, Any] = {"input": base_url, "type": "media", "variants": [], "error": None}
    lines = text.splitlines()
    variants = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            # master
            out["type"] = "master"
            attrs = _parse_inf_attrs(line)
            i += 1
            if i < len(lines):
                uri = lines[i].strip()
                if uri and not uri.startswith("#"):
                    full = urljoin(base_url, uri)
                    res = attrs.get("RESOLUTION", "")
                    height = ""
                    if "x" in res:
                        try:
                            height = res.split("x")[1]
                        except Exception:
                            height = ""
                    variants.append({
                        "url": full,
                        "resolution": f"{height}p" if height else res or "unknown",
                        "bandwidth": int(attrs.get("BANDWIDTH", 0) or 0),
                        "codecs": attrs.get("CODECS", ""),
                        "name": attrs.get("NAME", ""),
                        "average_bandwidth": int(attrs.get("AVERAGE-BANDWIDTH", 0) or 0),
                    })
        elif line.startswith("#EXTINF:") and out["type"] != "master":
            # media playlist (single variant)
            out["type"] = "media"
            variants.append({
                "url": base_url,
                "resolution": "original",
                "bandwidth": 0,
                "codecs": "",
                "name": "media",
                "average_bandwidth": 0,
            })
            break
        i += 1

    # de-dupe by url
    seen = set()
    clean = []
    for v in variants:
        if v["url"] not in seen:
            seen.add(v["url"])
            clean.append(v)
    out["variants"] = clean
    if not clean and "#EXTM3U" in text:
        out["type"] = "media"
        out["variants"] = [{
            "url": base_url,
            "resolution": "original",
            "bandwidth": 0,
            "codecs": "",
            "name": "media",
            "average_bandwidth": 0,
        }]
    return out


def _parse_inf_attrs(line: str) -> dict[str, str]:
    # #EXT-X-STREAM-INF:BANDWIDTH=...,RESOLUTION=...,CODECS="..."
    body = line.split(":", 1)[1] if ":" in line else ""
    attrs = {}
    # simple split on commas not inside quotes
    parts = re.findall(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)', body, re.I)
    for k, v in parts:
        attrs[k.upper()] = v.strip('"')
    return attrs


def enqueue_stream(title: str, stream_url: str, headers: dict | None = None) -> str:
    """Insert a stream job into the downloads table. Caller schedules the worker."""
    job_id = str(uuid.uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO downloads(id,title,url,status) VALUES(?,?,?,'queued')",
            (job_id, title, stream_url),
        )
    return job_id


def run_stream_download(job_id: str, title: str, stream_url: str, headers: dict | None = None) -> None:
    """Blocking FFmpeg download. Updates the downloads row as it goes.

    Called from a background task. Copies to DOWNLOAD_DIR on success.
    """
    from .downloader import is_cancelled, DownloadCancelled

    target_name = _safe_name(title, job_id) + ".mkv"
    staging = STAGING_DIR / f"{job_id}.part.mkv"
    final = DOWNLOAD_DIR / target_name

    try:
        if is_cancelled(job_id):
            raise DownloadCancelled("Cancelled")

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        STAGING_DIR.mkdir(parents=True, exist_ok=True)

        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='downloading', progress=0, error=NULL WHERE id=? AND status!='cancelled'",
                (job_id,),
            )

        # Build header string for FFmpeg
        hdr_lines = []
        if headers:
            for k, v in headers.items():
                if k.lower() in ("user-agent", "referer", "cookie", "origin"):
                    hdr_lines.append(f"{k}: {v}")
        header_arg = "\r\n".join(hdr_lines) + "\r\n" if hdr_lines else ""

        cmd = [
            "ffmpeg", "-y",
            "-hide_banner", "-loglevel", "error",
            "-user_agent", USER_AGENT,
        ]
        if header_arg:
            cmd += ["-headers", header_arg]
        cmd += [
            "-i", stream_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            str(staging),
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Poll for cancel
        while proc.poll() is None:
            if is_cancelled(job_id):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                raise DownloadCancelled("Cancelled")
            # crude progress: just mark as downloading
            import time
            time.sleep(1)

        if proc.returncode != 0:
            err = (proc.stderr.read() or b"").decode(errors="replace")[-500:]
            raise RuntimeError(f"FFmpeg failed: {err or 'unknown error'}")

        if not staging.exists() or staging.stat().st_size < 1000:
            raise RuntimeError("FFmpeg produced no usable output")

        final.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(final)

        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='completed', progress=100, path=?, error=NULL WHERE id=?",
                (str(final), job_id),
            )
    except Exception as exc:
        err = str(exc)[:500]
        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='failed', error=? WHERE id=?",
                (err, job_id),
            )
        if staging.exists():
            try:
                staging.unlink()
            except Exception:
                pass
        raise
