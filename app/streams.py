"""HLS / M3U8 stream detection and FFmpeg-based download for Directarr.

Standalone: works without Sonarr. Detects master/variant playlists, lets the
user pick a quality, and downloads via FFmpeg into the existing queue.
"""

from __future__ import annotations

import logging
import json
import re
import subprocess
import uuid
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx
import os

from .config import DOWNLOAD_DIR, STAGING_DIR
from .db import connect
from .settings_store import effective_flaresolverr_url

log = logging.getLogger("directarr.streams")
USER_AGENT = "Directarr/0.2 StreamDetect"

def get_proxy(tags: str | None = None) -> str | None:
    """Return proxy URL. Tag match wins, then saved/env proxy."""
    if tags:
        try:
            from .main import resolve_proxy_for_tags
            match = resolve_proxy_for_tags(tags)
            if match:
                return match
        except Exception:
            pass
    env = os.environ.get("DIRECTARR_PROXY")
    if env:
        return env
    try:
        from .config import CONFIG_DIR
        p = CONFIG_DIR / "proxy.txt"
        if p.is_file():
            val = p.read_text().strip()
            return val or None
    except Exception:
        pass
    return None

def httpx_client(**kwargs):
    """httpx client that uses DIRECTARR_PROXY when set."""
    proxy = get_proxy()
    if proxy:
        kwargs.setdefault("proxy", proxy)
    return httpx.AsyncClient(**kwargs)

def httpx_sync_client(**kwargs):
    proxy = get_proxy()
    if proxy:
        kwargs.setdefault("proxy", proxy)
    return httpx.Client(**kwargs)


async def _fetch_text(
    url: str,
    headers: dict | None = None,
    timeout: int = 20,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[str, str, int]:
    """Fetch text directly or through an optional FlareSolverr-compatible API."""
    emit = log_fn or _log
    flaresolverr_url = effective_flaresolverr_url()
    if flaresolverr_url:
        emit(f"request via FlareSolverr: {url}")
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": max(1000, timeout * 1000),
        }
        if headers:
            payload["headers"] = headers
        try:
            async with httpx.AsyncClient(timeout=timeout + 15) as client:
                response = await client.post(flaresolverr_url, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            emit(f"FlareSolverr request failed: {exc}")
            raise
        if data.get("status") != "ok":
            message = data.get("message") or "URL fetch service returned an error"
            emit(f"FlareSolverr returned an error: {message}")
            raise RuntimeError(message)
        solution = data.get("solution") or {}
        status_code = int(solution.get("status") or 200)
        if status_code < 200 or status_code >= 400:
            raise RuntimeError(f"URL fetch target returned HTTP {status_code}")
        text = solution.get("response")
        if not isinstance(text, str):
            raise RuntimeError("URL fetch service returned no text response")
        emit(f"FlareSolverr completed: target_status={status_code} bytes={len(text)}")
        return text, solution.get("url") or url, status_code

    emit(f"request via direct HTTP: {url}")
    async with httpx_client(follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        emit(f"direct HTTP completed: status={response.status_code} bytes={len(response.text)}")
        return response.text, str(response.url), response.status_code

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

    def step(msg: str, level: str = "info") -> None:
        result["log"].append(msg)
        _log(msg, level)

    step(f"detect start: {url}")
    flaresolverr_url = effective_flaresolverr_url()
    step(
        f"fetch service: FlareSolverr ({flaresolverr_url})"
        if flaresolverr_url
        else "fetch service: direct HTTP"
    )

    try:
        text, final_url, status_code = await _fetch_text(
            url, headers=headers, timeout=60, log_fn=step
        )
        step(f"fetched {final_url} status={status_code} bytes={len(text)}")
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
                pr_text, _, _ = await _fetch_text(
                    api, headers={**headers, "Referer": final_url}, timeout=15, log_fn=step
                )
                pdata = json.loads(pr_text)
                embed = (pdata or {}).get("embed_url") or ""
                step(f"dooplay embed: {embed or 'none'}")
                if embed:
                    # Follow embed and look for m3u8/mp4
                    etext, _, _ = await _fetch_text(
                        embed, headers={**headers, "Referer": final_url}, timeout=15, log_fn=step
                    )
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
            mtext, media_url, _ = await _fetch_text(
                murl, headers=headers, timeout=15, log_fn=step
            )
            parsed = _parse_m3u8(mtext, media_url)
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
        user_agent = next(
            str(v) for k, v in effective_headers.items() if k.lower() == "user-agent"
        )
        referer = next(
            (str(v) for k, v in effective_headers.items() if k.lower() == "referer"),
            "https://kisscloud.online/",
        )
        for k, v in effective_headers.items():
            # Avoid CR/LF injection in FFmpeg's header argument.
            if (
                k.lower() != "user-agent"
                and "\r" not in k
                and "\n" not in k
                and "\r" not in str(v)
                and "\n" not in str(v)
            ):
                hdr_lines.append(f"{k}: {v}")
        header_arg = "\r\n".join(hdr_lines) + "\r\n" if hdr_lines else ""

        # Probe the playlist so we can see what FFmpeg will hit
        probe_note = ""
        if ".m3u8" in stream_url.lower():
            try:
                import httpx as _httpx
                probe_headers = {
                    k: v
                    for k, v in effective_headers.items()
                    if k.lower() not in {"user-agent", "referer"}
                }
                probe_headers.update({"User-Agent": user_agent, "Referer": referer})
                with httpx_sync_client(follow_redirects=True, timeout=15) as client:
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
                    k: v
                    for k, v in effective_headers.items()
                    if k.lower() not in {"user-agent", "referer"}
                }
                probe_headers.update({"User-Agent": user_agent, "Referer": referer})
                with httpx_sync_client(follow_redirects=True, timeout=30) as client:
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
            "-user_agent", user_agent,
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

async def _flaresolverr_cookies(
    url: str,
    timeout: int,
    log_fn: Callable[[str], None],
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Use FlareSolverr to solve the page and return cookies/user-agent/final URL."""
    flaresolverr_url = effective_flaresolverr_url()
    if not flaresolverr_url:
        log_fn("FlareSolverr is not configured; continuing without it")
        return [], None, None

    log_fn("requesting page via FlareSolverr")
    async with httpx.AsyncClient(timeout=timeout + 15) as client:
        response = await client.post(
            flaresolverr_url,
            json={
                "cmd": "request.get",
                "url": url,
                "maxTimeout": max(1000, timeout * 1000),
            },
        )
        response.raise_for_status()
        data = response.json()

    if data.get("status") != "ok":
        raise RuntimeError(data.get("message") or "FlareSolverr request failed")

    solution = data.get("solution") or {}
    cookies = []

    for cookie in solution.get("cookies") or []:
        item = {
            "name": cookie.get("name"),
            "value": cookie.get("value"),
            "url": cookie.get("url") or url,
        }
        # Playwright requires a valid name/value and either url or domain/path.
        if item["name"] and item["value"] is not None:
            cookies.append(item)

    response_headers = solution.get("headers") or {}
    user_agent = next(
        (
            str(value)
            for key, value in response_headers.items()
            if key.lower() == "user-agent"
        ),
        None,
    )
    final_url = solution.get("url") or url

    log_fn(f"FlareSolverr completed; received {len(cookies)} cookies")
    return cookies, user_agent, final_url
      
async def capture_with_browser(url: str, timeout: int = 15) -> dict:
    """Load a page in a headless browser and capture m3u8/mp4 requests."""
    result = {"input": url, "type": "browser", "variants": [], "error": None, "log": []}

    def step(msg: str, level: str = "info") -> None:
        result["log"].append(msg)
        _log(msg, level)

    step(f"browser capture start: {url}")
    proxy = get_proxy()
    step(f"proxy: {proxy.split('@')[-1] if proxy else 'none'}")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        result["error"] = (
            "playwright not installed. Add it to the image and run "
            "playwright install chromium."
        )
        step(result["error"])
        return result

    try:
        fs_cookies, fs_user_agent, fs_final_url = await _flaresolverr_cookies(
            url, timeout, step
        )
    except Exception as exc:
        result["error"] = f"FlareSolverr failed: {exc}"
        step(result["error"], "error")
        return result

    found: list[str] = []
    captured_headers: dict[str, dict[str, str]] = {}
    seen_urls: list[str] = []

    async def on_request(req) -> None:
        request_url = req.url
        lower = request_url.lower()

        if len(seen_urls) < 30 and not any(
            marker in lower
            for marker in (
                ".js", ".css", ".png", ".jpg", ".woff", "google", "analytics"
            )
        ):
            seen_urls.append(request_url.split("?")[0][:120])

        is_media = ".m3u8" in lower or ".mp4" in lower
        is_embed = "/video/" in lower and "kisscloud" in lower

        if is_media or is_embed:
            if request_url not in found:
                found.append(request_url)
            try:
                captured_headers[request_url] = await req.all_headers()
            except Exception as exc:
                captured_headers.setdefault(request_url, {})
                step(f"could not capture request headers: {exc}")
            step(f"captured {'media' if is_media else 'embed'} {request_url}")

    async def on_document_response(response) -> None:
        if response.request.is_navigation_request():
            step(
                f"document response: status={response.status} "
                f"url={response.url[:200]}",
                "warning" if response.status >= 400 else "info",
            )

    async def on_document_request_failed(request) -> None:
        if request.is_navigation_request():
            step(
                f"document request failed: {request.failure or 'unknown failure'} "
                f"url={request.url[:200]}",
                "warning",
            )

    def on_page_error(error) -> None:
        step(f"page JavaScript error: {str(error)[:400]}", "error")

    browser = None
    try:
        async with async_playwright() as p:
            launch_options: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            }
            if proxy:
                launch_options["proxy"] = {"server": proxy}

            browser = await p.chromium.launch(**launch_options)
            context = await browser.new_context(
                user_agent=(
                    fs_user_agent
                    or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
                locale="en-US",
            )

            if fs_cookies:
                await context.add_cookies(fs_cookies)
                step(f"installed {len(fs_cookies)} FlareSolverr cookies")

            page = await context.new_page()

            try:
                from playwright_stealth import stealth_async
                await stealth_async(page)
                step("stealth applied")
            except ImportError:
                step("playwright-stealth unavailable")
            except Exception as exc:
                step(f"stealth not applied: {exc}")

            page.on("request", on_request)
            page.on("response", on_document_response)
            page.on("requestfailed", on_document_request_failed)
            page.on("pageerror", on_page_error)

            navigation_url = fs_final_url or url
            navigation_started = time.monotonic()
            step(f"navigation started: {navigation_url}")

            try:
                response = await page.goto(
                    navigation_url,
                    wait_until="domcontentloaded",
                    timeout=timeout * 1000,
                )
            except Exception as exc:
                elapsed = time.monotonic() - navigation_started
                step(
                    f"navigation failed after {elapsed:.2f}s: "
                    f"{type(exc).__name__}: {exc}",
                    "error",
                )
                raise

            elapsed = time.monotonic() - navigation_started
            if response is None:
                step(f"navigation finished without response after {elapsed:.2f}s")
            else:
                step(
                    f"main document status={response.status} "
                    f"url={response.url[:200]} elapsed={elapsed:.2f}s",
                    "warning" if response.status >= 400 else "info",
                )

            try:
                page_title = await page.title()
                if page_title:
                    result["title"] = page_title.strip()
                    step(f"page title: {page_title[:80]}")
            except Exception:
                pass

            for selector in (
                "#player-option-1",
                ".dooplay_player_option",
                ".play-btn",
                "button.play",
                "[class*='play']",
                "video",
            ):
                try:
                    element = await page.query_selector(selector)
                    if element:
                        await element.click(timeout=2000)
                        step(f"clicked {selector}")
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(6000)

            kisscloud_url = None
            for frame in page.frames:
                try:
                    if frame.url and frame.url != navigation_url and "about:" not in frame.url:
                        step(f"frame: {frame.url[:100]}")
                        if any(
                            marker in frame.url
                            for marker in ("kisscloud", "/video/", "vhscdn", "embed")
                        ):
                            kisscloud_url = frame.url
                except Exception:
                    pass

            if kisscloud_url:
                step(f"opening embed directly: {kisscloud_url[:80]}")
                page2 = await context.new_page()
                page2.on("request", on_request)

                try:
                    await page2.goto(
                        kisscloud_url,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    await page2.wait_for_timeout(5000)

                    video_id = kisscloud_url.rstrip("/").split("/")[-1]
                    if video_id and len(video_id) > 8:
                        api = (
                            "https://kisscloud.online/player/index.php"
                            f"?data={video_id}&do=getVideo"
                        )
                        step(f"trying getVideo POST: {api}")

                        try:
                            response_text = await page2.evaluate(
                                """async (videoId) => {
                                    const api =
                                      "https://kisscloud.online/player/index.php?data="
                                      + encodeURIComponent(videoId) + "&do=getVideo";
                                    const r = await fetch(api, {
                                      method: "POST",
                                      credentials: "include",
                                      headers: {
                                        "Content-Type":
                                          "application/x-www-form-urlencoded",
                                        "X-Requested-With": "XMLHttpRequest"
                                      },
                                      body: "hash=" + encodeURIComponent(videoId)
                                        + "&r=" + encodeURIComponent(location.href)
                                    });
                                    return await r.text();
                                }""",
                                video_id,
                            )

                            if response_text:
                                try:
                                    data = json.loads(response_text)
                                    for key in ("securedLink", "videoSource"):
                                        media_url = data.get(key)
                                        if (
                                            isinstance(media_url, str)
                                            and media_url.startswith("http")
                                            and media_url not in found
                                        ):
                                            found.append(media_url)
                                            captured_headers.setdefault(media_url, {})
                                            step(f"stream {key}: {media_url[:120]}")
                                except Exception:
                                    pass

                                for media_url in re.findall(
                                    r"""https?://[^"'\s<>]+""", response_text
                                ):
                                    if any(
                                        marker in media_url.lower()
                                        for marker in ("m3u8", "hls", ".mp4", "master")
                                    ) and media_url not in found:
                                        found.append(media_url)
                                        captured_headers.setdefault(media_url, {})
                                        step(f"getVideo URL: {media_url}")
                        except Exception as exc:
                            step(f"getVideo request failed: {exc}")

                    for selector in (
                        "video",
                        ".jw-icon-play",
                        "[class*='play']",
                        "button",
                    ):
                        try:
                            element = await page2.query_selector(selector)
                            if element:
                                await element.click(timeout=1500)
                                step(f"embed clicked {selector}")
                                break
                        except Exception:
                            pass

                    await page2.wait_for_timeout(6000)
                except Exception as exc:
                    step(f"embed open failed: {exc}")
                finally:
                    await page2.close()

            await page.wait_for_timeout(2000)

            if seen_urls:
                step("sample requests: " + " | ".join(seen_urls[:8]))

            try:
                injected_urls = await page.evaluate(
                    "() => window.__directarr_found || []"
                )
                for media_url in injected_urls:
                    if media_url not in found:
                        found.append(media_url)
                        captured_headers.setdefault(media_url, {})
                        step(f"injected URL: {media_url}")
            except Exception:
                pass

    except Exception as exc:
        result["error"] = str(exc)
        step(f"browser error: {exc}", "error")
        return result
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

    cleaned = [
        media_url
        for media_url in found
        if ".m3u8" in media_url.lower() or ".mp4" in media_url.lower()
        or "/cdn/hls/" in media_url.lower()
    ]
    if cleaned:
        found = cleaned

    for media_url in found[:10]:
        result["variants"].append({
            "url": media_url,
            "resolution": "captured",
            "bandwidth": 0,
            "codecs": "",
            "name": "browser",
            "average_bandwidth": 0,
            "headers": captured_headers.get(media_url, {}),
        })

    if not found:
        result["error"] = (
            "No m3u8/mp4 requests seen. The player may need a click or the stream is DRM."
        )
        step(result["error"])

    return result