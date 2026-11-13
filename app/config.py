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
API_KEY = os.getenv("DIRECTARR_API_KEY", "change-this-long-random-key")
# Web UI forms login (Sonarr/Radarr-style). Leave password empty to disable UI auth.
AUTH_USERNAME = os.getenv("DIRECTARR_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("DIRECTARR_PASSWORD", "")
SESSION_SECRET = (
    os.getenv("DIRECTARR_SESSION_SECRET")
    or os.getenv("DIRECTARR_API_KEY")
    or "change-this-long-random-key"
)
MAX_DOWNLOAD_BYTES = int(os.getenv("DIRECTARR_MAX_DOWNLOAD_MB", "20000")) * 1024 * 1024
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
