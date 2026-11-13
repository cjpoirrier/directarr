"""GoIndex / Google Drive Index folder resolution.

Sites like drive.kayoani.online expose Drive-backed folders via a web UI.
Listing is done with POST JSON to the folder URL; individual files download
as HTTPS streams at folder_url + / + filename.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

USER_AGENT = "Directarr/0.2 self-hosted indexer"

# Hostnames that use the GoIndex-style JSON list API
KNOWN_GOINDEX_HOSTS = {
    "drive.kayoani.online",
}


def is_goindex_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if host in KNOWN_GOINDEX_HOSTS:
        return True
    # Path pattern 0:/ or 1:/ etc. (common GoIndex roots)
    path = urlparse(url).path or ""
    return bool(re.match(r"^/\d+:/", path))


def _normalize_folder_url(url: str) -> str:
    """Ensure folder URL ends with / for joining filenames."""
    p = urlparse(url)
    path = p.path or "/"
    if not path.endswith("/"):
        path = path + "/"
    return p._replace(path=path, query="", fragment="").geturl()


def list_folder(url: str, timeout: float = 30.0) -> dict[str, Any]:
    """POST list request; return {name, files:[{name,url,size,mime_type,is_folder}]}."""
    folder = _normalize_folder_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": folder,
    }
    payload = {"password": "", "page_token": "", "page_index": 0}
    files_out: list[dict[str, Any]] = []
    page_index = 0
    with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
        while page_index < 50:
            payload["page_index"] = page_index
            response = client.post(folder, json=payload)
            ct = (response.headers.get("content-type") or "").lower()
            if "json" not in ct:
                raise ValueError(
                    "This URL does not look like a GoIndex folder API "
                    f"(got {ct or 'unknown'}). Open it in a browser to confirm."
                )
            response.raise_for_status()
            data = response.json()
            block = data.get("data") or data
            items = block.get("files") or block.get("list") or []
            if not isinstance(items, list):
                items = []
            for item in items:
                name = (item.get("name") or "").strip()
                if not name:
                    continue
                mime = item.get("mimeType") or item.get("mime_type") or ""
                is_folder = (
                    mime == "application/vnd.google-apps.folder"
                    or item.get("mimeType") == "application/vnd.google-apps.folder"
                    or bool(item.get("is_dir"))
                )
                size = item.get("size")
                try:
                    size_i = int(size) if size is not None else None
                except (TypeError, ValueError):
                    size_i = None
                if is_folder:
                    file_url = urljoin(folder, quote(name) + "/")
                else:
                    # Prefer relative download link if present
                    rel = item.get("link") or ""
                    if rel.startswith("/"):
                        file_url = urljoin(folder, rel)
                    else:
                        file_url = urljoin(folder, quote(name))
                files_out.append(
                    {
                        "id": item.get("id") or name,
                        "name": name,
                        "size": size_i,
                        "mime_type": mime,
                        "is_folder": is_folder,
                        "url": file_url,
                        "download_hint": file_url,
                    }
                )
            next_token = data.get("nextPageToken")
            if not next_token and not items:
                break
            if not next_token:
                break
            payload["page_token"] = next_token
            page_index += 1

    # Folder display name from path
    path = unquote(urlparse(folder).path or "").strip("/")
    name = path.split("/")[-1] if path else "GoIndex folder"
    return {
        "input_url": url,
        "name": name,
        "kind": "folder",
        "is_folder": True,
        "files": [f for f in files_out if not f["is_folder"]],
        "folders": [f for f in files_out if f["is_folder"]],
        "file_count": sum(1 for f in files_out if not f["is_folder"]),
    }


def resolve_url(url: str) -> dict[str, Any]:
    """Resolve a GoIndex folder or file URL into downloadable entries."""
    folder = _normalize_folder_url(url)
    # If URL points at a file (has extension, no trailing listing), try as single file
    path = unquote(urlparse(url).path or "")
    if path and not path.endswith("/"):
        last = path.rsplit("/", 1)[-1]
        if "." in last and not last.startswith("."):
            # Treat as single file
            return {
                "input_url": url,
                "name": last,
                "kind": "file",
                "is_folder": False,
                "files": [
                    {
                        "id": last,
                        "name": last,
                        "size": None,
                        "mime_type": None,
                        "is_folder": False,
                        "url": url,
                        "download_hint": url,
                    }
                ],
                "file_count": 1,
            }
    return list_folder(folder)
