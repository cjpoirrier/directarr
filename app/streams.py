"""HLS / M3U8 stream detection and FFmpeg-based download for Directarr.

Standalone: works without Sonarr. Detects master/variant playlists, lets the
user pick a quality, and downloads via FFmpeg into the existing queue.
"""

from __future__ import annotations

import asyncio
import logging
import os
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

# FlareSolverr (configurable via env for Docker) + optional Selenium fallback
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://localhost:8191/v1")

def _looks_like_cloudflare(text: str, status: int) -> bool:
    if status in (403, 503):
        return True
    low = (text or "").lower()
    markers = ("just a moment", "checking your browser", "cf-browser-verification", "challenge-platform")
    return any(m in low for m in markers)

async def _call_flaresolverr(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(FLARESOLVERR_URL, json=payload)
        r.raise_for_status()
        return r.json()

async def _fetch_via_flaresolverr(url: str, session_id: str | None = None, post_data: str | None = None) -> tuple[str, str, list]:
    cmd = "request.post" if post_data else "request.get"
    payload = {"cmd": cmd, "url": url, "maxTimeout": 120000}
    if session_id:
        payload["session"] = session_id
    if post_data:
        payload["postData"] = post_data
    data = await _call_flaresolverr(payload)
    sol = data.get("solution") or {}
    return sol.get("response") or "", sol.get("url") or url, sol.get("cookies") or []

async def create_flaresolverr_session() -> str | None:
    try:
        data = await _call_flaresolverr({"cmd": "sessions.create"})
        return data.get("session")
    except Exception:
        return None

async def destroy_flaresolverr_session(session_id: str) -> None:
    try:
        await _call_flaresolverr({"cmd": "sessions.destroy", "session": session_id})
    except Exception:
        pass

def _selenium_bypass_sync(url: str) -> tuple[str, str]:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium_captcha_processing import BypassCaptcha
    except ImportError as e:
        raise RuntimeError(f"selenium-captcha-processing not available: {e}")
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(service=Service(), options=options)
    try:
        driver.get(url)
        BypassCaptcha(driver).bypass()
        return driver.page_source, driver.current_url
    finally:
        driver.quit()

async def _try_selenium_captcha(url: str) -> tuple[str, str] | None:
    try:
        return await asyncio.to_thread(_selenium_bypass_sync, url)
    except Exception as exc:
        _log(f"selenium captcha bypass failed: {exc}", "warning")
        return None

async def make_persistent_client(headers: dict | None = None, cookies: list[dict] | None = None) -> httpx.AsyncClient:
    """Reusable client that keeps cookies and connections open."""
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    client = httpx.AsyncClient(follow_redirects=True, timeout=30, headers=hdrs)
    for c in cookies or []:
        name = c.get("name")
        if name:
            client.cookies.set(name, c.get("value", ""), domain=c.get("domain"), path=c.get("path", "/"))
    return client


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
    """Clean a page title into a filesystem-safe episode name.

    Drops site names, 'English Subbed', and the job id.
    """
    t = (title or "episode").strip()
    # Drop common site suffixes
    t = re.sub(r"\s*[|_-]\s*(MyAsiantv|AniWave|KissAnime|Gogoanime).*$", "", t, flags=re.I)
    t = re.sub(r"\s*English Subbed\s*", " ", t, flags=re.I)
    t = re.sub(r"\s*\(\d{4}\)\s*", " ", t)
    t = re.sub(r"[^\w\s\-_.]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return (t or "episode")[:120]


async def detect_streams(
    url: str,
    headers: dict | None = None,
    flaresolverr_session: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch a page or M3U8 and return detected variants.

    The returned dict always includes a "log" list of steps for the UI.
    Pass a persistent client to keep cookies/connections open.
    Falls back to FlareSolverr on Cloudflare, then optional Selenium captcha.
    """
    own_client = client is None
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    result: dict[str, Any] = {
        "input": url,
        "type": "unknown",
        "variants": [],
        "error": None,
        "log": [],
        "cookies": [],
    }

    def step(msg: str) -> None:
        result["log"].append(msg)
        _log(msg)

    step(f"detect start: {url}")

    text = ""
    final_url = url
    fs_cookies: list = []

    try:
        if client is None:
            client = httpx.AsyncClient(follow_redirects=True, timeout=20, headers=headers)
        r = await client.get(url)
        text = r.text
        final_url = str(r.url)
        status = r.status_code
        step(f"fetched {final_url} status={status} bytes={len(text)}")
        if _looks_like_cloudflare(text, status):
            step("Cloudflare indicators detected, trying FlareSolverr")
            try:
                text, final_url, fs_cookies = await _fetch_via_flaresolverr(url, session_id=flaresolverr_session)
                result["cookies"] = fs_cookies
                for c in fs_cookies:
                    if c.get("name"):
                        client.cookies.set(c["name"], c.get("value", ""), domain=c.get("domain"), path=c.get("path", "/"))
                step(f"FlareSolverr ok, bytes={len(text)} cookies={len(fs_cookies)}")
            except Exception as fs_exc:
                step(f"FlareSolverr failed: {fs_exc}")
                sel = await _try_selenium_captcha(url)
                if sel:
                    text, final_url = sel
                    step("selenium-captcha-processing succeeded")
                else:
                    result["error"] = f"blocked and fallbacks failed: {fs_exc}"
                    if own_client:
                        await client.aclose()
                    return result
    except Exception as exc:
        step(f"fetch failed: {exc}")
        try:
            step("trying FlareSolverr after exception")
            text, final_url, fs_cookies = await _fetch_via_flaresolverr(url, session_id=flaresolverr_session)
            result["cookies"] = fs_cookies
            if client is None:
                client = await make_persistent_client(headers, fs_cookies)
            else:
                for c in fs_cookies:
                    if c.get("name"):
                        client.cookies.set(c["name"], c.get("value", ""), domain=c.get("domain"), path=c.get("path", "/"))
            step(f"FlareSolverr recovered, bytes={len(text)}")
        except Exception as fs_exc:
            step(f"FlareSolverr also failed: {fs_exc}")
            sel = await _try_selenium_captcha(url)
            if sel:
                text, final_url = sel
                step("selenium-captcha-processing recovered")
            else:
                result["error"] = str(exc)
                if own_client and client:
                    await client.aclose()
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
        effective_headers = dict(headers or {})
        # Generic Referer: page URL if we have it, else stream origin
        if "referer" not in {k.lower() for k in effective_headers}:
            page = (headers or {}).get("Referer") or (headers or {}).get("referer")
            if page:
                effective_headers["Referer"] = page
            else:
                from urllib.parse import urlparse as _up
                p = _up(stream_url)
                if p.scheme and p.netloc:
                    origin = f"{p.scheme}://{p.netloc}/"
                    effective_headers["Referer"] = origin
                    effective_headers.setdefault("Origin", origin.rstrip("/"))
        if "user-agent" not in {k.lower() for k in effective_headers}:
            effective_headers["User-Agent"] = USER_AGENT
        for k, v in effective_headers.items():
            if k.lower() in ("user-agent", "referer", "cookie", "origin"):
                hdr_lines.append(f"{k}: {v}")
        header_arg = "\r\n".join(hdr_lines) + "\r\n" if hdr_lines else ""

        # Probe the playlist so we can see what FFmpeg will hit
        probe_note = ""
        if ".m3u8" in stream_url.lower():
            try:
                import httpx as _httpx
                probe_headers = {
                    "User-Agent": USER_AGENT,
                    "Referer": effective_headers.get("Referer", ""),
                }
                with _httpx.Client(follow_redirects=True, timeout=15) as client:
                    pr = client.get(stream_url, headers=probe_headers)
                    body = pr.text
                    import re as _re
                    stream_infs = _re.findall(
                        r'#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)', body
                    )
                    video_urls = []
                    from urllib.parse import urljoin
                    for _, u in stream_infs:
                        u = u.strip().strip('"')
                        if u:
                            full = u if u.startswith("http") else urljoin(stream_url, u)
                            video_urls.append(full)
                    probe_note = (
                        f"playlist {pr.status_code} len={len(body)} "
                        f"video={video_urls[:2]}"
                    )
                    _log(probe_note)
                    if video_urls:
                        try:
                            vr = client.get(video_urls[0], headers=probe_headers)
                            vbody = vr.text[:250].replace("\n", " | ")
                            probe_note += f" | variant0 {vr.status_code} len={len(vr.text)}: {vbody}"
                            _log(f"variant0: {vr.status_code} {vbody[:180]}")
                        except Exception as exc:
                            probe_note += f" | variant0 failed: {exc}"
                            _log(f"variant0 failed: {exc}")
                        # Test one segment URL from the variant playlist
                        try:
                            import re as _re2
                            seg = _re2.search(r'(https?://[^\s]+\.html)', vr.text if 'vr' in dir() else "")
                            if seg:
                                sr = client.get(seg.group(1), headers=probe_headers)
                                ctype = sr.headers.get("content-type", "")
                                start = sr.text[:150].replace("\n", " ")
                                probe_note += f" | seg {sr.status_code} type={ctype} len={len(sr.content)} start={start}"
                                _log(f"segment: {sr.status_code} {ctype} {len(sr.content)} bytes start={start}")
                        except Exception as exc:
                            _log(f"segment probe failed: {exc}")
            except Exception as exc:
                probe_note = f"playlist probe failed: {exc}"

        # Download segments ourselves for any m3u8 (avoids FFmpeg 403s)
        if ".m3u8" in stream_url.lower():
            try:
                import httpx as _httpx
                from urllib.parse import urljoin
                probe_headers = {
                    "User-Agent": USER_AGENT,
                    "Referer": effective_headers.get("Referer", ""),
                    "Origin": effective_headers.get("Origin", ""),
                }
                probe_headers = {k: v for k, v in probe_headers.items() if v}
                _log(f"download headers referer={probe_headers.get('Referer','')[:60]}")
                with _httpx.Client(follow_redirects=True, timeout=30) as client:
                    master = client.get(stream_url, headers=probe_headers).text
                    import re as _re
                    # Subtitle tracks
                    sub_tracks = _re.findall(
                        r'#EXT-X-MEDIA:TYPE=SUBTITLES[^\n]*LANGUAGE="([^"]+)"[^\n]*NAME="([^"]+)"[^\n]*URI="([^"]+)"',
                        master,
                    )
                    _log(f"subtitles found: {len(sub_tracks)}")
                    # get highest-bandwidth variant
                    stream_infs = _re.findall(
                        r'#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)', master
                    )
                    best = None
                    best_bw = -1
                    for info, url in stream_infs:
                        bw = 0
                        m_bw = _re.search(r'BANDWIDTH=(\d+)', info)
                        if m_bw:
                            bw = int(m_bw.group(1))
                        if bw > best_bw:
                            best_bw = bw
                            best = url.strip()
                    m = best
                    if m:
                        variant_url = m
                        if not variant_url.startswith("http"):
                            variant_url = urljoin(stream_url, variant_url)
                        _log(f"using variant bw={best_bw} {variant_url[:80]}")
                        variant = client.get(variant_url, headers=probe_headers).text
                        segs = _re.findall(r'(https?://[^\s]+)', variant)
                        _log(f"manual: {len(segs)} segments (full)")
                        limit = len(segs)
                        seg_dir = STAGING_DIR / f"{job_id}_segs"
                        seg_dir.mkdir(exist_ok=True)
                        paths = []
                        for i, su in enumerate(segs):
                            if is_cancelled(job_id):
                                raise DownloadCancelled("Cancelled")
                            r = client.get(su, headers=probe_headers)
                            p = seg_dir / f"{i:05d}.ts"
                            p.write_bytes(r.content)
                            paths.append(p)
                            if i % 20 == 0 or i == limit - 1:
                                with connect() as db:
                                    db.execute(
                                        "UPDATE downloads SET progress=? WHERE id=?",
                                        (int((i + 1) / limit * 90), job_id),
                                    )
                        # Binary-concat the TS segments, then remux
                        raw_ts = STAGING_DIR / f"{job_id}.raw.ts"
                        with raw_ts.open("wb") as out_f:
                            for p in paths:
                                out_f.write(p.read_bytes())
                        # Download subtitle tracks (best-effort)
                        sub_files = []
                        for lang, name, uri in sub_tracks[:3]:
                            try:
                                suri = uri if uri.startswith("http") else urljoin(stream_url, uri)
                                _log(f"subtitle {lang} raw={suri[:100]}")
                                # Build /r/<hash>.html style URLs
                                candidates = []
                                if "neocdn" in suri:
                                    base = suri.split("/r/")[-1] if "/r/" in suri else suri.split("/")[-1]
                                    host = suri.split("/")[2]
                                    candidates.append(f"https://{host}/r/{base}.html")
                                    candidates.append(f"https://{host}/r/{base}")
                                    candidates.append(f"https://{host}/{base}.html")
                                candidates.append(suri)
                                for cand in candidates:
                                    sr = client.get(cand, headers=probe_headers)
                                    is_vtt = b"WEBVTT" in sr.content[:40] or (b"-->" in sr.content[:800] and b"<html" not in sr.content[:200].lower())
                                    _log(f"  {sr.status_code} vtt={is_vtt} len={len(sr.content)} {cand[:70]}")
                                    if sr.status_code == 200 and is_vtt and len(sr.content) > 100:
                                        sp = STAGING_DIR / f"{job_id}_{lang}.vtt"
                                        sp.write_bytes(sr.content)
                                        sub_files.append((lang, name, sp))
                                        _log(f"subtitle {lang} saved")
                                        break
                                else:
                                    _log(f"subtitle {lang} no vtt")
                            except Exception as exc:
                                _log(f"subtitle {lang} failed: {exc}")
                        out = STAGING_DIR / f"{job_id}.part.mkv"
                        cmd = [
                            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "mpegts", "-i", str(raw_ts),
                        ]
                        for lang, name, sp in sub_files:
                            cmd += ["-i", str(sp)]
                        cmd += ["-map", "0:v", "-map", "0:a?"]
                        for i in range(len(sub_files)):
                            cmd += ["-map", str(i + 1)]
                        cmd += ["-c", "copy", "-c:s", "srt", "-bsf:a", "aac_adtstoasc"]
                        for i, (lang, name, _) in enumerate(sub_files):
                            cmd += [f"-metadata:s:s:{i}", f"language={lang}", f"-metadata:s:s:{i}", f"title={name}"]
                        cmd += [str(out)]
                        result = subprocess.run(
                            cmd, capture_output=True, text=True, timeout=600,
                        )
                        if result.returncode != 0:
                            raise RuntimeError(f"remux failed: {result.stderr[-400:]}")
                        nice = _safe_name(title if title and title != "captured" else "episode", job_id)
                        final = DOWNLOAD_DIR / (nice + ".mkv")
                        import shutil
                        shutil.move(str(out), str(final))
                        with connect() as db:
                            db.execute(
                                "UPDATE downloads SET status='completed', progress=100, path=? WHERE id=?",
                                (str(final), job_id),
                            )
                        _log(f"manual download complete: {final} ({final.stat().st_size} bytes)")
                        # Probe the result
                        try:
                            pr = subprocess.run(
                                ["ffprobe", "-hide_banner", "-show_format", "-show_streams", str(final)],
                                capture_output=True, text=True, timeout=30,
                            )
                            _log(f"ffprobe: {pr.stdout[:500] or pr.stderr[:300]}")
                        except Exception as exc:
                            _log(f"ffprobe failed: {exc}")
                        # cleanup
                        try:
                            raw_ts.unlink()
                            for p in paths:
                                p.unlink()
                            seg_dir.rmdir()
                        except Exception:
                            pass
                        return
            except Exception as exc:
                _log(f"manual download failed: {exc}", "error")
                # fall through to normal ffmpeg

        cmd = [
            "ffmpeg", "-y",
            "-hide_banner", "-loglevel", "error",
            "-user_agent", USER_AGENT,
            "-allowed_extensions", "ALL",
            "-extension_picky", "0",
            "-f", "hls",
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
        # Poll for cancel and timeout
        import time
        start = time.time()
        while proc.poll() is None:
            if is_cancelled(job_id):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                raise DownloadCancelled("Cancelled")
            if time.time() - start > 120:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                raise RuntimeError("FFmpeg timed out after 120s")
            # rough progress tick
            elapsed = int(time.time() - start)
            with connect() as db:
                db.execute(
                    "UPDATE downloads SET progress=? WHERE id=? AND status='downloading'",
                    (min(95, elapsed), job_id),
                )
            time.sleep(2)

        if proc.returncode != 0:
            err = (proc.stderr.read() or b"").decode(errors="replace")[-300:]
            msg = f"FFmpeg failed: {err or 'unknown error'}"
            if probe_note:
                msg = probe_note + " || " + msg
            _log(msg, "error")
            raise RuntimeError(msg)

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


async def capture_with_browser(url: str, timeout: int = 15, flaresolverr_session: str | None = None, cookies: list | None = None) -> dict:
    """Robust nodriver capture with multiple strategies for Aniwave-style sites."""
    result = {"input": url, "type": "browser", "variants": [], "error": None, "log": [], "cookies": []}

    def step(msg: str) -> None:
        result["log"].append(msg)
        _log(msg)

    step(f"browser capture start (nodriver robust): {url}")

    try:
        import nodriver as uc
    except ImportError:
        result["error"] = "nodriver not installed"
        step(result["error"])
        return result

    browser = None
    try:
        browser = await uc.start(
            headless=True,
            browser_args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        step("nodriver started")
        tab = await browser.get(url)
        await tab.sleep(4)
        title = await tab.evaluate("document.title") or ""
        step(f"title: {title[:70]}")

        found = []
        def on_req(event):
            try:
                req = getattr(event, "request", None)
                u = req.url if req else ""
                if u and any(k in u.lower() for k in ("m3u8", "getsources", "/ajax/sources", "vhscdn.to/embed")):
                    if u not in found:
                        found.append(u)
                        step(f"net: {u[:130]}")
            except Exception:
                pass
        await tab.send(uc.cdp.network.enable())
        tab.add_handler(uc.cdp.network.RequestWillBeSent, on_req)

        for sel in ["[class*='play']", "video", "button"]:
            try:
                el = await tab.select(sel, timeout=2)
                if el:
                    await el.click()
                    step(f"clicked {sel}")
                    break
            except Exception:
                continue

        for name in ["DatSaV", "Vidplay", "BYFMS", "MyCloud"]:
            try:
                el = await tab.find(name, best_match=True, timeout=2)
                if el:
                    await el.click()
                    step(f"clicked server {name}")
                    await tab.sleep(4)
                    break
            except Exception:
                continue

        await tab.sleep(8)

        ajax_urls = [u for u in found if "/ajax/sources" in u]
        embed_url = None
        if ajax_urls:
            try:
                step("opening ajax/sources directly")
                await tab.get(ajax_urls[0])
                await tab.sleep(2)
                body = await tab.evaluate("document.body.innerText") or ""
                step(f"ajax text: {body[:300]}")
                import json as _json
                try:
                    data = _json.loads(body)
                    embed_url = (data.get("result") or {}).get("url") or data.get("url")
                    if embed_url:
                        step(f"embed from ajax: {embed_url[:100]}")
                        found.append(embed_url)
                except Exception:
                    import re as _re
                    for m in _re.findall(r"https?://[^\s\"']+", body):
                        if "vhscdn" in m or "embed" in m:
                            embed_url = m
                            found.append(m)
                            step(f"embed regex: {m[:100]}")
                            break
            except Exception as exc:
                step(f"ajax direct failed: {exc}")

        # Follow the embed to get the real source
        if embed_url:
            try:
                step(f"opening embed: {embed_url[:80]}")
                # Clear found so we only see new requests from the embed
                pre_len = len(found)
                await tab.get(embed_url)
                await tab.sleep(8)
                # Check what new network requests appeared
                new_reqs = found[pre_len:]
                step(f"new requests after embed: {len(new_reqs)}")
                for nr in new_reqs[:5]:
                    step(f"  embed-net: {nr[:130]}")

                html = await tab.evaluate("document.documentElement.outerHTML") or ""
                step(f"embed html length: {len(html)}")
                import re as _re
                for m in _re.findall(r"https?://[^\s\"']*(?:getSources|m3u8|file)[^\s\"']*", html, _re.I):
                    if m not in found:
                        found.append(m)
                        step(f"from embed html: {m[:120]}")

                # Look for source patterns in scripts
                sources_js = await tab.evaluate("""
                    () => {
                        const out = [];
                        document.querySelectorAll('script').forEach(s => {
                            const t = s.textContent || '';
                            if (t.includes('sources') || t.includes('file:') || t.includes('m3u8') || t.includes('getSources')) {
                                out.push(t.slice(0, 800));
                            }
                        });
                        return out.slice(0, 2);
                    }
                """)
                if sources_js:
                    for sj in sources_js:
                        step(f"embed script: {sj[:180]}")
            except Exception as exc:
                step(f"embed follow failed: {exc}")

        step(f"total candidates: {len(found)}")

        for u in found:
            low = u.lower()
            if any(x in low for x in ("m3u8", ".mp4", ".ts")):
                result["variants"].append({
                    "url": u, "resolution": "captured", "bandwidth": 0,
                    "codecs": "", "name": "nodriver", "headers": {"Referer": url},
                })

        if not result["variants"]:
            for u in found:
                if "/ajax/" not in u and "vhscdn" in u:
                    result["variants"].append({
                        "url": u, "resolution": "embed", "bandwidth": 0,
                        "codecs": "", "name": "nodriver-embed", "headers": {"Referer": url},
                    })
                    break

        if not result["variants"]:
            result["error"] = "No usable media URL found"
            step(result["error"])

    except Exception as exc:
        step(f"capture failed: {exc}")
        result["error"] = str(exc)
    finally:
        if browser:
            try:
                await browser.stop()
            except Exception:
                pass

    return result
