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
    path = urlparse(url).path or ""
    return bool(re.match(r"^/\d+:/", path))


def _normalize_folder_url(url: str) -> str:
    p = urlparse(url)
    path = p.path or "/"
    if not path.endswith("/"):
        path = path + "/"
    return p._replace(path=path, query="", fragment="").geturl()


def _list_folder_once(
    client: httpx.Client,
    folder: str,
) -> list[dict[str, Any]]:
    """One folder level: return raw entries with is_folder flag."""
    folder = _normalize_folder_url(folder)
    payload: dict[str, Any] = {"password": "", "page_token": "", "page_index": 0}
    files_out: list[dict[str, Any]] = []
    page_index = 0
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
        if not next_token:
            break
        payload["page_token"] = next_token
        page_index += 1
    return files_out


def list_folder(
    url: str,
    timeout: float = 30.0,
    *,
    recursive: bool = True,
    max_depth: int = 4,
    max_files: int = 2000,
) -> dict[str, Any]:
    """List a GoIndex folder; optionally walk nested folders for files."""
    folder = _normalize_folder_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": folder,
    }
    files: list[dict[str, Any]] = []
    folders_seen: list[dict[str, Any]] = []

    with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:

        def walk(folder_url: str, depth: int, prefix: str) -> None:
            if depth > max_depth or len(files) >= max_files:
                return
            try:
                entries = _list_folder_once(client, folder_url)
            except Exception:
                return
            for entry in entries:
                if entry["is_folder"]:
                    display = f"{prefix}{entry['name']}" if prefix else entry["name"]
                    folders_seen.append({**entry, "name": display})
                    if recursive:
                        walk(entry["url"], depth + 1, f"{display}/")
                    continue
                display = f"{prefix}{entry['name']}" if prefix else entry["name"]
                files.append({**entry, "name": display, "is_folder": False})
                if len(files) >= max_files:
                    return

        walk(folder, 0, "")

    path = unquote(urlparse(folder).path or "").strip("/")
    name = path.split("/")[-1] if path else "GoIndex folder"
    return {
        "input_url": url,
        "name": name,
        "kind": "folder",
        "is_folder": True,
        "files": files,
        "folders": folders_seen if not recursive else [],
        "file_count": len(files),
        "recursive": recursive,
    }


def resolve_url(url: str, recursive: bool = True) -> dict[str, Any]:
    """Resolve a GoIndex folder or file URL into downloadable entries."""
    path = unquote(urlparse(url).path or "")
    if path and not path.endswith("/"):
        last = path.rsplit("/", 1)[-1]
        if "." in last and not last.startswith("."):
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
    return list_folder(url, recursive=recursive)
