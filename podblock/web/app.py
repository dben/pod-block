"""The FastAPI app: management pages behind a login, and (later) the public feed routes."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import accounts
from .config import Settings
from .db import connect, prepare, setting

HERE = Path(__file__).parent
SESSION_DAYS = 30
FAILED_LOGIN_DELAY = 1.0  # seconds, to slow down guessing


class LoginRequired(Exception):
    pass


def _static_versions(root: Path) -> dict[str, str]:
    """A short content hash per static file, so browsers drop old copies when one changes."""
    return {str(p.relative_to(root)): hashlib.sha1(p.read_bytes()).hexdigest()[:10]
            for p in root.rglob("*") if p.is_file()}


def init(settings: Settings) -> tuple[str, str | None]:
    """Get the database and keys ready. Returns the cookie signing key, and a new API key if one was made."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    prepare(settings.db_path)
    db = connect(settings.db_path)
    try:
        accounts.admin_id(db)
        made = None
        if settings.api_key:
            accounts.set_api_key(db, settings.api_key)
        elif setting(db, "api_key_hash") is None:
            made = accounts.new_api_key(db)
        return settings.secret_key or accounts.secret_key(db), made
    finally:
        db.close()


def safe_next(target: str | None) -> str:
    """Only redirect within this site after login."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    secret, made = init(settings)
    if made:
        print(f"\n  pod-block API key (shown once; `podblock api-key` makes a new one):\n\n    {made}\n",
              file=sys.stderr, flush=True)

    app = FastAPI(title="pod-block", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="podblock_session",
                       max_age=SESSION_DAYS * 86400, same_site="lax", https_only=settings.secure_cookies)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    versions = _static_versions(HERE / "static")
    templates.env.globals["static"] = lambda name: f"/static/{name}?v={versions.get(name, '0')}"

    def page(request: Request, name: str, status_code: int = 200, **context) -> Response:
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def get_db() -> Iterator[sqlite3.Connection]:
        db = connect(settings.db_path)
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

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        target = "/login?next=" + quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": target})
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(target, status_code=303)
        return JSONResponse({"detail": "login required"}, status_code=401)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz(db: sqlite3.Connection = Depends(get_db)) -> dict:
        db.execute("SELECT 1").fetchone()
        return {"ok": True}

    @app.get("/login")
    def login_form(request: Request, next: str = "/") -> Response:
        return page(request, "login.html", title="Log in", next=safe_next(next))

    @app.post("/login")
    async def login(request: Request, key: str = Form(""), next: str = Form("/"),
                    db: sqlite3.Connection = Depends(get_db)) -> Response:
        if not accounts.check_api_key(db, key.strip()):
            await asyncio.sleep(FAILED_LOGIN_DELAY)
            return page(request, "login.html", status_code=401, title="Log in", next=safe_next(next),
                        error="That key didn't work.")
        request.session.clear()
        request.session.update(uid=accounts.admin_id(db), epoch=accounts.auth_epoch(db))
        return RedirectResponse(safe_next(next), status_code=303)

    @app.post("/logout")
    def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/")
    def home(request: Request, user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
        podcasts = db.execute(
            """
            SELECT p.id, p.name, p.feed_url,
                   (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id = p.id) AS episodes,
                   (SELECT COUNT(*) FROM segments s WHERE s.podcast_id = p.id) AS blocks
            FROM podcasts p ORDER BY p.name COLLATE NOCASE
            """
        ).fetchall()
        return page(request, "home.html", title="Podcasts", user=user, podcasts=podcasts, nav="podcasts")

    return app
