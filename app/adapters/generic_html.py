"""Configurable HTML site adapter (Prowlarr-style cardigann-lite)."""

from __future__ import annotations

from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import Release, SourceAdapter
from ..open_redirect import is_open_interstitial, resolve_interstitial, is_drive_url as _is_drive

USER_AGENT = "Directarr/0.2 self-hosted indexer"
DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}

# Common "this is a real download / redirector" patterns on anime DDL sites
DEFAULT_DOWNLOAD_SELECTORS = [
    "a.a4u-download-link",
    "a.kayo-download-link",
    "a.shortc-button",
    'a[href*="drive.google.com"]',
    'a[href*="docs.google.com"]',
    'a[href*="/open/?id="]',
    'a[href*="mega.nz"]',
    'a[href*="mediafire.com"]',
    'a[href*="pixeldrain.com"]',
    'a[href*="gofile.io"]',
]


def _is_drive_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in DRIVE_HOSTS


def _is_site_page(url: str, base: str) -> bool:
    try:
        u = urlparse(url)
        b = urlparse(base)
        return (u.hostname or "").lower() == (b.hostname or "").lower()
    except Exception:
        return False


class GenericHtmlAdapter(SourceAdapter):
    def __init__(self, config: dict):
        self.config = config
        self.base = (config.get("base_url") or "").rstrip("/")
        if not self.base:
            raise ValueError("base_url is required")
        self.selectors = config.get("selectors") or {}
        self.timeout = float(config.get("timeout", 25))
        self.max_results = int(config.get("max_results", 25))
        self.follow_posts = bool(config.get("follow_posts", False))
        self.max_posts = int(config.get("max_posts", 5))

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
        )

    def _search_url(self, query: str) -> str:
        template = self.config.get("search_url_template") or "/?s={query}"
        path = template.format(query=quote_plus(query), query_raw=query)
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(self.base + "/", path.lstrip("/"))

    def _download_selectors(self) -> list[str]:
        custom = (self.selectors.get("drive_link") or "").strip()
        if custom:
            return [custom] + [s for s in DEFAULT_DOWNLOAD_SELECTORS if s != custom]
        return list(DEFAULT_DOWNLOAD_SELECTORS)

    def _extract_download_links(self, soup: BeautifulSoup, page_url: str) -> list[tuple[str, str]]:
        """Return (label, absolute_url) for download-like anchors on a post page."""
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for sel in self._download_selectors():
            try:
                nodes = soup.select(sel)
            except Exception:
                continue
            for a in nodes:
                href = (a.get("href") or "").strip()
                if not href or href.startswith("#"):
                    continue
                abs_url = urljoin(page_url, href)
                if abs_url in seen:
                    continue
                # Skip pure category/nav links on same host without open/download markers
                if _is_site_page(abs_url, self.base):
                    path = (urlparse(abs_url).path or "").lower()
                    q = (urlparse(abs_url).query or "").lower()
                    if not any(
                        x in path or x in q or x in href.lower()
                        for x in ("/open/", "download", "drive", "id=")
                    ) and not _is_drive_url(abs_url):
                        # still allow if selector was explicitly a download class hit
                        classes = " ".join(a.get("class") or []).lower()
                        if "download" not in classes and "kayo" not in classes and "a4u" not in classes:
                            continue
                seen.add(abs_url)
                label = a.get_text(" ", strip=True) or abs_url
                found.append((label, abs_url))
        return found

    async def search(self, query: str) -> list[Release]:
        query = (query or "").strip()
        if not query:
            return []
        result_sel = self.selectors.get("result") or "article"
        title_sel = self.selectors.get("title") or "h2 a, h2, a"
        url_sel = self.selectors.get("url") or "a"
        date_sel = self.selectors.get("date")

        async with self._client() as client:
            response = await client.get(self._search_url(query))
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            posts: list[tuple[str, str, str | None]] = []
            for node in soup.select(result_sel):
                title_node = node.select_one(title_sel)
                url_node = node.select_one(url_sel)
                if not title_node or not url_node or not url_node.get("href"):
                    continue
                title = title_node.get_text(" ", strip=True)
                href = urljoin(self.base + "/", url_node["href"])
                date = None
                if date_sel:
                    date_node = node.select_one(date_sel)
                    if date_node:
                        date = date_node.get_text(" ", strip=True)
                posts.append((title, href, date))
                if len(posts) >= max(self.max_results, self.max_posts):
                    break

            source_name = self.config.get("name", "Generic")

            if not self.follow_posts:
                return [
                    Release(title=title, url=href, source=source_name, publish_date=date)
                    for title, href, date in posts[: self.max_results]
                ]

            releases: list[Release] = []
            for title, post_url, date in posts[: self.max_posts]:
                try:
                    page = await client.get(post_url, headers={"Referer": self.base + "/"})
                    page.raise_for_status()
                    page_soup = BeautifulSoup(page.text, "html.parser")
                    dlinks = self._extract_download_links(page_soup, post_url)
                    # Resolve /open/?id= interstitials to real Drive (or shortener) URLs
                    resolved: list[tuple[str, str]] = []
                    for label, dlink in dlinks:
                        if is_open_interstitial(dlink):
                            try:
                                dest = resolve_interstitial(
                                    dlink, referer=post_url, timeout=self.timeout
                                )
                                resolved.append((label, dest))
                            except Exception:
                                # Keep open link; queue path may retry resolve
                                resolved.append((label, dlink))
                        else:
                            resolved.append((label, dlink))
                    dlinks = resolved
                    if not dlinks:
                        # Don't queue the HTML post page as a "download" — mark clearly
                        releases.append(
                            Release(
                                title=f"{title} (no download links found)",
                                url=post_url,
                                source=source_name,
                                publish_date=date,
                            )
                        )
                        continue
                    for label, dlink in dlinks:
                        item_title = f"{title} — {label}" if label and label.lower() not in title.lower() else title
                        if len(dlinks) == 1:
                            item_title = title
                        releases.append(
                            Release(
                                title=item_title[:300],
                                url=dlink,
                                source=source_name,
                                publish_date=date,
                            )
                        )
                except Exception:
                    releases.append(
                        Release(
                            title=f"{title} (post fetch failed)",
                            url=post_url,
                            source=source_name,
                            publish_date=date,
                        )
                    )
            return releases
