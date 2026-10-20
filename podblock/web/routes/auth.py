"""Logging in and out."""

from __future__ import annotations

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ...app import accounts
from ..deps import get_db, start_session
from ..templating import page

FAILED_LOGIN_DELAY = 1.0  # seconds, to slow down guessing

router = APIRouter()


def safe_next(target: str | None) -> str:
    """Only redirect within this site after login."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


@router.get("/login")
def login_form(request: Request, next: str = "/") -> Response:
    return page(request, "login.html", title="Log in", next=safe_next(next))


@router.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""), key: str = Form(""),
                next: str = Form("/"), db: sqlite3.Connection = Depends(get_db)) -> Response:
    # `key` alone is the old form, kept for scripts: the API key, logging in as the admin
    name, secret = (accounts.ADMIN, key) if key else (username, password)
    found = await run_in_threadpool(accounts.log_in, db, name, secret)
    if found is None:
        await asyncio.sleep(FAILED_LOGIN_DELAY)
        return page(request, "login.html", status_code=401, title="Log in", next=safe_next(next),
                    username=username, error="That user name and password didn't work.")
    await run_in_threadpool(_start, request, db, found)  # it writes, so not on the event loop
    return RedirectResponse(safe_next(next), status_code=303)


def _start(request: Request, db: sqlite3.Connection, user: sqlite3.Row) -> None:
    accounts.logged_in(db, user["id"])
    request.session.clear()
    start_session(request, db, user)


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
