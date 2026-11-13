import os
from pathlib import Path

# Load project-root .env if present (no-op when python-dotenv is missing).
_ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

# Docker Compose sets these to /config, /downloads, /staging.
# Outside Docker, fall back to project-local directories.
CONFIG_DIR = Path(os.getenv("DIRECTARR_CONFIG_DIR", str(_ROOT / "config")))
DOWNLOAD_DIR = Path(os.getenv("DIRECTARR_DOWNLOAD_DIR", str(_ROOT / "downloads")))
STAGING_DIR = Path(os.getenv("DIRECTARR_STAGING_DIR", str(_ROOT / "staging")))
DB_PATH = CONFIG_DIR / "directarr.sqlite3"
API_KEY_FILE = CONFIG_DIR / "api_key.txt"


def _load_api_key() -> str:
    """Prefer a regenerated key stored under config/, else env, else default."""
    try:
        if API_KEY_FILE.is_file():
            stored = API_KEY_FILE.read_text(encoding="utf-8").strip()
            if stored:
                return stored
    except OSError:
        pass
    return os.getenv("DIRECTARR_API_KEY", "change-this-long-random-key")


API_KEY = _load_api_key()


def set_api_key(new_key: str) -> str:
    """Persist a new API key (Sonarr-style regenerate) and update in-memory value."""
    global API_KEY
    new_key = (new_key or "").strip()
    if len(new_key) < 16:
        raise ValueError("API key must be at least 16 characters")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    API_KEY_FILE.write_text(new_key + chr(10), encoding="utf-8")
    API_KEY = new_key
    return API_KEY


MAX_DOWNLOAD_BYTES = int(os.getenv("DIRECTARR_MAX_DOWNLOAD_MB", "20000")) * 1024 * 1024
# Parallel HTTP connections for direct (non-Drive) downloads. 1 = single stream.
DOWNLOAD_CONNECTIONS = max(1, min(32, int(os.getenv("DIRECTARR_DOWNLOAD_CONNECTIONS", "8"))))
# Only split into multi-connection parts when the file is at least this large (MiB).
DOWNLOAD_MULTI_MIN_MB = max(1, int(os.getenv("DIRECTARR_DOWNLOAD_MULTI_MIN_MB", "8")))
ALLOWED_HOSTS = {
    v.strip().lower()
    for v in os.getenv("DIRECTARR_ALLOWED_HOSTS", "").split(",")
    if v.strip()
}

# Google Drive OAuth (operator must place client secrets + complete auth once).
GDRIVE_CLIENT_SECRETS = os.getenv(
    "DIRECTARR_GDRIVE_CREDENTIALS", str(CONFIG_DIR / "credentials.json")
)
GDRIVE_TOKEN_PATH = os.getenv(
    "DIRECTARR_GDRIVE_TOKEN", str(CONFIG_DIR / "token.json")
)
GDRIVE_SCOPES = [
    s.strip()
    for s in os.getenv(
        "DIRECTARR_GDRIVE_SCOPES",
        "https://www.googleapis.com/auth/drive.readonly",
    ).split(",")
    if s.strip()
]
