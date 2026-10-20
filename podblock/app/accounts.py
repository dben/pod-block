"""Login keys and users.

The admin logs in with an API key; only its hash is stored. Everyone else
has a user name and a password, which the admin sets and they can change.
Passwords are stored as scrypt hashes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time

from .db import set_setting, setting, transaction

ADMIN = "admin"
USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
MIN_PASSWORD = 8
MAX_PASSWORD = 1024
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}  # about 16 MB and 50 ms a hash


class AccountError(ValueError):
    """A user can't be made or changed; the message says why, for the page."""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key(db: sqlite3.Connection) -> str:
    """Make and store a new API key, replacing the old one and logging everyone out."""
    key = secrets.token_urlsafe(24)
    set_api_key(db, key)
    return key


def set_api_key(db: sqlite3.Connection, key: str) -> None:
    with transaction(db):
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
    with transaction(db):
        key = setting(db, "secret_key")
        if key is None:
            key = secrets.token_urlsafe(32)
            set_setting(db, "secret_key", key)
    return key


def admin_id(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT id FROM users WHERE username = ?", (ADMIN,)).fetchone()
    if row:
        return row["id"]
    with transaction(db):
        row = db.execute("SELECT id FROM users WHERE username = ?", (ADMIN,)).fetchone()
        if row:  # made since the first look
            return row["id"]
        now = time.time()
        cur = db.execute("INSERT INTO users (username, is_admin, created_at) VALUES (?, 1, ?)", (ADMIN, now))
        # podcasts the CLI added before the app first ran start out on the admin's home page
        db.execute("INSERT INTO user_podcasts (user_id, podcast_id, added_at) SELECT ?, id, ? FROM podcasts "
                   "WHERE followed = 1", (cur.lastrowid, now))
    return cur.lastrowid


def user(db: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def users(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Everyone, the admin first, with how many podcasts each has picked and episodes each has grabbed."""
    return db.execute(
        """SELECT u.*, (SELECT COUNT(*) FROM user_podcasts up JOIN podcasts p ON p.id = up.podcast_id
                        WHERE up.user_id = u.id AND p.followed = 1) AS podcast_count,
                  (SELECT COUNT(*) FROM grabs g WHERE g.user_id = u.id) AS grab_count,
                  (SELECT MAX(last_at) FROM grabs g WHERE g.user_id = u.id) AS grabbed_at
           FROM users u ORDER BY u.username != ?, u.username""", (ADMIN,)).fetchall()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return "$".join(["scrypt", *(str(SCRYPT[k]) for k in "nrp"),
                     base64.b64encode(salt).decode(), base64.b64encode(digest).decode()])


def check_password(stored: str | None, password: str) -> bool:
    try:
        kind, n, r, p, salt, digest = (stored or "").split("$")
        if kind != "scrypt":
            return False
        found = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p))
    except ValueError:
        return False
    return hmac.compare_digest(found, base64.b64decode(digest))


_DUMMY = hash_password(secrets.token_urlsafe(16))  # checked for unknown names, so they take as long as known ones


def clean_username(name: str) -> str:
    return name.strip().lower()


def _check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Passwords need at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AccountError("That password is too long.")


def create_user(db: sqlite3.Connection, username: str, password: str) -> int:
    name = clean_username(username)
    if name == ADMIN:
        raise AccountError("That name is the admin's.")
    if not USERNAME.fullmatch(name):
        raise AccountError("User names are 1 to 40 letters, digits, dots, dashes or underscores.")
    _check_new_password(password)
    hashed = hash_password(password)
    try:
        with transaction(db):
            cur = db.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                             (name, hashed, time.time()))
    except sqlite3.IntegrityError:
        raise AccountError(f"There is already a user called {name}.") from None
    return cur.lastrowid


def set_password(db: sqlite3.Connection, user_id: int, password: str) -> None:
    """Give a user a new password, ending their sessions."""
    row = user(db, user_id)
    if row is None:
        raise AccountError("There is no such user.")
    if row["username"] == ADMIN:
        raise AccountError("The admin logs in with the API key; `podblock api-key` makes a new one.")
    _check_new_password(password)
    hashed = hash_password(password)
    with transaction(db):
        db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE id = ?",
                   (hashed, user_id))


def remove_user(db: sqlite3.Connection, user_id: int) -> None:
    """Delete a user with their archived, kept and picked podcasts, and their filters. Jobs and edits they made
    stay, and so do their filters from processing, since those apply to everyone."""
    row = user(db, user_id)
    if row is None:
        raise AccountError("There is no such user.")
    if row["username"] == ADMIN:
        raise AccountError("The admin can't be removed.")
    with transaction(db):
        db.execute("DELETE FROM episode_filters WHERE user_id = ? AND processing = 0", (user_id,))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))


def log_in(db: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    """The user these details belong to, or None. The admin's password is the API key."""
    name = clean_username(username)
    if name == ADMIN:
        return user(db, admin_id(db)) if check_api_key(db, password.strip()) else None
    row = db.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
    if row is None or row["password_hash"] is None:
        check_password(_DUMMY, password)
        return None
    return row if check_password(row["password_hash"], password) else None


def logged_in(db: sqlite3.Connection, user_id: int) -> None:
    with transaction(db):
        db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (time.time(), user_id))
