"""Google Drive authorized link resolution.

Uses the official Drive API with the operator's OAuth credentials.
Does not bypass sharing permissions — the authenticated Google account
must already have access to the shared file or folder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .config import CONFIG_DIR, GDRIVE_CLIENT_SECRETS, GDRIVE_SCOPES, GDRIVE_TOKEN_PATH

# Lazy imports so the rest of the app starts without Google libs installed.
_google_ready = False


def _ensure_google():
    global _google_ready
    if _google_ready:
        return
    try:
        import google.auth.transport.requests  # noqa: F401
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
        import googleapiclient.http  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Google API libraries are not installed. "
            "Install: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        ) from exc
    _google_ready = True


@dataclass
class DriveRef:
    kind: str  # "file" | "folder" | "unknown"
    file_id: str
    original_url: str


@dataclass
class DriveItem:
    id: str
    name: str
    mime_type: str
    size: Optional[int]
    is_folder: bool
    web_view_link: Optional[str] = None


_FILE_PATTERNS = [
    re.compile(r"/file/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"/document/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"/presentation/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)"),
]
_FOLDER_PATTERNS = [
    re.compile(r"/folders/([a-zA-Z0-9_-]+)"),
    re.compile(r"/drive/folders/([a-zA-Z0-9_-]+)"),
]


def parse_drive_url(url: str) -> DriveRef:
    """Extract file/folder id from common Google Drive URL shapes."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host not in {"drive.google.com", "docs.google.com"}:
        raise ValueError("Not a Google Drive or Docs URL.")

    path = parsed.path or ""
    for pattern in _FOLDER_PATTERNS:
        m = pattern.search(path)
        if m:
            return DriveRef(kind="folder", file_id=m.group(1), original_url=url)
    for pattern in _FILE_PATTERNS:
        m = pattern.search(path)
        if m:
            return DriveRef(kind="file", file_id=m.group(1), original_url=url)

    qs = parse_qs(parsed.query)
    for key in ("id", "fileid", "resourcekey"):
        if key == "resourcekey":
            continue
        if key in qs and qs[key]:
            return DriveRef(kind="unknown", file_id=qs[key][0], original_url=url)

    # uc?export=download&id=
    if "id" in qs and qs["id"]:
        return DriveRef(kind="file", file_id=qs["id"][0], original_url=url)

    raise ValueError("Could not extract a Google Drive file or folder id from the URL.")


def is_drive_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host in {"drive.google.com", "docs.google.com"}
    except Exception:
        return False


def credentials_status() -> dict[str, Any]:
    secrets = Path(GDRIVE_CLIENT_SECRETS)
    token = Path(GDRIVE_TOKEN_PATH)
    return {
        "client_secrets_present": secrets.is_file(),
        "token_present": token.is_file(),
        "client_secrets_path": str(secrets),
        "token_path": str(token),
        "scopes": list(GDRIVE_SCOPES),
        "authorized": token.is_file() and secrets.is_file(),
    }


def _load_credentials():
    _ensure_google()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = Path(GDRIVE_TOKEN_PATH)
    secrets_path = Path(GDRIVE_CLIENT_SECRETS)
    if not secrets_path.is_file():
        raise RuntimeError(
            f"Missing OAuth client secrets at {secrets_path}. "
            "Create a Desktop OAuth client in Google Cloud Console, enable Drive API, "
            "and save the JSON as config/credentials.json."
        )

    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), GDRIVE_SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    raise RuntimeError(
        "Google Drive is not authorized yet. "
        "Call GET /api/v1/gdrive/auth-url, open the URL, then POST the code to "
        "/api/v1/gdrive/auth-code."
    )


def build_auth_url(redirect_uri: str = "http://localhost") -> dict[str, str]:
    """Start OAuth and return a browser URL + state.

    Uses the loopback redirect (http://localhost) required by modern Google
    Desktop OAuth clients. The browser will fail to load the page after consent;
    copy the `code` parameter from the address bar and POST it to
    /api/v1/gdrive/auth-code.
    """
    _ensure_google()
    from google_auth_oauthlib.flow import Flow

    secrets_path = Path(GDRIVE_CLIENT_SECRETS)
    if not secrets_path.is_file():
        raise RuntimeError(f"Missing OAuth client secrets at {secrets_path}")

    flow = Flow.from_client_secrets_file(
        str(secrets_path),
        scopes=GDRIVE_SCOPES,
        redirect_uri=redirect_uri,
    )
    # PKCE is enabled by default for Desktop clients; we must persist the
    # code_verifier so the later token exchange can include it.
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    code_verifier = getattr(flow, "code_verifier", None) or ""
    # Persist state + redirect + PKCE verifier for the code exchange step.
    state_path = CONFIG_DIR / "gdrive_oauth_state.txt"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        f"{state}\n{redirect_uri}\n{code_verifier}\n",
        encoding="utf-8",
    )
    return {"auth_url": auth_url, "state": state, "redirect_uri": redirect_uri}


def _extract_oauth_code(value: str) -> str:
    """Accept either a bare authorization code or the full localhost redirect URL."""
    value = (value or "").strip()
    if not value:
        raise ValueError("Empty authorization code.")
    # User pasted the entire redirect URL: http://localhost/?code=...&state=...
    if "code=" in value or value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        qs = parse_qs(parsed.query)
        if "code" in qs and qs["code"]:
            return qs["code"][0].strip()
        # Also handle rare fragment-style responses
        frag = parse_qs(parsed.fragment)
        if "code" in frag and frag["code"]:
            return frag["code"][0].strip()
        raise ValueError(
            "Could not find a 'code' parameter in the pasted URL. "
            "Paste the full address-bar URL after Google redirects to localhost."
        )
    return value


def exchange_auth_code(code: str) -> dict[str, Any]:
    """Exchange an authorization code for a refreshable token.json.

    Accepts either the bare code or the full http://localhost/?code=... URL
    so the operator can paste the entire address bar after consent.
    """
    _ensure_google()
    from google_auth_oauthlib.flow import Flow

    secrets_path = Path(GDRIVE_CLIENT_SECRETS)
    state_path = CONFIG_DIR / "gdrive_oauth_state.txt"
    if not secrets_path.is_file():
        raise RuntimeError(f"Missing OAuth client secrets at {secrets_path}")

    code = _extract_oauth_code(code)

    redirect_uri = "http://localhost"
    code_verifier = None
    if state_path.is_file():
        lines = state_path.read_text(encoding="utf-8").strip().splitlines()
        if len(lines) >= 2:
            redirect_uri = lines[1].strip() or redirect_uri
        if len(lines) >= 3 and lines[2].strip():
            code_verifier = lines[2].strip()

    flow = Flow.from_client_secrets_file(
        str(secrets_path),
        scopes=GDRIVE_SCOPES,
        redirect_uri=redirect_uri,
    )
    if code_verifier:
        flow.code_verifier = code_verifier

    flow.fetch_token(code=code)
    creds = flow.credentials
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    Path(GDRIVE_TOKEN_PATH).write_text(creds.to_json(), encoding="utf-8")
    if state_path.is_file():
        state_path.unlink(missing_ok=True)
    return {"authorized": True, "token_path": str(GDRIVE_TOKEN_PATH)}


def _service():
    _ensure_google()
    from googleapiclient.discovery import build

    creds = _load_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_item_metadata(file_id: str) -> DriveItem:
    service = _service()
    meta = (
        service.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,size,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    mime = meta.get("mimeType") or ""
    size = meta.get("size")
    return DriveItem(
        id=meta["id"],
        name=meta.get("name") or meta["id"],
        mime_type=mime,
        size=int(size) if size is not None else None,
        is_folder=mime == "application/vnd.google-apps.folder",
        web_view_link=meta.get("webViewLink"),
    )


def list_folder(folder_id: str, page_size: int = 200) -> list[DriveItem]:
    service = _service()
    items: list[DriveItem] = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed=false"
    while True:
        response = (
            service.files()
            .list(
                q=query,
                pageSize=min(page_size, 1000),
                fields="nextPageToken, files(id,name,mimeType,size,webViewLink)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                orderBy="name_natural",
            )
            .execute()
        )
        for meta in response.get("files", []):
            mime = meta.get("mimeType") or ""
            size = meta.get("size")
            items.append(
                DriveItem(
                    id=meta["id"],
                    name=meta.get("name") or meta["id"],
                    mime_type=mime,
                    size=int(size) if size is not None else None,
                    is_folder=mime == "application/vnd.google-apps.folder",
                    web_view_link=meta.get("webViewLink"),
                )
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def resolve_url(url: str) -> dict[str, Any]:
    """Resolve a Drive share URL into metadata and, for folders, child files."""
    ref = parse_drive_url(url)
    meta = get_item_metadata(ref.file_id)
    result: dict[str, Any] = {
        "input_url": url,
        "id": meta.id,
        "name": meta.name,
        "mime_type": meta.mime_type,
        "size": meta.size,
        "is_folder": meta.is_folder,
        "web_view_link": meta.web_view_link,
        "kind": "folder" if meta.is_folder else "file",
        "files": [],
    }
    if meta.is_folder:
        children = list_folder(meta.id)
        result["files"] = [
            {
                "id": c.id,
                "name": c.name,
                "mime_type": c.mime_type,
                "size": c.size,
                "is_folder": c.is_folder,
                "web_view_link": c.web_view_link,
                # API media URL still requires Authorization: Bearer <token>
                "download_hint": f"drive://{c.id}",
            }
            for c in children
        ]
    else:
        result["files"] = [
            {
                "id": meta.id,
                "name": meta.name,
                "mime_type": meta.mime_type,
                "size": meta.size,
                "is_folder": False,
                "web_view_link": meta.web_view_link,
                "download_hint": f"drive://{meta.id}",
            }
        ]
    return result


def download_file_to_path(file_id: str, dest: Path, progress_cb=None) -> Path:
    """Download a binary Drive file using the authorized account. Returns dest path."""
    _ensure_google()
    from googleapiclient.http import MediaIoBaseDownload

    service = _service()
    meta = get_item_metadata(file_id)
    if meta.is_folder:
        raise ValueError("Cannot download a folder as a single file. Resolve and pick a file id.")
    if meta.mime_type.startswith("application/vnd.google-apps."):
        raise ValueError(
            f"Google Workspace file ({meta.mime_type}) cannot be downloaded as binary. "
            "Export is not used for anime media files."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with dest.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if progress_cb and status:
                try:
                    progress_cb(int(status.progress() * 100))
                except Exception:
                    pass
    return dest
