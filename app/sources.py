import json
from typing import Any

from .adapters.example_html import ExampleHtmlAdapter
from .adapters.kayoanime import KayoAnimeAdapter
from .config import CONFIG_DIR

# Registry of adapter name -> class. New adapters register here.
ADAPTERS: dict[str, type] = {
    "example_html": ExampleHtmlAdapter,
    "kayoanime": KayoAnimeAdapter,
}


def _sources_path():
    return CONFIG_DIR / "sources.json"


def load_sources() -> tuple[list[dict[str, Any]], list]:
    path = _sources_path()
    if not path.exists():
        path.write_text("[]", encoding="utf-8")
    raw = json.loads(path.read_text(encoding="utf-8"))
    adapters = []
    for item in raw:
        if not item.get("enabled"):
            continue
        name = item.get("adapter")
        cls = ADAPTERS.get(name)
        if cls is None:
            continue
        adapters.append((item, cls(item)))
    return raw, adapters


def set_source_enabled(source_id: str, enabled: bool) -> dict[str, Any]:
    path = _sources_path()
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    found = None
    for item in raw:
        if item.get("id") == source_id:
            item["enabled"] = bool(enabled)
            found = item
            break
    if found is None:
        raise KeyError(f"Source not found: {source_id}")
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return {
        "id": found.get("id"),
        "name": found.get("name"),
        "enabled": found.get("enabled", False),
        "adapter": found.get("adapter"),
    }
