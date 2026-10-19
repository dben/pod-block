"""Login keys and users.

Round one has one login: an API key. Only its hash is stored. It logs in
as the `admin` user, so per-user state already has a user to belong to
when username and password logins arrive.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time

from .db import set_setting, setting

ADMIN = "admin"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key(db: sqlite3.Connection) -> str:
    """Make and store a new API key, replacing the old one and logging everyone out."""
    key = secrets.token_urlsafe(24)
    set_api_key(db, key)
    return key


def set_api_key(db: sqlite3.Connection, key: str) -> None:
    if setting(db, "api_key_hash") != _hash(key):
        set_setting(db, "api_key_hash", _hash(key))
        set_setting(db, "auth_epoch", str(int(setting(db, "auth_epoch") or 0) + 1))


def check_api_key(db: sqlite3.Connection, key: str) -> bool:
    stored = setting(db, "api_key_hash")
    return bool(stored and key) and hmac.compare_digest(stored, _hash(key))


def auth_epoch(db: sqlite3.Connection) -> int:
    """Goes up whenever the API key changes; sessions from an older epoch are void."""
    return int(setting(db, "auth_epoch") or 0)


def secret_key(db: sqlite3.Connection) -> str:
    """The key that signs session cookies, made on first use."""
    key = setting(db, "secret_key")
    if key is None:
        key = secrets.token_urlsafe(32)
        set_setting(db, "secret_key", key)
    return key


def admin_id(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT id FROM users WHERE username = ?", (ADMIN,)).fetchone()
    if row:
        return row["id"]
    cur = db.execute("INSERT INTO users (username, is_admin, created_at) VALUES (?, 1, ?)",
                     (ADMIN, time.time()))
    db.commit()
    return cur.lastrowid


def user(db: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
