"""HLS / M3U8 stream detection and FFmpeg-based download for Directarr.

Standalone: works without Sonarr. Detects master/variant playlists, lets the
user pick a quality, and downloads via FFmpeg into the existing queue.
"""

from __future__ import annotations

import logging
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .config import DOWNLOAD_DIR, STAGING_DIR
from .db import connect

log = logging.getLogger("directarr.streams")
USER_AGENT = "Directarr/0.2 StreamDetect"

# Simple ring buffer for recent stream-related log lines (shared with main)
_LOG_BUFFER: list[str] = []
_LOG_MAX = 200


def _log(msg: str, level: str = "info") -> None:
    """Write to logger and keep a short in-memory buffer for the UI."""
    line = f"[streams] {msg}"
    getattr(log, level, log.info)(msg)
    _LOG_BUFFER.append(line)
    if len(_LOG_BUFFER) > _LOG_MAX:
        del _LOG_BUFFER[: len(_LOG_BUFFER) - _LOG_MAX]


def get_recent_logs() -> list[str]:
    return list(_LOG_BUFFER[-100:])


def _safe_name(title: str, job_id: str) -> str:
    base = re.sub(r"[^\w\s\-_.]", "_", (title or "stream").strip())[:180]
    return f"{base or 'stream'}_{job_id[:8]}"


async def detect_streams(url: str, headers: dict | None = None) -> dict[str, Any]:
    """Fetch a page or M3U8 and return detected variants.

    The returned dict always includes a "log" list of steps for the UI.
    """
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    result: dict[str, Any] = {
        "input": url,
        "type": "unknown",
        "variants": [],
        "error": None,
        "log": [],
    }

    def step(msg: str) -> None:
        result["log"].append(msg)
        _log(msg)

    step(f"detect start: {url}")

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            text = r.text
            final_url = str(r.url)
        step(f"fetched {final_url} status={r.status_code} bytes={len(text)}")
    except Exception as exc:
        step(f"fetch failed: {exc}")
        result["error"] = str(exc)
        return result

    # Direct M3U8?
    if "#EXTM3U" in text or url.lower().endswith(".m3u8") or "m3u8" in final_url.lower():
        parsed = _parse_m3u8(text, final_url)
        step(f"parsed as m3u8 type={parsed['type']} variants={len(parsed['variants'])}")
        parsed["log"] = result["log"] + parsed.get("log", [])
        return parsed

    # Page: look for m3u8 URLs
    m3u8_urls = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', text, re.I)
    if not m3u8_urls:
        rel = re.findall(r'["\']([^"\']+\.m3u8[^"\']*)["\']', text, re.I)
        m3u8_urls = [urljoin(final_url, u) for u in rel]
    step(f"page scan found {len(m3u8_urls)} m3u8 candidates")

    # Dooplay / similar: player API returns an embed URL
    if not m3u8_urls:
        doo = re.search(r'player_api["\']?\s*[:=]\s*["\']([^"\']+)["\']', text)
        post = re.search(r"data-post=['\"](\d+)['\"]", text)
        ptype = re.search(r"data-type=['\"](\w+)['\"]", text)
        nume = re.search(r"data-nume=['\"](\d+)['\"]", text)
        if doo and post and ptype and nume:
            api = doo.group(1).rstrip("/") + f"/{post.group(1)}/{ptype.group(1)}/{nume.group(1)}"
            step(f"dooplay api: {api}")
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                    pr = await client.get(api, headers={**headers, "Referer": final_url})
                    pr.raise_for_status()
                    pdata = pr.json()
                embed = (pdata or {}).get("embed_url") or ""
                step(f"dooplay embed: {embed or 'none'}")
                if embed:
                    # Follow embed and look for m3u8/mp4
                    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                        er = await client.get(embed, headers={**headers, "Referer": final_url})
                        er.raise_for_status()
                        etext = er.text
                    found = re.findall(r'https?://[^\s"\'<>]+\.(?:m3u8|mp4)[^\s"\'<>]*', etext, re.I)
                    step(f"embed scan found {len(found)} media urls")
                    if found:
                        variants = []
                        for u in found[:8]:
                            variants.append({
                                "url": u,
                                "resolution": "embed",
                                "bandwidth": 0,
                                "codecs": "",
                                "name": "embed",
                                "average_bandwidth": 0,
                            })
                        return {"input": url, "type": "embed", "variants": variants, "error": None, "log": result["log"]}
                    # No direct media found. Do not offer the embed page as a downloadable stream.
                    step("embed page has no direct m3u8/mp4 (stream is likely loaded by encrypted JS)")
                    return {
                        "input": url,
                        "type": "embed",
                        "variants": [],
                        "error": "Found the player embed, but the stream URL is encrypted/loaded by JavaScript. Direct download is not available for this page.",
                        "log": result["log"],
                    }
            except Exception as exc:
                step(f"dooplay follow failed: {exc}")

    if not m3u8_urls:
        result["type"] = "page"
        result["error"] = "No .m3u8 playlists found on the page. Paste an M3U8 URL directly."
        step(result["error"])
        return result

    for murl in m3u8_urls[:3]:
        try:
            step(f"fetching candidate {murl}")
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                mr = await client.get(murl, headers=headers)
                mr.raise_for_status()
                parsed = _parse_m3u8(mr.text, str(mr.url))
                step(f"candidate parsed type={parsed['type']} variants={len(parsed['variants'])}")
                if parsed["variants"]:
                    parsed["type"] = "master" if parsed["type"] == "master" else "media"
                    parsed["log"] = result["log"]
                    return parsed
        except Exception as exc:
            step(f"candidate failed: {exc}")
            continue

    result["type"] = "page"
    result["error"] = "Found .m3u8 links but could not parse variants."
    step(result["error"])
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
    """Blocking FFmpeg download. Updates the downloads row as it goes."""
    from .downloader import is_cancelled, DownloadCancelled
    import shutil

    if not shutil.which("ffmpeg"):
        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='failed', error=? WHERE id=?",
                ("ffmpeg not found in container. Install ffmpeg to download streams.", job_id),
            )
        return

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
        import shutil
        shutil.move(str(staging), str(final))

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


async def capture_with_browser(url: str, timeout: int = 15) -> dict:
    """Load a page in a headless browser and capture m3u8/mp4 requests.

    Requires playwright and a Chromium install.
    """
    result = {"input": url, "type": "browser", "variants": [], "error": None, "log": []}

    def step(msg: str) -> None:
        result["log"].append(msg)
        _log(msg)

    step(f"browser capture start: {url}")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        result["error"] = "playwright not installed. Add it to the image and run playwright install chromium."
        step(result["error"])
        return result

    found = []
    seen_urls = []

    async def on_request(req):
        u = req.url
        if len(seen_urls) < 30 and not any(x in u for x in [".js", ".css", ".png", ".jpg", ".woff", "google", "analytics"]):
            seen_urls.append(u.split("?")[0][:120])
        if ".m3u8" in u.lower() or ".mp4" in u.lower():
            if u not in found:
                found.append(u)
                step(f"captured media {u}")
        elif "neocdn" in u.lower() and u.endswith(".html"):
            pass  # subtitle tracks, skip
        elif "/video/" in u.lower() and "kisscloud" in u.lower():
            if u not in found:
                found.append(u)
                step(f"captured embed {u}")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            page.on("request", on_request)
            # Also watch frames as they appear
            page.on("frameattached", lambda frame: frame.page.on("request", on_request) if frame.page else None)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            step("page loaded, looking for play controls")
            for sel in [
                "#player-option-1",
                ".dooplay_player_option",
                ".play-btn",
                "button.play",
                "[class*='play']",
                "video",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click(timeout=2000)
                        step(f"clicked {sel}")
                        break
                except Exception:
                    pass
            await page.wait_for_timeout(6000)
            # Check for iframes and try to interact
            frames = page.frames
            step(f"frames: {len(frames)}")
            kisscloud = None
            for frame in frames:
                try:
                    if frame.url and frame.url != url and "about:" not in frame.url:
                        step(f"frame: {frame.url[:100]}")
                        if "kisscloud" in frame.url or "/video/" in frame.url:
                            kisscloud = frame.url
                except Exception:
                    pass
            # If we found an embed iframe, open it directly and watch longer
            if kisscloud:
                step(f"opening embed directly: {kisscloud[:80]}")
                page2 = await context.new_page()
                page2.on("request", on_request)
                try:
                    await page2.goto(kisscloud, wait_until="domcontentloaded", timeout=15000)
                    await page2.wait_for_timeout(8000)
                    # try clicking play inside the embed
                    for sel in ["video", ".jw-icon-play", "[class*='play']", "button"]:
                        try:
                            el = await page2.query_selector(sel)
                            if el:
                                await el.click(timeout=1500)
                                step(f"embed clicked {sel}")
                                break
                        except Exception:
                            pass
                    await page2.wait_for_timeout(6000)
                except Exception as exc:
                    step(f"embed open failed: {exc}")
                await page2.close()
            await page.wait_for_timeout(2000)
            if seen_urls:
                step("sample requests: " + " | ".join(seen_urls[:8]))
            await browser.close()
    except Exception as exc:
        result["error"] = str(exc)
        step(f"browser error: {exc}")
        return result

    for u in found[:10]:
        result["variants"].append({
            "url": u,
            "resolution": "captured",
            "bandwidth": 0,
            "codecs": "",
            "name": "browser",
            "average_bandwidth": 0,
        })
    if not found:
        result["error"] = "No m3u8/mp4 requests seen. The player may need a click or the stream is DRM."
        step(result["error"])
    return result
