"""The FastAPI app: management pages behind a login, and (later) the public feed routes."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import accounts, podcasts
from .background import Background
from .config import Settings
from .db import connect, prepare, setting
from .deps import LoginRequired, get_db
from .templating import STATIC, page

SESSION_DAYS = 30
FAILED_LOGIN_DELAY = 1.0  # seconds, to slow down guessing


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        background = Background(settings.db_path) if settings.background else None
        if background:
            background.start()
        yield
        if background:
            background.stop()

    app = FastAPI(title="pod-block", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="podblock_session",
                       max_age=SESSION_DAYS * 86400, same_site="lax", https_only=settings.secure_cookies)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(podcasts.router)

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

    return app
