"""Configurable HTML site adapter (Prowlarr-style cardigann-lite).

Users define search URL templates and CSS selectors in the UI — no code changes
required for many public listing sites.
"""

from __future__ import annotations

from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import Release, SourceAdapter

USER_AGENT = "Directarr/0.2 self-hosted indexer"
DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}


def _is_drive_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in DRIVE_HOSTS


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
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        )

    def _search_url(self, query: str) -> str:
        template = self.config.get("search_url_template") or "/?s={query}"
        # Support {query} and {query_quoted}
        path = template.format(query=quote_plus(query), query_raw=query)
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(self.base + "/", path.lstrip("/"))

    async def search(self, query: str) -> list[Release]:
        query = (query or "").strip()
        if not query:
            return []
        result_sel = self.selectors.get("result") or "article"
        title_sel = self.selectors.get("title") or "h2 a, h2, a"
        url_sel = self.selectors.get("url") or "a"
        date_sel = self.selectors.get("date")
        drive_sel = self.selectors.get("drive_link") or 'a[href*="drive.google.com"]'

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

            if not self.follow_posts:
                return [
                    Release(
                        title=title,
                        url=href,
                        source=self.config.get("name", "Generic"),
                        publish_date=date,
                    )
                    for title, href, date in posts[: self.max_results]
                ]

            releases: list[Release] = []
            for title, post_url, date in posts[: self.max_posts]:
                try:
                    page = await client.get(post_url)
                    page.raise_for_status()
                    page_soup = BeautifulSoup(page.text, "html.parser")
                    drive_links = []
                    seen = set()
                    for a in page_soup.select(drive_sel):
                        href = a.get("href") or ""
                        href = urljoin(post_url, href)
                        if not _is_drive_url(href) or href in seen:
                            continue
                        seen.add(href)
                        drive_links.append(href)
                    if not drive_links:
                        releases.append(
                            Release(
                                title=title,
                                url=post_url,
                                source=self.config.get("name", "Generic"),
                                publish_date=date,
                            )
                        )
                        continue
                    for i, dlink in enumerate(drive_links):
                        label = title if len(drive_links) == 1 else f"{title} [{i + 1}]"
                        releases.append(
                            Release(
                                title=label,
                                url=dlink,
                                source=self.config.get("name", "Generic"),
                                publish_date=date,
                            )
                        )
                except Exception:
                    releases.append(
                        Release(
                            title=title,
                            url=post_url,
                            source=self.config.get("name", "Generic"),
                            publish_date=date,
                        )
                    )
            return releases
