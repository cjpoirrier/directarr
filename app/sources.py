"""Source registry + Prowlarr-style adapter catalog.

Built-in adapter definitions expose a settings schema. Users add instances via
the API/UI; configs are stored in config/sources.json.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .adapters.example_html import ExampleHtmlAdapter
from .adapters.generic_html import GenericHtmlAdapter
from .adapters.kayoanime import KayoAnimeAdapter
from .config import CONFIG_DIR

# Runtime adapter implementations
ADAPTERS: dict[str, type] = {
    "kayoanime": KayoAnimeAdapter,
    "generic_html": GenericHtmlAdapter,
    "example_html": ExampleHtmlAdapter,  # alias
}

# Catalog shown in "Add indexer" UI (Prowlarr-style)
ADAPTER_CATALOG: list[dict[str, Any]] = [
    {
        "id": "kayoanime",
        "name": "KayoAnime",
        "adapter": "kayoanime",
        "protocol": "direct",
        "privacy": "public",
        "categories": ["TV", "Anime"],
        "description": (
            "kayoanime.com — searches posts and extracts Google Drive share links. "
            "Requires your Google account for Drive downloads."
        ),
        "settings": [
            {"name": "name", "label": "Display name", "type": "string", "default": "KayoAnime", "required": True},
            {"name": "base_url", "label": "Base URL", "type": "url", "default": "https://kayoanime.com", "required": True},
            {"name": "follow_posts", "label": "Open posts and extract Drive links", "type": "boolean", "default": True, "required": False},
            {"name": "max_posts", "label": "Max posts to open", "type": "number", "default": 5, "required": False},
            {"name": "timeout", "label": "HTTP timeout (seconds)", "type": "number", "default": 25, "required": False},
            {"name": "priority", "label": "Priority (lower = preferred)", "type": "number", "default": 25, "required": False},
        ],
    },
    {
        "id": "animekaizen",
        "name": "AnimeKaizen",
        "adapter": "generic_html",
        "protocol": "direct",
        "privacy": "public",
        "categories": ["TV", "Anime"],
        "description": (
            "www.animekaizen.org — searches posts and extracts a4u/open download links. "
            "Enable Follow posts so results are real download URLs, not empty HTML pages."
        ),
        "settings": [
            {"name": "name", "label": "Display name", "type": "string", "default": "AnimeKaizen", "required": True},
            {"name": "base_url", "label": "Base URL", "type": "url", "default": "https://www.animekaizen.org", "required": True},
            {"name": "search_url_template", "label": "Search URL template", "type": "string", "default": "/?s={query}", "required": True},
            {"name": "selector_result", "label": "Result item selector", "type": "string", "default": "article", "required": True},
            {"name": "selector_title", "label": "Title selector", "type": "string", "default": "h2 a, .entry-title a", "required": True},
            {"name": "selector_url", "label": "Link selector", "type": "string", "default": "h2 a, .entry-title a", "required": True},
            {"name": "selector_date", "label": "Date selector (optional)", "type": "string", "default": "", "required": False},
            {"name": "follow_posts", "label": "Open posts and extract download links", "type": "boolean", "default": True, "required": False},
            {"name": "selector_drive_link", "label": "Download link selector", "type": "string", "default": "a.a4u-download-link, a[href*='/open/?id=']", "required": False},
            {"name": "max_posts", "label": "Max posts to open", "type": "number", "default": 5, "required": False},
            {"name": "max_results", "label": "Max search results", "type": "number", "default": 25, "required": False},
            {"name": "timeout", "label": "HTTP timeout (seconds)", "type": "number", "default": 25, "required": False},
            {"name": "priority", "label": "Priority (lower = preferred)", "type": "number", "default": 25, "required": False},
        ],
    },
    {
        "id": "generic_html",
        "name": "Generic HTML site",
        "adapter": "generic_html",
        "protocol": "direct",
        "privacy": "public",
        "categories": ["TV", "Anime"],
        "description": (
            "Any public HTML listing site. Configure the search URL template and "
            "CSS selectors. Turn on Follow posts so search returns download links "
            "instead of the post page (post pages download as blank HTML files)."
        ),
        "settings": [
            {"name": "name", "label": "Display name", "type": "string", "default": "My site", "required": True},
            {"name": "base_url", "label": "Base URL", "type": "url", "default": "https://example.com", "required": True},
            {"name": "search_url_template", "label": "Search URL template", "type": "string", "default": "/?s={query}", "required": True,
             "help": "Use {query} (URL-encoded) or {query_raw}."},
            {"name": "selector_result", "label": "Result item selector", "type": "string", "default": "article", "required": True},
            {"name": "selector_title", "label": "Title selector", "type": "string", "default": "h2 a, h2", "required": True},
            {"name": "selector_url", "label": "Link selector", "type": "string", "default": "h2 a, a", "required": True},
            {"name": "selector_date", "label": "Date selector (optional)", "type": "string", "default": "", "required": False},
            {"name": "follow_posts", "label": "Open posts and extract download links", "type": "boolean", "default": True, "required": False},
            {"name": "selector_drive_link", "label": "Download link selector (optional)", "type": "string",
             "default": "a.a4u-download-link, a.kayo-download-link, a[href*='drive.google.com'], a[href*='/open/?id=']", "required": False},
            {"name": "max_posts", "label": "Max posts to open", "type": "number", "default": 5, "required": False},
            {"name": "max_results", "label": "Max search results", "type": "number", "default": 25, "required": False},
            {"name": "timeout", "label": "HTTP timeout (seconds)", "type": "number", "default": 25, "required": False},
            {"name": "priority", "label": "Priority (lower = preferred)", "type": "number", "default": 25, "required": False},
        ],
    },
]

def _sources_path():
    return CONFIG_DIR / "sources.json"


def _read_raw() -> list[dict[str, Any]]:
    path = _sources_path()
    if not path.exists():
        path.write_text("[]\n", encoding="utf-8")
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _write_raw(raw: list[dict[str, Any]]) -> None:
    path = _sources_path()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


def list_adapter_catalog() -> list[dict[str, Any]]:
    return list(ADAPTER_CATALOG)


def get_catalog_entry(adapter_id: str) -> dict[str, Any] | None:
    for entry in ADAPTER_CATALOG:
        if entry["id"] == adapter_id or entry["adapter"] == adapter_id:
            return entry
    return None


def _normalize_generic_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Map flat UI fields into the nested shape GenericHtmlAdapter expects."""
    out = dict(settings)
    selectors = dict(out.get("selectors") or {})
    mapping = {
        "selector_result": "result",
        "selector_title": "title",
        "selector_url": "url",
        "selector_date": "date",
        "selector_drive_link": "drive_link",
    }
    for flat, nested in mapping.items():
        if flat in out and out[flat] not in (None, ""):
            selectors[nested] = out.pop(flat)
        elif flat in out:
            out.pop(flat)
    if selectors:
        out["selectors"] = selectors
    return out


def _slug_id(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "source").lower()).strip("-") or "source"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def load_sources() -> tuple[list[dict[str, Any]], list]:
    raw = _read_raw()
    adapters = []
    for item in raw:
        if not item.get("enabled"):
            continue
        name = item.get("adapter")
        cls = ADAPTERS.get(name)
        if cls is None:
            continue
        try:
            cfg = _normalize_generic_settings(item) if name in {"generic_html", "example_html"} else item
            adapters.append((item, cls(cfg)))
        except Exception:
            continue
    return raw, adapters


def set_source_enabled(source_id: str, enabled: bool) -> dict[str, Any]:
    raw = _read_raw()
    found = None
    for item in raw:
        if item.get("id") == source_id:
            item["enabled"] = bool(enabled)
            found = item
            break
    if found is None:
        raise KeyError(f"Source not found: {source_id}")
    _write_raw(raw)
    return _public_source(found)


def add_source(adapter_id: str, settings: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    entry = get_catalog_entry(adapter_id)
    if entry is None:
        raise KeyError(f"Unknown adapter: {adapter_id}")
    if entry["adapter"] not in ADAPTERS:
        raise KeyError(f"Adapter implementation missing: {entry['adapter']}")

    # Apply defaults from schema then overlay user settings
    merged: dict[str, Any] = {}
    for field in entry.get("settings") or []:
        if "default" in field:
            merged[field["name"]] = field["default"]
    merged.update({k: v for k, v in (settings or {}).items() if v is not None})

    for field in entry.get("settings") or []:
        if field.get("required") and not str(merged.get(field["name"], "")).strip():
            raise ValueError(f"Missing required setting: {field['name']}")

    source = {
        "id": _slug_id(str(merged.get("name") or entry["name"])),
        "adapter": entry["adapter"],
        "enabled": bool(enabled),
        **merged,
    }
    if entry["adapter"] in {"generic_html", "example_html"}:
        source = _normalize_generic_settings(source)

    # Validate constructible
    ADAPTERS[entry["adapter"]](source)

    raw = _read_raw()
    raw.append(source)
    _write_raw(raw)
    return _public_source(source)


def delete_source(source_id: str) -> None:
    raw = _read_raw()
    new_raw = [s for s in raw if s.get("id") != source_id]
    if len(new_raw) == len(raw):
        raise KeyError(f"Source not found: {source_id}")
    _write_raw(new_raw)


def update_source_settings(source_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    raw = _read_raw()
    found = None
    for item in raw:
        if item.get("id") == source_id:
            found = item
            break
    if found is None:
        raise KeyError(f"Source not found: {source_id}")
    for k, v in (settings or {}).items():
        if k in {"id", "adapter"}:
            continue
        found[k] = v
    if found.get("adapter") in {"generic_html", "example_html"}:
        normalized = _normalize_generic_settings(found)
        found.clear()
        found.update(normalized)
    _write_raw(raw)
    return _public_source(found)


def _catalog_meta_for(adapter: str | None) -> dict[str, Any]:
    for entry in ADAPTER_CATALOG:
        if entry.get("adapter") == adapter or entry.get("id") == adapter:
            return entry
    return {}


def _public_source(s: dict[str, Any]) -> dict[str, Any]:
    meta = _catalog_meta_for(s.get("adapter"))
    priority = s.get("priority")
    try:
        priority = int(priority) if priority is not None else 25
    except (TypeError, ValueError):
        priority = 25
    return {
        "id": s.get("id"),
        "name": s.get("name"),
        "enabled": bool(s.get("enabled", False)),
        "adapter": s.get("adapter"),
        "base_url": s.get("base_url"),
        "priority": priority,
        "protocol": meta.get("protocol") or "direct",
        "privacy": meta.get("privacy") or "public",
        "categories": list(meta.get("categories") or ["TV", "Anime"]),
        "settings": {
            k: v
            for k, v in s.items()
            if k not in {"id", "adapter", "enabled"}
        },
    }


def list_sources_public() -> list[dict[str, Any]]:
    return [_public_source(s) for s in _read_raw()]
