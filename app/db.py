from __future__ import annotations

import logging
from .config import CONFIG_DIR, DB_PATH
import sqlite3

log = logging.getLogger("directarr.db")


def connect():
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("Could not create CONFIG_DIR %s: %s", CONFIG_DIR, e)
    # check_same_thread=False is safe here; FastAPI runs sync endpoints in a threadpool
    db = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return db


_SCHEMA_SQL = """
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
CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  app_type TEXT NOT NULL,
  base_url TEXT NOT NULL,
  api_key TEXT NOT NULL DEFAULT '',
  sync_level TEXT NOT NULL DEFAULT 'fullSync',
  sync_profile_id TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  tags TEXT DEFAULT '',
  last_sync_at TEXT,
  last_sync_status TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sync_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  enable_rss INTEGER NOT NULL DEFAULT 1,
  enable_automatic_search INTEGER NOT NULL DEFAULT 1,
  enable_interactive_search INTEGER NOT NULL DEFAULT 1,
  minimum_seeders INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  details TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def apply_schema(db: sqlite3.Connection) -> None:
    """Run CREATE TABLE IF NOT EXISTS + migrations on an open connection."""
    db.executescript(_SCHEMA_SQL)
    _migrate_downloads(db)
    _seed_sync_profile(db)


def _migrate_downloads(db: sqlite3.Connection) -> None:
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(downloads)").fetchall()}
    except sqlite3.Error:
        return
    for col, decl in (
        ("drive_file_id", "TEXT"),
        ("size_bytes", "INTEGER"),
        ("bytes_done", "INTEGER"),
    ):
        if col not in cols:
            try:
                db.execute(f"ALTER TABLE downloads ADD COLUMN {col} {decl}")
            except sqlite3.Error:
                pass


def _seed_sync_profile(db: sqlite3.Connection) -> None:
    try:
        n = db.execute("SELECT COUNT(*) AS c FROM sync_profiles").fetchone()["c"]
        if int(n) == 0:
            db.execute(
                "INSERT INTO sync_profiles(id,name,enable_rss,enable_automatic_search,"
                "enable_interactive_search,minimum_seeders) VALUES(?,?,1,1,1,1)",
                ("default", "Standard"),
            )
    except sqlite3.Error as e:
        log.debug("seed sync profile: %s", e)


def init_db() -> None:
    """Create tables, run lightweight migrations, seed defaults."""
    log.info("Initializing database at %s", DB_PATH)
    try:
        with connect() as db:
            apply_schema(db)
            db.commit()
    except Exception:
        log.exception("init_db failed for %s", DB_PATH)
        raise


def ensure_schema() -> None:
    """Always apply CREATE IF NOT EXISTS. Raises on hard failure so callers see the cause."""
    try:
        with connect() as db:
            apply_schema(db)
            db.commit()
            # Verify critical tables exist
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='applications'"
            ).fetchone()
            if not row:
                raise RuntimeError(
                    f"applications table missing after schema apply (db={DB_PATH})"
                )
    except Exception as e:
        log.exception("ensure_schema failed (db=%s)", DB_PATH)
        raise RuntimeError(
            f"Database schema unavailable at {DB_PATH}: {e}. "
            "Check that the config volume is writable (./config → /config)."
        ) from e


def add_history(event_type: str, message: str, details: str | None = None) -> None:
    try:
        with connect() as db:
            apply_schema(db)
            db.execute(
                "INSERT INTO history(event_type, message, details) VALUES(?,?,?)",
                (event_type, message, details),
            )
            db.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT 2000)"
            )
            db.commit()
    except Exception:
        log.debug("add_history failed", exc_info=True)


def list_history(limit: int = 100, event_type: str | None = None) -> list[dict]:
    ensure_schema()
    limit = max(1, min(500, int(limit or 100)))
    with connect() as db:
        if event_type:
            rows = db.execute(
                "SELECT id, event_type, message, details, created_at FROM history "
                "WHERE event_type=? ORDER BY id DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, event_type, message, details, created_at FROM history "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]
