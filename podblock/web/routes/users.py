"""Your own account, and the admin's page for managing users."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from ...app import accounts
from ..deps import admin_user, current_user, get_db, start_session
from ..templating import flash, page

router = APIRouter()


@router.get("/account")
def account(request: Request, user=Depends(current_user)) -> Response:
    return page(request, "account.html", title="Account", nav="account", user=user)


@router.post("/account/password")
def change_password(request: Request, current: str = Form(""), new: str = Form(""), repeat: str = Form(""),
                    user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Change your own password. Your other sessions end; this one carries on."""
    def again(error: str) -> Response:
        return page(request, "account.html", status_code=422, title="Account", nav="account", user=user, error=error)

    if new != repeat:
        return again("The new passwords don't match.")
    try:
        accounts.change_password(db, user["id"], current, new)
    except accounts.AccountError as e:
        return again(str(e))
    start_session(request, db, accounts.user(db, user["id"]))
    flash(request, "Password changed. You're logged out everywhere else.")
    return RedirectResponse("/account", status_code=303)


def _users_page(request: Request, user, db: sqlite3.Connection, status_code: int = 200, **context) -> Response:
    return page(request, "users.html", status_code=status_code, title="Users", nav="users", user=user,
                users=accounts.users(db), min_password=accounts.MIN_PASSWORD, **context)


@router.get("/users")
def users_page(request: Request, user=Depends(admin_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _users_page(request, user, db)


@router.post("/users")
def add_user(request: Request, username: str = Form(""), password: str = Form(""), user=Depends(admin_user),
             db: sqlite3.Connection = Depends(get_db)) -> Response:
    try:
        accounts.create_user(db, username, password)
    except accounts.AccountError as e:
        return _users_page(request, user, db, status_code=422, error=str(e), username=username)
    flash(request, f"Added {accounts.clean_username(username)}.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/password")
def reset_password(request: Request, user_id: int, password: str = Form(""), user=Depends(admin_user),
                   db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Give someone a new password; they are logged out until they use it."""
    target = accounts.user(db, user_id)
    try:
        accounts.set_password(db, user_id, password)
    except accounts.AccountError as e:
        flash(request, str(e), "error")
    else:
        flash(request, f"New password set for {target['username']}. They're logged out until they use it.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/remove")
def remove_user(request: Request, user_id: int, user=Depends(admin_user),
                db: sqlite3.Connection = Depends(get_db)) -> Response:
    target = accounts.user(db, user_id)
    try:
        accounts.remove_user(db, user_id)
    except accounts.AccountError as e:
        flash(request, str(e), "error")
    else:
        flash(request, f"Removed {target['username']}.")
    return RedirectResponse("/users", status_code=303)
