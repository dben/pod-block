"""Templates, the filters they use, and one-off messages carried across a redirect."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from . import jobs
from .db import connect
from .feeds import html_to_text

HERE = Path(__file__).parent
STATIC = HERE / "static"

templates = Jinja2Templates(directory=HERE / "templates")


def _static_versions(root: Path) -> dict[str, str]:
    """A short content hash per static file, so browsers drop old copies when one changes."""
    return {str(p.relative_to(root)): hashlib.sha1(p.read_bytes()).hexdigest()[:10]
            for p in root.rglob("*") if p.is_file()}


_versions = _static_versions(STATIC)


def static(name: str) -> str:
    return f"/static/{name}?v={_versions.get(name, '0')}"


def page(request: Request, name: str, status_code: int = 200, **context) -> Response:
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def flash(request: Request, text: str, kind: str = "ok") -> None:
    """A message for the next page shown, e.g. after a redirect."""
    request.session["flash"] = [*request.session.get("flash", []), [kind, text]]


def take_flashes(request: Request) -> list[list[str]]:
    return request.session.pop("flash", [])


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    secs = time.time() - ts
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} hr ago"
    if secs < 7 * 86400:
        days = int(secs // 86400)
        return f"{days} {'day' if days == 1 else 'days'} ago"
    return day(ts)


def day(ts: float | None) -> str:
    if not ts:
        return ""
    when = datetime.fromtimestamp(ts, timezone.utc)
    this_year = when.year == datetime.now(timezone.utc).year
    return f"{when:%b} {when.day}" + ("" if this_year else f", {when.year}")


def duration(secs: float | None) -> str:
    if not secs:
        return ""
    mins = round(secs / 60)
    if mins < 60:
        return f"{max(mins, 1)} min"
    return f"{mins // 60} hr {mins % 60} min" if mins % 60 else f"{mins // 60} hr"


def web_url(url: str | None) -> str | None:
    """Only http(s) links from a feed make it into a page."""
    return url if url and url.split(":", 1)[0].lower() in {"http", "https"} else None


def clock(secs: float | None) -> str:
    secs = int(round(secs or 0))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def active_jobs(request: Request) -> int:
    db = connect(request.app.state.settings.db_path)
    try:
        return jobs.active_count(db)
    finally:
        db.close()


def expires(ep, podcast=None) -> float | None:
    """When a ready episode will be cleaned up, or None if it won't (kept, or the podcast keeps everything)."""
    days = podcast["retention_days"] if podcast is not None else ep["retention_days"]
    if ep["state"] != "ready" or ep["kept_any"] or not days or not ep["ready_at"]:
        return None
    return ep["ready_at"] + days * 86400


def until(ts: float | None) -> str:
    """How long until a time: "in 3 days", "in 5 hr", "soon"."""
    if not ts:
        return ""
    secs = ts - time.time()
    if secs < 3600:
        return "soon"
    if secs < 86400:
        return f"in {int(secs // 3600)} hr"
    days = round(secs / 86400)
    return f"in {days} {'day' if days == 1 else 'days'}"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


templates.env.globals.update(static=static, flashes=take_flashes, active_jobs=active_jobs, expires=expires)
templates.env.filters.update(until=until, ago=ago, day=day, duration=duration, clock=clock, web_url=web_url, plural=plural,
                             text=html_to_text, fromjson=lambda text: json.loads(text) if text else [])
