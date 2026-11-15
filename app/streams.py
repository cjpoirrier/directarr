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
        effective_headers = dict(headers or {})
        # Kisscloud / similar hosts require a Referer
        if "kisscloud" in stream_url.lower() and "referer" not in {k.lower() for k in effective_headers}:
            effective_headers["Referer"] = "https://kisscloud.online/"
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
                    "Referer": effective_headers.get("Referer", "https://kisscloud.online/"),
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

        # For kisscloud-style playlists, download segments ourselves
        if "kisscloud" in stream_url.lower() and ".m3u8" in stream_url.lower():
            try:
                import httpx as _httpx
                from urllib.parse import urljoin
                probe_headers = {
                    "User-Agent": USER_AGENT,
                    "Referer": "https://kisscloud.online/",
                }
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
                                _log(f"subtitle {lang} raw uri={uri[:120]}")
                                candidates = [suri]
                                # common WebVTT locations
                                if not suri.endswith(".vtt"):
                                    candidates.append(suri + ".vtt")
                                if not suri.endswith(".html"):
                                    candidates.append(suri + ".html")
                                if "neocdn" in suri:
                                    candidates.append(suri.replace(".com/", ".com/r/") + ".vtt")
                                    candidates.append(suri.replace(".top/", ".top/r/") + ".vtt")
                                for cand in candidates:
                                    sr = client.get(cand, headers=probe_headers)
                                    is_vtt = b"WEBVTT" in sr.content[:30] or b"-->" in sr.content[:500]
                                    _log(f"  {sr.status_code} vtt={is_vtt} {cand[:80]}")
                                    if sr.status_code == 200 and is_vtt:
                                        sp = STAGING_DIR / f"{job_id}_{lang}.vtt"
                                        sp.write_bytes(sr.content)
                                        sub_files.append((lang, name, sp))
                                        _log(f"subtitle {lang} saved vtt")
                                        break
                                else:
                                    _log(f"subtitle {lang} no vtt found")
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
            # Inject into every page/frame created in this context
            await context.add_init_script("""
                window.__directarr_found = window.__directarr_found || [];
                function _check(u) {
                    if (!u) return;
                    const s = String(u);
                    if (s.includes('.m3u8') || s.includes('/cdn/hls/') || s.includes('master.txt') || s.includes('do=getVideo') || s.includes('.mp4')) {
                        if (!window.__directarr_found.includes(s)) window.__directarr_found.push(s);
                    }
                }
                const _fetch = window.fetch;
                window.fetch = function(input, init) {
                    try { _check(typeof input === 'string' ? input : input && input.url); } catch(e) {}
                    return _fetch.apply(this, arguments);
                };
                const _open = XMLHttpRequest.prototype.open;
                XMLHttpRequest.prototype.open = function(method, url) {
                    try { _check(url); } catch(e) {}
                    return _open.apply(this, arguments);
                };
            """)
            page = await context.new_page()
            page.on("request", on_request)
            # Also watch frames as they appear
            page.on("frameattached", lambda frame: frame.page.on("request", on_request) if frame.page else None)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            step("page loaded, looking for play controls")
            try:
                page_title = await page.title()
                if page_title:
                    result["title"] = page_title.strip()
                    step(f"page title: {page_title[:80]}")
            except Exception:
                pass
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
                    await page2.wait_for_timeout(5000)
                    # Try the getVideo API directly if we can extract the id
                    vid = kisscloud.rstrip("/").split("/")[-1]
                    if vid and len(vid) > 8:
                        api = f"https://kisscloud.online/player/index.php?data={vid}&do=getVideo"
                        step(f"trying getVideo POST: {api}")
                        try:
                            resp = await page2.evaluate(f"""async () => {{
                                const r = await fetch("{api}", {{
                                    method: "POST",
                                    credentials: "include",
                                    headers: {{
                                        "Content-Type": "application/x-www-form-urlencoded",
                                        "X-Requested-With": "XMLHttpRequest"
                                    }},
                                    body: "hash={vid}&r=" + encodeURIComponent(location.href)
                                }});
                                return await r.text();
                            }}""")
                            step(f"getVideo response length: {len(resp or '')}")
                            if resp:
                                step("getVideo snippet: " + resp[:300].replace("\n", " "))
                                import re as _re
                                # Try to parse as JSON
                                try:
                                    import json as _json
                                    data = _json.loads(resp)
                                    step(f"getVideo JSON keys: {list(data.keys())[:10]}")
                                    # Prefer the signed m3u8
                                    for key in ("securedLink", "videoSource"):
                                        val = data.get(key)
                                        if val and isinstance(val, str) and val.startswith("http"):
                                            if val not in found:
                                                found.append(val)
                                                step(f"stream {key}: {val[:120]}")
                                    if data.get("ck"):
                                        step("has ck key")
                                except Exception as exc:
                                    step(f"json parse failed: {exc}")
                                for m in _re.findall(r'https?://[^"\'\s<>]+', resp):
                                    if any(x in m.lower() for x in ["m3u8", "hls", ".mp4", "master"]):
                                        if m not in found:
                                            found.append(m)
                                            step(f"getVideo url {m}")
                        except Exception as exc:
                            step(f"getVideo fetch failed: {exc}")
                        except Exception as exc:
                            step(f"getVideo fetch failed: {exc}")
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
                    # Collect from this page too
                    try:
                        injected2 = await page2.evaluate("() => window.__directarr_found || []")
                        for u in injected2:
                            if u not in found:
                                found.append(u)
                                step(f"embed injected {u}")
                    except Exception:
                        pass
                except Exception as exc:
                    step(f"embed open failed: {exc}")
                await page2.close()
            await page.wait_for_timeout(2000)
            if seen_urls:
                step("sample requests: " + " | ".join(seen_urls[:8]))
            # Pull URLs collected by the injected script
            try:
                injected = await page.evaluate("() => window.__directarr_found || []")
                for u in injected:
                    if u not in found:
                        found.append(u)
                        step(f"injected found {u}")
            except Exception:
                pass
            await browser.close()
    except Exception as exc:
        result["error"] = str(exc)
        step(f"browser error: {exc}")
        return result

    # Prefer signed m3u8; drop embed pages and API endpoints from the list
    cleaned = []
    for u in found:
        lu = u.lower()
        if ".m3u8" in lu or "/cdn/hls/" in lu:
            if u not in cleaned:
                cleaned.append(u)
    if cleaned:
        found = cleaned
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
