"""What route functions ask for (a database connection, the logged-in user), and what they work out from the request."""

from __future__ import annotations

import logging
import sqlite3
from typing import Iterator

from fastapi import Depends, HTTPException, Request

from ..app import accounts
from ..app.db import connect
from ..app.private_feeds import FEED_FILES

log = logging.getLogger(__name__)


class LoginRequired(Exception):
    pass


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """A connection for the request. Each write commits in its own `transaction` block; a route that needs
    several to land together wraps them in one."""
    db = connect(request.app.state.settings.db_path)
    request.state.db = db  # for what the page's layout shows on every page (templating.active_jobs)
    try:
        yield db
    finally:
        if db.in_transaction:  # a write outside a transaction block: it would hold the write lock until now
            log.error("%s %s left a write uncommitted; rolled back", request.method, request.url.path)
            db.rollback()
        db.close()


def start_session(request: Request, db: sqlite3.Connection, user: sqlite3.Row) -> None:
    request.session.update(uid=user["id"], epoch=accounts.auth_epoch(db), version=user["session_version"])


def current_user(request: Request, db: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row:
    """The logged-in user. A session ends when the API key changes, or when the user's password does."""
    row = None
    uid = request.session.get("uid")
    if uid is not None and request.session.get("epoch") == accounts.auth_epoch(db):
        row = accounts.user(db, uid)
        if row is not None and request.session.get("version", 0) != row["session_version"]:
            row = None
    auth = request.headers.get("authorization", "")
    if row is None and auth[:7].lower() == "bearer " and accounts.check_api_key(db, auth[7:].strip()):
        row = accounts.user(db, accounts.admin_id(db))
    if row is None:
        raise LoginRequired()
    request.state.user = row  # for the nav in base.html
    return row


def admin_user(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
    if not user["is_admin"]:
        raise HTTPException(403, "Only the admin can do that.")
    return user


def wake_worker(request: Request) -> None:
    """Have the worker look at the queue now, after queuing a job."""
    if background := getattr(request.app.state, "background", None):
        background.wake()


def base_url(request: Request) -> str:
    """Where this site is, for links that leave it: the public address if there is one, else the request's."""
    return request.app.state.settings.public_url or str(request.base_url).rstrip("/")


def feed_url(request: Request, token: str, by_download: bool = False) -> str:
    """The address of the private feed whose token this is."""
    return f"{base_url(request)}/p/{token}/{FEED_FILES[by_download]}"
