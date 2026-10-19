"""What route functions ask for: a database connection and the logged-in user."""

from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import Depends, Request

from . import accounts
from .db import connect


class LoginRequired(Exception):
    pass


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    db = connect(request.app.state.settings.db_path)
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row:
    uid = request.session.get("uid")
    if uid is not None and request.session.get("epoch") == accounts.auth_epoch(db):
        row = accounts.user(db, uid)
        if row is not None:
            return row
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer " and accounts.check_api_key(db, auth[7:].strip()):
        return accounts.user(db, accounts.admin_id(db))
    raise LoginRequired()
