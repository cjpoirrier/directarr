from .config import CONFIG_DIR, DB_PATH
import sqlite3


def connect():
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Volume may be missing/unwritable on first boot; sqlite will error clearly.
        pass
    db = sqlite3.connect(str(DB_PATH), timeout=30)
    db.row_factory = sqlite3.Row
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
    except sqlite3.Error:
        pass


def init_db():
    """Create tables, run lightweight migrations, seed defaults.

    Steps are isolated so a failed ALTER cannot roll back CREATE TABLE.
    """
    # 1) Core schema (own connection / commit)
    with connect() as db:
        db.executescript(_SCHEMA_SQL)
        db.commit()

    # 2) Column migrations (best-effort)
    with connect() as db:
        _migrate_downloads(db)
        db.commit()

    # 3) Seed default sync profile
    with connect() as db:
        _seed_sync_profile(db)
        db.commit()


def ensure_schema() -> None:
    """Idempotent schema ensure for request paths if startup init was skipped."""
    try:
        with connect() as db:
            # Fast path: applications table present
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='applications'"
            ).fetchone()
            if row:
                _seed_sync_profile(db)
                db.commit()
                return
        init_db()
    except Exception:
        # Last resort — try full init once more without swallowing to caller
        try:
            init_db()
        except Exception:
            pass


def add_history(event_type: str, message: str, details: str | None = None) -> None:
    try:
        ensure_schema()
        with connect() as db:
            db.execute(
                "INSERT INTO history(event_type, message, details) VALUES(?,?,?)",
                (event_type, message, details),
            )
            # Keep history bounded
            db.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT 2000)"
            )
            db.commit()
    except Exception:
        pass


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
