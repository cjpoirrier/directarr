from urllib.parse import urljoin
import httpx
from bs4 import BeautifulSoup
from .base import SourceAdapter, Release

class ExampleHtmlAdapter(SourceAdapter):
    """Generic CSS-selector adapter for permitted public pages."""
    def __init__(self, config: dict):
        self.config = config
        self.base = config["base_url"].rstrip("/")
        self.selectors = config["selectors"]

    async def search(self, query: str) -> list[Release]:
        path = self.config["search_url_template"].format(query=query)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
            headers={"User-Agent": "Directarr/0.1 self-hosted indexer"}) as client:
            response = await client.get(urljoin(self.base + "/", path.lstrip("/")))
            response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        found = []
        for node in soup.select(self.selectors["result"]):
            title_node = node.select_one(self.selectors["title"])
            url_node = node.select_one(self.selectors["url"])
            if not title_node or not url_node or not url_node.get("href"):
                continue
            date_sel = self.selectors.get("date")
            date_node = node.select_one(date_sel) if date_sel else None
            found.append(Release(
                title=title_node.get_text(" ", strip=True),
                url=urljoin(self.base + "/", url_node["href"]),
                source=self.config["name"],
                publish_date=date_node.get_text(" ", strip=True) if date_node else None
            ))
        return found
