"""The web app's tables, kept in the detector's own database.

The detector's `Store` makes `settings`, `podcasts`, `episodes` and the block
tables. The app adds its own tables, and its own columns on `podcasts` and
`episodes`, through the numbered steps in MIGRATIONS: SQL scripts, or
functions given the connection. The last step applied is kept in
`settings` under `app_schema`.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime
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
    # 2: feeds. What the app knows about a show and its episodes beyond what the
    # detector needs, and each user's archived and kept flags.
    """
    ALTER TABLE podcasts ADD COLUMN author TEXT;
    ALTER TABLE podcasts ADD COLUMN description TEXT;
    ALTER TABLE podcasts ADD COLUMN image_url TEXT;
    ALTER TABLE podcasts ADD COLUMN link TEXT;
    ALTER TABLE podcasts ADD COLUMN language TEXT;
    ALTER TABLE podcasts ADD COLUMN categories TEXT;  -- JSON list
    ALTER TABLE podcasts ADD COLUMN explicit INTEGER;
    ALTER TABLE podcasts ADD COLUMN channel_xml TEXT;  -- the <channel> without items, for the private feed
    ALTER TABLE podcasts ADD COLUMN followed INTEGER NOT NULL DEFAULT 1;  -- 0 once removed in the app
    ALTER TABLE podcasts ADD COLUMN refresh_minutes INTEGER NOT NULL DEFAULT 60;
    ALTER TABLE podcasts ADD COLUMN feed_etag TEXT;
    ALTER TABLE podcasts ADD COLUMN feed_last_modified TEXT;
    ALTER TABLE podcasts ADD COLUMN last_refreshed_at REAL;
    ALTER TABLE podcasts ADD COLUMN last_error TEXT;
    ALTER TABLE podcasts ADD COLUMN added_at REAL;

    ALTER TABLE episodes ADD COLUMN pub_ts REAL;  -- pub_date as a timestamp, for sorting
    ALTER TABLE episodes ADD COLUMN summary TEXT;  -- start of the show notes, as plain text
    ALTER TABLE episodes ADD COLUMN image_url TEXT;
    ALTER TABLE episodes ADD COLUMN link TEXT;
    ALTER TABLE episodes ADD COLUMN audio_type TEXT;
    ALTER TABLE episodes ADD COLUMN audio_length INTEGER;
    ALTER TABLE episodes ADD COLUMN feed_duration REAL;  -- as the feed states it; `duration` is measured
    ALTER TABLE episodes ADD COLUMN season INTEGER;
    ALTER TABLE episodes ADD COLUMN episode_number INTEGER;
    ALTER TABLE episodes ADD COLUMN episode_type TEXT;
    ALTER TABLE episodes ADD COLUMN explicit INTEGER;
    ALTER TABLE episodes ADD COLUMN item_xml TEXT;  -- the raw <item>, for show notes and the private feed
    ALTER TABLE episodes ADD COLUMN in_feed INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE episodes ADD COLUMN ever_downloaded INTEGER NOT NULL DEFAULT 0;  -- kept when it leaves the feed
    CREATE INDEX episodes_podcast_pub ON episodes(podcast_id, pub_ts);

    UPDATE episodes SET ever_downloaded = 1
    WHERE fingerprint_path IS NOT NULL
       OR id IN (SELECT episode_id FROM occurrences) OR id IN (SELECT episode_id FROM reports);

    CREATE TABLE user_episodes (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
        archived INTEGER NOT NULL DEFAULT 0,  -- hidden from this user's list
        kept INTEGER NOT NULL DEFAULT 0,  -- never expires while this user keeps it
        PRIMARY KEY (user_id, episode_id)
    );
    """,
    # 3: timestamps for the publish dates the detector stored
    lambda db: db.executemany("UPDATE episodes SET pub_ts = ? WHERE id = ?", [
        (ts, r["id"]) for r in db.execute("SELECT id, pub_date FROM episodes WHERE pub_date IS NOT NULL")
        if (ts := iso_ts(r["pub_date"])) is not None]),
    # 4: processing. Each episode's place in it, the files it made, and the jobs.
    """
    ALTER TABLE episodes ADD COLUMN state TEXT;  -- NULL, queued, running, ready, failed
    ALTER TABLE episodes ADD COLUMN state_note TEXT;  -- what it is doing, or why it failed
    ALTER TABLE episodes ADD COLUMN original_path TEXT;  -- the download, under the media folder
    ALTER TABLE episodes ADD COLUMN cut_path TEXT;  -- the audio with repeats cut out
    ALTER TABLE episodes ADD COLUMN cut_type TEXT;
    ALTER TABLE episodes ADD COLUMN cut_duration REAL;
    ALTER TABLE episodes ADD COLUMN cut_bytes INTEGER;
    ALTER TABLE episodes ADD COLUMN processed_at REAL;

    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,  -- process
        episode_id INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
        state TEXT NOT NULL DEFAULT 'queued',  -- queued, running, done, failed, cancelled
        step TEXT,  -- what it is doing now
        progress REAL,  -- 0 to 1 through the step, when known
        error TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL
    );
    CREATE INDEX jobs_state ON jobs(state, id);
    CREATE INDEX jobs_episode ON jobs(episode_id);
    """,
    # 5: the timeline editor. Sections cut by hand, what a job is asked to change,
    # the waveform, and what each cut file was made from (so unchanged ones aren't redone).
    """
    ALTER TABLE jobs ADD COLUMN payload TEXT;  -- JSON, for jobs that need more than an episode
    ALTER TABLE episodes ADD COLUMN cut_spec TEXT;  -- JSON list of the [start, end] kept in the cut file
    ALTER TABLE episodes ADD COLUMN peaks_path TEXT;  -- the waveform, as JSON
    CREATE TABLE edits (
        id INTEGER PRIMARY KEY,
        episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
        start REAL NOT NULL,
        end REAL NOT NULL,
        created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at REAL NOT NULL
    );
    CREATE INDEX edits_episode ON edits(episode_id);
    """,
    # 6: the private feed: a random token per podcast, which is the feed's address
    "ALTER TABLE podcasts ADD COLUMN public_token TEXT; CREATE UNIQUE INDEX podcasts_token ON podcasts(public_token);",
    lambda db: db.executemany("UPDATE podcasts SET public_token = ? WHERE id = ?",
                              [(new_token(), r["id"]) for r in db.execute("SELECT id FROM podcasts")]),
    # 7: retention. Downloads are deleted a set time after they were first made ready, unless kept.
    """
    ALTER TABLE podcasts ADD COLUMN retention_days INTEGER NOT NULL DEFAULT 7;  -- 0 keeps downloads for good
    ALTER TABLE episodes ADD COLUMN ready_at REAL;  -- first made ready since its download: retention counts from here
    ALTER TABLE episodes ADD COLUMN expired_at REAL;  -- when retention deleted its audio (state 'expired')
    UPDATE episodes SET ready_at = processed_at WHERE state = 'ready';
    """,
    # 8: more per-podcast settings
    """
    ALTER TABLE podcasts ADD COLUMN auto_download INTEGER NOT NULL DEFAULT 0;  -- download new episodes as they appear
    ALTER TABLE podcasts ADD COLUMN fill_gaps REAL NOT NULL DEFAULT 4;  -- seconds of new audio between cuts cut too
    """,
]


def new_token() -> str:
    """128 random bits, URL-safe."""
    return secrets.token_urlsafe(16)


def iso_ts(text: str | None) -> float | None:
    try:
        return datetime.fromisoformat(text).timestamp() if text else None
    except ValueError:
        return None


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
        for step, change in enumerate(MIGRATIONS[done:], start=done + 1):
            mark = f"INSERT OR REPLACE INTO settings VALUES ('app_schema', '{step}');"
            if callable(change):
                change(db)
                db.execute(mark)
                db.commit()
            else:
                db.executescript(f"BEGIN; {change}; {mark} COMMIT;")
    finally:
        db.close()
