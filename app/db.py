from .config import CONFIG_DIR, DB_PATH
import sqlite3


def connect():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS releases (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
          source TEXT NOT NULL, size INTEGER, publish_date TEXT,
          season INTEGER, episode INTEGER, resolution TEXT, codec TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS downloads (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
          status TEXT NOT NULL, progress INTEGER DEFAULT 0,
          path TEXT, error TEXT, drive_file_id TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cols = {row[1] for row in db.execute("PRAGMA table_info(downloads)").fetchall()}
        if "drive_file_id" not in cols:
            db.execute("ALTER TABLE downloads ADD COLUMN drive_file_id TEXT")
