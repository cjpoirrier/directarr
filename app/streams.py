"""HLS / M3U8 stream detection and FFmpeg-based download for Directarr.

Standalone: works without Sonarr. Detects master/variant playlists, lets the
user pick a quality, and downloads via FFmpeg into the existing queue.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx

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

        path = CONFIG_DIR / "proxy.txt"
        if path.is_file():
            value = path.read_text().strip()
            return value or None
    except Exception:
        pass

    return None


def httpx_client(**kwargs):
    """Create an async httpx client that uses DIRECTARR_PROXY when set."""
    proxy = get_proxy()
    if proxy:
        kwargs.setdefault("proxy", proxy)
    return httpx.AsyncClient(**kwargs)


def httpx_sync_client(**kwargs):
    """Create a sync httpx client that uses DIRECTARR_PROXY when set."""
    proxy = get_proxy()
    if proxy:
        kwargs.setdefault("proxy", proxy)
    return httpx.Client(**kwargs)


# Simple ring buffer for recent stream-related log lines (shared with main).
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


async def _fetch_text(
    url: str,
    headers: dict | None = None,
    timeout: int = 20,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[str, str, int]:
    """Fetch text via preferred FlareSolverr, or direct HTTP if not configured."""
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

        emit(
            f"FlareSolverr completed: target_status={status_code} "
            f"bytes={len(text)}"
        )
        return text, solution.get("url") or url, status_code

    emit(f"request via direct HTTP: {url}")
    async with httpx_client(follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        emit(
            f"direct HTTP completed: status={response.status_code} "
            f"bytes={len(response.text)}"
        )
        return response.text, str(response.url), response.status_code


async def _flaresolverr_page(url: str, timeout: int) -> dict[str, Any]:
    """Fetch a page through FlareSolverr and return its session information."""
    flaresolverr_url = effective_flaresolverr_url()
    if not flaresolverr_url:
        raise RuntimeError("FlareSolverr is not configured")

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
    html = solution.get("response")
    if not isinstance(html, str):
        raise RuntimeError("FlareSolverr returned no page HTML")

    cookies = []
    for item in solution.get("cookies") or []:
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": item.get("domain"),
                    "path": item.get("path") or "/",
                    "expires": item.get("expires"),
                    "httpOnly": item.get("httpOnly"),
                    "secure": item.get("secure"),
                    "sameSite": item.get("sameSite"),
                }
            )

    response_headers = solution.get("headers") or {}
    user_agent = next(
        (
            value
            for key, value in response_headers.items()
            if key.lower() == "user-agent"
        ),
        None,
    )

    return {
        "html": html,
        "url": solution.get("url") or url,
        "cookies": cookies,
        "user_agent": user_agent,
    }


def _safe_name(title: str, job_id: str) -> str:
    """Clean a page title into a filesystem-safe episode name."""
    name = (title or "episode").strip()
    name = re.sub(
        r"\s*[|_-]\s*(MyAsiantv|AniWave|KissAnime|Gogoanime).*$",
        "",
        name,
        flags=re.I,
    )
    name = re.sub(r"\s*English Subbed\s*", " ", name, flags=re.I)
    name = re.sub(r"\s*\(\d{4}\)\s*", " ", name)
    name = re.sub(r"[^\w\s\-_.]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return (name or "episode")[:120]


async def detect_streams(url: str, headers: dict | None = None) -> dict[str, Any]:
    """Fetch a page or M3U8 and return detected variants."""
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

    if (
        "#EXTM3U" in text
        or url.lower().endswith(".m3u8")
        or "m3u8" in final_url.lower()
    ):
        parsed = _parse_m3u8(text, final_url)
        step(f"parsed as m3u8 type={parsed['type']} variants={len(parsed['variants'])}")
        parsed["log"] = result["log"] + parsed.get("log", [])
        return parsed

    m3u8_urls = re.findall(
        r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*',
        text,
        re.I,
    )
    if not m3u8_urls:
        relative_urls = re.findall(
            r'["\']([^"\']+\.m3u8[^"\']*)["\']',
            text,
            re.I,
        )
        m3u8_urls = [urljoin(final_url, candidate) for candidate in relative_urls]

    step(f"page scan found {len(m3u8_urls)} m3u8 candidates")

    if not m3u8_urls:
        doo = re.search(r'player_api["\']?\s*[:=]\s*["\']([^"\']+)["\']', text)
        post = re.search(r"data-post=['\"](\d+)['\"]", text)
        ptype = re.search(r"data-type=['\"](\w+)['\"]", text)
        nume = re.search(r"data-nume=['\"](\d+)['\"]", text)

        if doo and post and ptype and nume:
            api = (
                doo.group(1).rstrip("/")
                + f"/{post.group(1)}/{ptype.group(1)}/{nume.group(1)}"
            )
            step(f"dooplay api: {api}")

            try:
                pr_text, _, _ = await _fetch_text(
                    api,
                    headers={**headers, "Referer": final_url},
                    timeout=15,
                    log_fn=step,
                )
                pdata = json.loads(pr_text)
                embed = (pdata or {}).get("embed_url") or ""
                step(f"dooplay embed: {embed or 'none'}")

                if embed:
                    embed_text, _, _ = await _fetch_text(
                        embed,
                        headers={**headers, "Referer": final_url},
                        timeout=15,
                        log_fn=step,
                    )
                    found = re.findall(
                        r'https?://[^\s"\'<>]+\.(?:m3u8|mp4)[^\s"\'<>]*',
                        embed_text,
                        re.I,
                    )
                    step(f"embed scan found {len(found)} media urls")

                    if found:
                        variants = [
                            {
                                "url": media_url,
                                "resolution": "embed",
                                "bandwidth": 0,
                                "codecs": "",
                                "name": "embed",
                                "average_bandwidth": 0,
                            }
                            for media_url in found[:8]
                        ]
                        return {
                            "input": url,
                            "type": "embed",
                            "variants": variants,
                            "error": None,
                            "log": result["log"],
                        }

                    step(
                        "embed page has no direct m3u8/mp4 "
                        "(stream is likely loaded by encrypted JS)"
                    )
                    return {
                        "input": url,
                        "type": "embed",
                        "variants": [],
                        "error": (
                            "Found the player embed, but the stream URL is "
                            "encrypted/loaded by JavaScript. Direct download "
                            "is not available for this page."
                        ),
                        "log": result["log"],
                    }
            except Exception as exc:
                step(f"dooplay follow failed: {exc}")

    if not m3u8_urls:
        result["type"] = "page"
        result["error"] = (
            "No .m3u8 playlists found on the page. Paste an M3U8 URL directly."
        )
        step(result["error"])
        return result

    for media_url in m3u8_urls[:3]:
        try:
            step(f"fetching candidate {media_url}")
            media_text, final_media_url, _ = await _fetch_text(
                media_url,
                headers=headers,
                timeout=15,
                log_fn=step,
            )
            parsed = _parse_m3u8(media_text, final_media_url)
            step(
                f"candidate parsed type={parsed['type']} "
                f"variants={len(parsed['variants'])}"
            )

            if parsed["variants"]:
                parsed["type"] = (
                    "master" if parsed["type"] == "master" else "media"
                )
                parsed["log"] = result["log"]
                return parsed

        except Exception as exc:
            step(f"candidate failed: {exc}")

    result["type"] = "page"
    result["error"] = "Found .m3u8 links but could not parse variants."
    step(result["error"])
    return result


def _parse_m3u8(text: str, base_url: str) -> dict[str, Any]:
    """Parse master or media playlist into variants."""
    out: dict[str, Any] = {
        "input": base_url,
        "type": "media",
        "variants": [],
        "error": None,
    }
    lines = text.splitlines()
    variants = []
    index = 0

    while index < len(lines):
        line = lines[index].strip()

        if line.startswith("#EXT-X-STREAM-INF:"):
            out["type"] = "master"
            attrs = _parse_inf_attrs(line)
            index += 1

            if index < len(lines):
                uri = lines[index].strip()
                if uri and not uri.startswith("#"):
                    full_url = urljoin(base_url, uri)
                    resolution = attrs.get("RESOLUTION", "")
                    height = ""

                    if "x" in resolution:
                        try:
                            height = resolution.split("x")[1]
                        except Exception:
                            height = ""

                    variants.append(
                        {
                            "url": full_url,
                            "resolution": (
                                f"{height}p" if height else resolution or "unknown"
                            ),
                            "bandwidth": int(attrs.get("BANDWIDTH", 0) or 0),
                            "codecs": attrs.get("CODECS", ""),
                            "name": attrs.get("NAME", ""),
                            "average_bandwidth": int(
                                attrs.get("AVERAGE-BANDWIDTH", 0) or 0
                            ),
                        }
                    )

        elif line.startswith("#EXTINF:") and out["type"] != "master":
            out["type"] = "media"
            variants.append(
                {
                    "url": base_url,
                    "resolution": "original",
                    "bandwidth": 0,
                    "codecs": "",
                    "name": "media",
                    "average_bandwidth": 0,
                }
            )
            break

        index += 1

    seen = set()
    clean = []
    for variant in variants:
        if variant["url"] not in seen:
            seen.add(variant["url"])
            clean.append(variant)

    out["variants"] = clean

    if not clean and "#EXTM3U" in text:
        out["type"] = "media"
        out["variants"] = [
            {
                "url": base_url,
                "resolution": "original",
                "bandwidth": 0,
                "codecs": "",
                "name": "media",
                "average_bandwidth": 0,
            }
        ]

    return out


def _parse_inf_attrs(line: str) -> dict[str, str]:
    """Parse attributes from an #EXT-X-STREAM-INF line."""
    body = line.split(":", 1)[1] if ":" in line else ""
    attrs = {}
    parts = re.findall(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)', body, re.I)
    for key, value in parts:
        attrs[key.upper()] = value.strip('"')
    return attrs


def enqueue_stream(
    title: str,
    stream_url: str,
    headers: dict | None = None,
) -> str:
    """Insert a stream job into the downloads table. Caller schedules the worker."""
    job_id = str(uuid.uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO downloads(id,title,url,status) VALUES(?,?,?,'queued')",
            (job_id, title, stream_url),
        )
    return job_id


def run_stream_download(
    job_id: str,
    title: str,
    stream_url: str,
    headers: dict | None = None,
) -> None:
    """Blocking FFmpeg download. Updates the downloads row as it goes."""
    from .downloader import DownloadCancelled, is_cancelled
    import shutil

    if not shutil.which("ffmpeg"):
        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='failed', error=? WHERE id=?",
                (
                    "ffmpeg not found in container. Install ffmpeg to download streams.",
                    job_id,
                ),
            )
        return

    staging = STAGING_DIR / f"{job_id}.part.mkv"
    final = DOWNLOAD_DIR / (_safe_name(title, job_id) + ".mkv")

    try:
        if is_cancelled(job_id):
            raise DownloadCancelled("Cancelled")

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        STAGING_DIR.mkdir(parents=True, exist_ok=True)

        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='downloading', progress=0, error=NULL "
                "WHERE id=? AND status!='cancelled'",
                (job_id,),
            )

        effective_headers = dict(headers or {})

        if (
            "kisscloud" in stream_url.lower()
            and "referer" not in {key.lower() for key in effective_headers}
        ):
            effective_headers["Referer"] = "https://kisscloud.online/"

        if "user-agent" not in {key.lower() for key in effective_headers}:
            effective_headers["User-Agent"] = USER_AGENT

        hdr_lines = [
            f"{key}: {value}"
            for key, value in effective_headers.items()
            if key.lower() in ("user-agent", "referer", "cookie", "origin")
        ]
        header_arg = "\r\n".join(hdr_lines) + "\r\n" if hdr_lines else ""

        # Probe the playlist so we can see what FFmpeg will hit.
        probe_note = ""
        if ".m3u8" in stream_url.lower():
            try:
                probe_headers = {
                    "User-Agent": effective_headers.get("User-Agent", USER_AGENT),
                    "Referer": effective_headers.get(
                        "Referer", "https://kisscloud.online/"
                    ),
                }
                if effective_headers.get("Cookie"):
                    probe_headers["Cookie"] = effective_headers["Cookie"]

                with httpx_sync_client(
                    follow_redirects=True,
                    timeout=15,
                ) as client:
                    playlist_response = client.get(
                        stream_url,
                        headers=probe_headers,
                    )
                    body = playlist_response.text
                    stream_infs = re.findall(
                        r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)",
                        body,
                    )
                    video_urls = [
                        urljoin(stream_url, uri.strip().strip('"'))
                        for _, uri in stream_infs
                        if uri.strip()
                    ]

                    probe_note = (
                        f"playlist {playlist_response.status_code} "
                        f"len={len(body)} video={video_urls[:2]}"
                    )
                    _log(probe_note)

                    if video_urls:
                        try:
                            variant_response = client.get(
                                video_urls[0],
                                headers=probe_headers,
                            )
                            variant_body = variant_response.text
                            snippet = variant_body[:250].replace("\n", " | ")
                            probe_note += (
                                f" | variant0 {variant_response.status_code} "
                                f"len={len(variant_body)}: {snippet}"
                            )
                            _log(
                                f"variant0: {variant_response.status_code} "
                                f"{snippet[:180]}"
                            )

                            segment_match = re.search(
                                r"(https?://[^\s]+\.html)",
                                variant_body,
                            )
                            if segment_match:
                                segment_response = client.get(
                                    segment_match.group(1),
                                    headers=probe_headers,
                                )
                                content_type = segment_response.headers.get(
                                    "content-type", ""
                                )
                                start_text = segment_response.text[:150].replace(
                                    "\n", " "
                                )
                                probe_note += (
                                    f" | seg {segment_response.status_code} "
                                    f"type={content_type} "
                                    f"len={len(segment_response.content)} "
                                    f"start={start_text}"
                                )
                                _log(
                                    f"segment: {segment_response.status_code} "
                                    f"{content_type} "
                                    f"{len(segment_response.content)} bytes "
                                    f"start={start_text}"
                                )
                        except Exception as exc:
                            probe_note += f" | variant0 failed: {exc}"
                            _log(f"variant0 failed: {exc}")

            except Exception as exc:
                probe_note = f"playlist probe failed: {exc}"

        # For Kisscloud-style playlists, download segments ourselves.
        if "kisscloud" in stream_url.lower() and ".m3u8" in stream_url.lower():
            try:
                probe_headers = {
                    "User-Agent": effective_headers.get("User-Agent", USER_AGENT),
                    "Referer": effective_headers.get(
                        "Referer", "https://kisscloud.online/"
                    ),
                }
                if effective_headers.get("Cookie"):
                    probe_headers["Cookie"] = effective_headers["Cookie"]

                with httpx_sync_client(
                    follow_redirects=True,
                    timeout=30,
                ) as client:
                    master = client.get(
                        stream_url,
                        headers=probe_headers,
                    ).text

                    sub_tracks = re.findall(
                        r'#EXT-X-MEDIA:TYPE=SUBTITLES[^\n]*LANGUAGE="([^"]+)"'
                        r'[^\n]*NAME="([^"]+)"[^\n]*URI="([^"]+)"',
                        master,
                    )
                    _log(f"subtitles found: {len(sub_tracks)}")

                    stream_infs = re.findall(
                        r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)",
                        master,
                    )
                    best = None
                    best_bandwidth = -1

                    for info, variant_uri in stream_infs:
                        match = re.search(r"BANDWIDTH=(\d+)", info)
                        bandwidth = int(match.group(1)) if match else 0
                        if bandwidth > best_bandwidth:
                            best_bandwidth = bandwidth
                            best = variant_uri.strip()

                    if best:
                        variant_url = urljoin(stream_url, best)
                        _log(
                            f"using variant bw={best_bandwidth} "
                            f"{variant_url[:80]}"
                        )
                        variant = client.get(
                            variant_url,
                            headers=probe_headers,
                        ).text

                        segments = [
                            urljoin(variant_url, line.strip())
                            for line in variant.splitlines()
                            if line.strip() and not line.strip().startswith("#")
                        ]
                        _log(f"manual: {len(segments)} segments (full)")

                        if segments:
                            limit = len(segments)
                            segment_dir = STAGING_DIR / f"{job_id}_segs"
                            segment_dir.mkdir(exist_ok=True)
                            paths = []

                            for index, segment_url in enumerate(segments):
                                if is_cancelled(job_id):
                                    raise DownloadCancelled("Cancelled")

                                segment_response = client.get(
                                    segment_url,
                                    headers=probe_headers,
                                )
                                segment_response.raise_for_status()

                                segment_path = segment_dir / f"{index:05d}.ts"
                                segment_path.write_bytes(segment_response.content)
                                paths.append(segment_path)

                                if index % 20 == 0 or index == limit - 1:
                                    with connect() as db:
                                        db.execute(
                                            "UPDATE downloads SET progress=? WHERE id=?",
                                            (int((index + 1) / limit * 90), job_id),
                                        )

                            raw_ts = STAGING_DIR / f"{job_id}.raw.ts"
                            with raw_ts.open("wb") as output_file:
                                for path in paths:
                                    output_file.write(path.read_bytes())

                            # Download subtitle tracks (best-effort).
                            subtitle_files = []
                            for lang, name, uri in sub_tracks[:3]:
                                try:
                                    subtitle_url = urljoin(stream_url, uri)
                                    _log(
                                        f"subtitle {lang} raw={subtitle_url[:100]}"
                                    )

                                    candidates = []
                                    if "neocdn" in subtitle_url:
                                        base = (
                                            subtitle_url.split("/r/")[-1]
                                            if "/r/" in subtitle_url
                                            else subtitle_url.split("/")[-1]
                                        )
                                        host = urlparse(subtitle_url).netloc
                                        candidates.extend(
                                            [
                                                f"https://{host}/r/{base}.html",
                                                f"https://{host}/r/{base}",
                                                f"https://{host}/{base}.html",
                                            ]
                                        )
                                    candidates.append(subtitle_url)

                                    for candidate in candidates:
                                        subtitle_response = client.get(
                                            candidate,
                                            headers=probe_headers,
                                        )
                                        is_vtt = (
                                            b"WEBVTT"
                                            in subtitle_response.content[:40]
                                            or (
                                                b"-->"
                                                in subtitle_response.content[:800]
                                                and b"<html"
                                                not in subtitle_response.content[
                                                    :200
                                                ].lower()
                                            )
                                        )
                                        _log(
                                            f"  {subtitle_response.status_code} "
                                            f"vtt={is_vtt} "
                                            f"len={len(subtitle_response.content)} "
                                            f"{candidate[:70]}"
                                        )

                                        if (
                                            subtitle_response.status_code == 200
                                            and is_vtt
                                            and len(subtitle_response.content) > 100
                                        ):
                                            subtitle_path = (
                                                STAGING_DIR / f"{job_id}_{lang}.vtt"
                                            )
                                            subtitle_path.write_bytes(
                                                subtitle_response.content
                                            )
                                            subtitle_files.append(
                                                (lang, name, subtitle_path)
                                            )
                                            _log(f"subtitle {lang} saved")
                                            break
                                    else:
                                        _log(f"subtitle {lang} no vtt")

                                except Exception as exc:
                                    _log(f"subtitle {lang} failed: {exc}")

                            output_path = STAGING_DIR / f"{job_id}.part.mkv"
                            cmd = [
                                "ffmpeg",
                                "-y",
                                "-hide_banner",
                                "-loglevel",
                                "error",
                                "-f",
                                "mpegts",
                                "-i",
                                str(raw_ts),
                            ]

                            for _, _, subtitle_path in subtitle_files:
                                cmd += ["-i", str(subtitle_path)]

                            cmd += ["-map", "0:v", "-map", "0:a?"]
                            for index in range(len(subtitle_files)):
                                cmd += ["-map", str(index + 1)]

                            cmd += [
                                "-c",
                                "copy",
                                "-c:s",
                                "srt",
                                "-bsf:a",
                                "aac_adtstoasc",
                            ]

                            for index, (lang, name, _) in enumerate(subtitle_files):
                                cmd += [
                                    f"-metadata:s:s:{index}",
                                    f"language={lang}",
                                    f"-metadata:s:s:{index}",
                                    f"title={name}",
                                ]

                            cmd += [str(output_path)]
                            remux = subprocess.run(
                                cmd,
                                capture_output=True,
                                text=True,
                                timeout=600,
                            )
                            if remux.returncode != 0:
                                raise RuntimeError(
                                    f"remux failed: {remux.stderr[-400:]}"
                                )

                            nice_name = _safe_name(
                                title if title and title != "captured" else "episode",
                                job_id,
                            )
                            final = DOWNLOAD_DIR / f"{nice_name}.mkv"
                            shutil.move(str(output_path), str(final))

                            with connect() as db:
                                db.execute(
                                    "UPDATE downloads SET status='completed', "
                                    "progress=100, path=? WHERE id=?",
                                    (str(final), job_id),
                                )

                            _log(
                                f"manual download complete: {final} "
                                f"({final.stat().st_size} bytes)"
                            )

                            try:
                                probe = subprocess.run(
                                    [
                                        "ffprobe",
                                        "-hide_banner",
                                        "-show_format",
                                        "-show_streams",
                                        str(final),
                                    ],
                                    capture_output=True,
                                    text=True,
                                    timeout=30,
                                )
                                _log(
                                    f"ffprobe: "
                                    f"{probe.stdout[:500] or probe.stderr[:300]}"
                                )
                            except Exception as exc:
                                _log(f"ffprobe failed: {exc}")

                            try:
                                raw_ts.unlink()
                                for path in paths:
                                    path.unlink()
                                segment_dir.rmdir()
                            except Exception:
                                pass

                            return

            except DownloadCancelled:
                raise
            except Exception as exc:
                _log(f"manual download failed: {exc}", "error")
                # Fall through to normal FFmpeg download.

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-user_agent",
            effective_headers.get("User-Agent", USER_AGENT),
            "-allowed_extensions",
            "ALL",
            "-extension_picky",
            "0",
            "-f",
            "hls",
        ]

        if header_arg:
            cmd += ["-headers", header_arg]

        cmd += [
            "-i",
            stream_url,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(staging),
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
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

            elapsed = int(time.time() - start)
            with connect() as db:
                db.execute(
                    "UPDATE downloads SET progress=? WHERE id=? "
                    "AND status='downloading'",
                    (min(95, elapsed), job_id),
                )
            time.sleep(2)

        if proc.returncode != 0:
            error_text = (proc.stderr.read() or b"").decode(errors="replace")[-300:]
            message = f"FFmpeg failed: {error_text or 'unknown error'}"
            if probe_note:
                message = probe_note + " || " + message
            _log(message, "error")
            raise RuntimeError(message)

        if not staging.exists() or staging.stat().st_size < 1000:
            raise RuntimeError("FFmpeg produced no usable output")

        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(final))

        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='completed', progress=100, "
                "path=?, error=NULL WHERE id=?",
                (str(final), job_id),
            )

    except Exception as exc:
        error = str(exc)[:500]
        with connect() as db:
            db.execute(
                "UPDATE downloads SET status='failed', error=? WHERE id=?",
                (error, job_id),
            )

        if staging.exists():
            try:
                staging.unlink()
            except Exception:
                pass
        raise


async def capture_with_browser(url: str, timeout: int = 15) -> dict:
    """Capture media requests, using FlareSolverr to bootstrap browser session.

    FlareSolverr is used first to obtain page cookies and the resolved URL.
    Playwright then navigates to the real URL with those cookies so scripts and
    relative resources run under the site's actual browser origin. This is a
    separate Playwright request; FlareSolverr is not a proxy for browser media
    requests or FFmpeg downloads.
    """
    result = {
        "input": url,
        "type": "browser",
        "variants": [],
        "error": None,
        "log": [],
    }

    def step(msg: str, level: str = "info") -> None:
        result["log"].append(msg)
        _log(msg, level)

    step(f"browser capture start: {url}")
    proxy = get_proxy()
    step(f"proxy: {proxy.split('@')[-1] if proxy else 'none'}")

    fs_page = None
    if effective_flaresolverr_url():
        step("capture mode: FlareSolverr session bootstrap, then real browser navigation")
        try:
            fs_page = await _flaresolverr_page(url, timeout)
            step(
                f"FlareSolverr fetched page: {fs_page['url']} "
                f"bytes={len(fs_page['html'])}"
            )
            step(f"FlareSolverr returned {len(fs_page['cookies'])} cookie(s)")
        except Exception as exc:
            step(
                "FlareSolverr bootstrap failed; using direct browser session: "
                f"{exc}",
                "warning",
            )
    else:
        step("capture mode: direct Playwright (FlareSolverr is not configured)")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        result["error"] = (
            "playwright not installed. Add it to the image and run "
            "playwright install chromium."
        )
        step(result["error"])
        return result

    found: list[str] = []
    seen_urls: list[str] = []

    async def on_request(req):
        request_url = req.url

        if len(seen_urls) < 30 and not any(
            ignored in request_url
            for ignored in (
                ".js",
                ".css",
                ".png",
                ".jpg",
                ".woff",
                "google",
                "analytics",
            )
        ):
            seen_urls.append(request_url.split("?")[0][:120])

        if ".m3u8" in request_url.lower() or ".mp4" in request_url.lower():
            if request_url not in found:
                found.append(request_url)
                step(f"captured media {request_url}")
        elif "neocdn" in request_url.lower() and request_url.endswith(".html"):
            pass  # Subtitle tracks, skip.
        elif "/video/" in request_url.lower() and "kisscloud" in request_url.lower():
            if request_url not in found:
                found.append(request_url)
                step(f"captured embed {request_url}")

    browser = None

    try:
        async with async_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            }
            if proxy:
                launch_options["proxy"] = {"server": proxy}

            browser = await playwright.chromium.launch(**launch_options)

            fallback_user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
            context_options: dict[str, Any] = {
                "user_agent": (
                    fs_page.get("user_agent")
                    if fs_page and fs_page.get("user_agent")
                    else fallback_user_agent
                ),
                "viewport": {"width": 1280, "height": 720},
                "locale": "en-US",
            }
            context = await browser.new_context(**context_options)

            if fs_page and fs_page.get("cookies"):
                target_url = fs_page.get("url") or url
                target_host = urlparse(target_url).hostname

                cookies_to_add = []
                for cookie in fs_page["cookies"]:
                    cookie_data = {
                        key: value
                        for key, value in cookie.items()
                        if value is not None
                    }
                    # A cookie with a domain can be added directly. Otherwise,
                    # bind it to the FlareSolverr-resolved host.
                    if not cookie_data.get("domain") and not cookie_data.get("url"):
                        if target_host:
                            parsed_target = urlparse(target_url)
                            scheme = parsed_target.scheme or "https"
                            cookie_data["url"] = f"{scheme}://{target_host}/"
                        else:
                            continue
                    cookies_to_add.append(cookie_data)

                if cookies_to_add:
                    await context.add_cookies(cookies_to_add)
                    step(f"added {len(cookies_to_add)} FlareSolverr cookie(s)")

            page = None
            stealth_ok = False

            try:
                from playwright_stealth import stealth_async

                page = await context.new_page()
                await stealth_async(page)
                stealth_ok = True
                step("stealth applied (1.x)")
            except ImportError:
                pass

            if not stealth_ok:
                try:
                    from playwright_stealth import Stealth

                    page = await context.new_page()
                    await Stealth().apply_stealth_async(page)
                    stealth_ok = True
                    step("stealth applied (2.x)")
                except Exception as exc:
                    page = await context.new_page()
                    step(f"stealth not applied: {exc}")

            if page is None:
                page = await context.new_page()
                if not stealth_ok:
                    step("stealth unavailable")

            page.on("request", on_request)
            page.on(
                "frameattached",
                lambda frame: frame.page.on("request", on_request)
                if frame.page
                else None,
            )

            navigation_url = fs_page["url"] if fs_page else url
            navigation_started = time.monotonic()
            step(f"Playwright navigation started: {navigation_url}")

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
                    f"{type(exc).__name__}: {exc}"
                )

                # If navigating to FlareSolverr's resolved URL fails, retry
                # the original URL using the same browser context and cookies.
                if fs_page and navigation_url != url:
                    step("retrying Playwright navigation to original URL", "warning")
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout * 1000,
                    )
                else:
                    raise

            elapsed = time.monotonic() - navigation_started
            if response is None:
                step(
                    f"navigation completed after {elapsed:.2f}s "
                    "without an HTTP response"
                )
            else:
                step(
                    f"navigation response: status={response.status} "
                    f"url={response.url} elapsed={elapsed:.2f}s"
                )
                if response.status >= 400:
                    step(
                        f"navigation returned HTTP error status {response.status}",
                        "warning",
                    )

            step("page loaded, looking for play controls")
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

            frames = page.frames
            step(f"frames: {len(frames)}")
            kisscloud = None

            for frame in frames:
                try:
                    if frame.url and frame.url != navigation_url and "about:" not in frame.url:
                        step(f"frame: {frame.url[:100]}")
                        if any(
                            token in frame.url
                            for token in (
                                "kisscloud",
                                "/video/",
                                "vhscdn",
                                "embed",
                            )
                        ):
                            kisscloud = frame.url
                except Exception:
                    pass

            if kisscloud:
                step(f"opening embed directly: {kisscloud[:80]}")
                page2 = await context.new_page()
                page2.on("request", on_request)

                try:
                    await page2.goto(
                        kisscloud,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    await page2.wait_for_timeout(5000)

                    video_id = kisscloud.rstrip("/").split("/")[-1]
                    if video_id and len(video_id) > 8:
                        api = (
                            "https://kisscloud.online/player/index.php"
                            f"?data={video_id}&do=getVideo"
                        )
                        step(f"trying getVideo POST: {api}")

                        try:
                            response_text = await page2.evaluate(
                                """async (api, videoId) => {
                                    const response = await fetch(api, {
                                        method: "POST",
                                        credentials: "include",
                                        headers: {
                                            "Content-Type":
                                                "application/x-www-form-urlencoded",
                                            "X-Requested-With": "XMLHttpRequest"
                                        },
                                        body: "hash=" + encodeURIComponent(videoId) +
                                            "&r=" + encodeURIComponent(location.href)
                                    });
                                    return await response.text();
                                }""",
                                api,
                                video_id,
                            )

                            step(
                                "getVideo response length: "
                                f"{len(response_text or '')}"
                            )
                            if response_text:
                                step(
                                    "getVideo snippet: "
                                    + response_text[:300].replace("\n", " ")
                                )

                                try:
                                    data = json.loads(response_text)
                                    step(
                                        "getVideo JSON keys: "
                                        f"{list(data.keys())[:10]}"
                                    )

                                    for key in ("securedLink", "videoSource"):
                                        value = data.get(key)
                                        if (
                                            isinstance(value, str)
                                            and value.startswith("http")
                                            and value not in found
                                        ):
                                            found.append(value)
                                            step(f"stream {key}: {value[:120]}")

                                    if data.get("ck"):
                                        step("has ck key")

                                except Exception as exc:
                                    step(f"json parse failed: {exc}")

                                for media_url in re.findall(
                                    r'https?://[^"\'\s<>]+',
                                    response_text,
                                ):
                                    if any(
                                        token in media_url.lower()
                                        for token in (
                                            "m3u8",
                                            "hls",
                                            ".mp4",
                                            "master",
                                        )
                                    ):
                                        if media_url not in found:
                                            found.append(media_url)
                                            step(f"getVideo url {media_url}")

                        except Exception as exc:
                            step(f"getVideo fetch failed: {exc}")

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

                    try:
                        injected_urls = await page2.evaluate(
                            "() => window.__directarr_found || []"
                        )
                        for media_url in injected_urls:
                            if media_url not in found:
                                found.append(media_url)
                                step(f"embed injected {media_url}")
                    except Exception:
                        pass

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
                        step(f"injected found {media_url}")
            except Exception:
                pass

            await context.close()

    except Exception as exc:
        result["error"] = str(exc)
        step(f"browser error: {type(exc).__name__}: {exc}", "error")
        return result

    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

    # Prefer signed M3U8 URLs; drop embed pages and API endpoints.
    cleaned = []
    for media_url in found:
        lower_url = media_url.lower()
        if ".m3u8" in lower_url or "/cdn/hls/" in lower_url:
            if media_url not in cleaned:
                cleaned.append(media_url)

    if cleaned:
        found = cleaned

    for media_url in found[:10]:
        result["variants"].append(
            {
                "url": media_url,
                "resolution": "captured",
                "bandwidth": 0,
                "codecs": "",
                "name": "browser",
                "average_bandwidth": 0,
            }
        )

    if not found:
        result["error"] = (
            "No m3u8/mp4 requests seen. The player may need a click "
            "or the stream is DRM."
        )
        step(result["error"])

    return result