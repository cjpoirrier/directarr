"""KayoAnime (kayoanime.com) adapter.

Discovers anime release pages via site search, then extracts Google Drive
folder/file share links from each post. Does not bypass Drive permissions —
links still require the owner's authorization / your Google account access.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import Release, SourceAdapter

DEFAULT_BASE = "https://kayoanime.com"
USER_AGENT = "Directarr/0.1 self-hosted indexer (+https://github.com/)"
DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}


def _is_drive_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in DRIVE_HOSTS


class KayoAnimeAdapter(SourceAdapter):
    def __init__(self, config: dict):
        self.config = config
        self.base = (config.get("base_url") or DEFAULT_BASE).rstrip("/")
        self.max_posts = int(config.get("max_posts", 5))
        self.follow_posts = bool(config.get("follow_posts", True))
        self.timeout = float(config.get("timeout", 25))

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )

    async def search(self, query: str) -> list[Release]:
        query = (query or "").strip()
        if not query:
            return []

        search_url = f"{self.base}/?s={quote_plus(query)}"
        async with self._client() as client:
            response = await client.get(search_url)
            response.raise_for_status()
            posts = self._parse_search(response.text)

            if not self.follow_posts:
                return [
                    Release(
                        title=title,
                        url=url,
                        source=self.config.get("name", "KayoAnime"),
                    )
                    for title, url in posts[: self.max_posts]
                ]

            releases: list[Release] = []
            for title, post_url in posts[: self.max_posts]:
                try:
                    page = await client.get(post_url)
                    page.raise_for_status()
                    releases.extend(self._parse_post(title, post_url, page.text))
                except Exception:
                    # Keep searching other posts if one page fails.
                    continue
            return releases

    def _parse_search(self, html: str) -> list[tuple[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        found: list[tuple[str, str]] = []
        seen: set[str] = set()

        for heading in soup.select("h2.post-title"):
            anchor = heading.find("a", href=True)
            if not anchor:
                continue
            title = anchor.get_text(" ", strip=True)
            href = urljoin(self.base + "/", anchor["href"])
            if not title or href in seen:
                continue
            # Skip non-post paths (categories, tags, author).
            path = urlparse(href).path or ""
            if path in ("/", "") or path.startswith(
                ("/category/", "/tag/", "/author/", "/page/")
            ):
                continue
            seen.add(href)
            found.append((title, href))

        # Fallback: list items used on some layouts.
        if not found:
            for item in soup.select("li.post-item a[href]"):
                title = item.get("aria-label") or item.get_text(" ", strip=True)
                href = urljoin(self.base + "/", item["href"])
                if title and href not in seen and "kayoanime.com" in href:
                    seen.add(href)
                    found.append((title, href))
        return found

    def _parse_post(self, post_title: str, post_url: str, html: str) -> list[Release]:
        soup = BeautifulSoup(html, "html.parser")
        source = self.config.get("name", "KayoAnime")
        releases: list[Release] = []
        seen: set[str] = set()

        # Primary: shortcode buttons used by the theme.
        candidates = soup.select("a.shortc-button[href]")
        if not candidates:
            candidates = soup.select('a[href*="drive.google.com"]')

        for anchor in candidates:
            href = urljoin(post_url, anchor["href"]).strip()
            if not _is_drive_url(href) or href in seen:
                continue
            seen.add(href)
            label = anchor.get_text(" ", strip=True) or "Download"
            # Prefer a descriptive release title.
            if label.lower() in {"download", "drive", "google drive", "link"}:
                title = post_title
            else:
                title = f"{post_title} — {label}"
            season, episode, resolution, codec = self._meta_from_title(title)
            releases.append(
                Release(
                    title=title,
                    url=href,
                    source=source,
                    season=season,
                    episode=episode,
                    resolution=resolution,
                    codec=codec,
                )
            )

        # If a post has no Drive buttons yet, still surface the post page.
        if not releases:
            releases.append(
                Release(title=post_title, url=post_url, source=source)
            )
        return releases

    @staticmethod
    def _meta_from_title(title: str):
        match = re.search(r"(?i)S(\d{1,2})\s*E(\d{1,3})", title)
        if not match:
            match = re.search(r"(?i)(\d{1,2})x(\d{1,3})", title)
        if match:
            season, episode = int(match.group(1)), int(match.group(2))
        else:
            ep = re.search(r"(?i)(?:Episode|Ep)[.\s_-]*(\d{1,3})", title)
            season, episode = None, (int(ep.group(1)) if ep else None)
        resolution = next(
            (v for v in ("2160p", "1080p", "720p", "480p") if v.lower() in title.lower()),
            None,
        )
        codec = next(
            (
                v
                for v in ("AV1", "HEVC", "H.265", "x265", "H.264", "x264")
                if v.lower() in title.lower()
            ),
            None,
        )
        return season, episode, resolution, codec
