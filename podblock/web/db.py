"""The web app's tables, kept in the detector's own database.

The detector's `Store` makes `settings`, `podcasts`, `episodes` and the block
tables. The app adds its own tables, and later its own columns on
`podcasts` and `episodes`, through the numbered steps in MIGRATIONS. The
last step applied is kept in `settings` under `app_schema`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..store import Store

MIGRATIONS = [
    # 1: accounts. Feeds, downloads and blocks are shared; what is per user
    # (archived, kept) hangs off users.id.
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT,  -- NULL until password logins exist; the API key logs in as admin
        is_admin INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    """A connection for one request or job. Safe to hand between threads, not to share."""
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def setting(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()


def prepare(path: Path) -> None:
    """Make or upgrade the database: the detector's tables first, then the app's."""
    Store(path).close()  # creates the detector's tables; raises StoreError on a foreign store
    db = connect(path)
    try:
        db.execute("PRAGMA journal_mode = WAL")  # lets pages read while the worker writes
        done = int(setting(db, "app_schema") or 0)
        for step, sql in enumerate(MIGRATIONS[done:], start=done + 1):
            db.executescript(f"BEGIN; {sql}; INSERT OR REPLACE INTO settings VALUES ('app_schema', '{step}'); COMMIT;")
    finally:
        db.close()
