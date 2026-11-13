"""Resolve site interstitial pages (e.g. animekaizen /open/?id=) to real destinations.

Many anime DDL sites wrap Google Drive URLs behind a short countdown page.
The real target is embedded in the HTML (fallback JS var or visible URL box).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

USER_AGENT = "Directarr/0.2 self-hosted indexer"

DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}


def is_open_interstitial(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    path = (p.path or "").rstrip("/")
    q = p.query or ""
    return path.endswith("/open") or path == "/open" or ("/open/" in (p.path or "") and "id=" in q)


def _pick_destination(html: str) -> str | None:
    # JS: fallback = "https://..."
    for m in re.finditer(r'fallback\s*=\s*"(https?://[^"]+)"', html):
        return m.group(1)
    for m in re.finditer(r'finalUrl\s*=\s*"(https?://[^"]+)"', html):
        return m.group(1)
    # Prefer Drive links embedded anywhere
    drives = re.findall(r'https?://(?:drive|docs)\.google\.com/[^\s"\'<>]+', html)
    if drives:
        return drives[0].rstrip(").,;'\"")
    # URL box text
    for m in re.finditer(r'https?://(?:tinyurl\.com|bit\.ly|cutt\.ly)/[^\s"\'<>]+', html):
        return m.group(0)
    return None


def resolve_interstitial(
    url: str,
    *,
    referer: str | None = None,
    timeout: float = 25.0,
) -> str:
    """Fetch interstitial and return the destination URL (may still be a shortener)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
        response = client.get(url)
        response.raise_for_status()
        dest = _pick_destination(response.text)
        if not dest:
            raise ValueError(
                "Could not find destination URL on interstitial page. "
                "The site may have changed its redirect format."
            )
        # One more hop for known shorteners
        host = (urlparse(dest).hostname or "").lower()
        if host in {"tinyurl.com", "bit.ly", "cutt.ly", "t.co"}:
            r2 = client.head(dest, follow_redirects=True)
            final = str(r2.url)
            if final.startswith("http"):
                dest = final
            else:
                r2 = client.get(dest, follow_redirects=True)
                dest = str(r2.url)
        return dest


def is_drive_url(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "").lower() in DRIVE_HOSTS
    except Exception:
        return False
