"""Pages for following feeds and going through their episodes."""

from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

import segno
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import feeds, jobs, library, publish
from .episodes import wake_worker
from .deps import current_user, get_db
from .templating import flash, page

PAGE_SIZE = 50
VIEWS = {"active": (False, False), "archived": (True, False), "downloaded": (False, True)}  # (archived, downloaded)
RETENTION_DAYS = (0, 3650)
REFRESH_MINUTES = (15, 7 * 24 * 60)

router = APIRouter()


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _podcast_or_404(db: sqlite3.Connection, podcast_id: int) -> sqlite3.Row:
    pod = library.podcast(db, podcast_id)
    if pod is None:
        raise HTTPException(404, "No such podcast")
    return pod


def _home(request: Request, user, db: sqlite3.Connection, status_code: int = 200, **context) -> Response:
    podcasts = library.podcasts(db, user["id"])
    return page(request, "home.html", status_code=status_code, title="Podcasts", nav="podcasts", user=user,
                podcasts=podcasts, expired_total=sum(p["expired_count"] for p in podcasts), **context)


@router.get("/")
def home(request: Request, user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _home(request, user, db)


@router.post("/episodes/archive-expired")
def archive_all_expired(request: Request, user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Archive every cleaned-up episode, of every podcast, for this user."""
    changed = library.archive_expired(db, user["id"])
    flash(request, f"Archived {len(changed):,} cleaned-up {'episode' if len(changed) == 1 else 'episodes'}.")
    return RedirectResponse("/", status_code=303)


@router.post("/podcasts")
def add_podcast(request: Request, url: str = Form(""), user=Depends(current_user),
                db: sqlite3.Connection = Depends(get_db)) -> Response:
    try:
        result = feeds.add_podcast(db, url)
    except feeds.RefreshError as e:
        return _home(request, user, db, status_code=422, url=url, error=f"Couldn't add that feed: {e}.")
    flash(request, f"Added, with {result.added:,} episodes." if result.added else "Added.")
    return RedirectResponse(f"/podcasts/{result.podcast_id}", status_code=303)


@router.get("/podcasts/{podcast_id}")
def show_podcast(request: Request, podcast_id: int, view: str = "active", q: str = "",
                 page_no: int = Query(1, alias="page", ge=1), partial: bool = False,
                 user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    pod = _podcast_or_404(db, podcast_id)
    view = view if view in VIEWS else "active"
    archived, downloaded = VIEWS[view]
    rows, total = library.episodes(db, user["id"], podcast_id, archived, q, limit=PAGE_SIZE,
                                   offset=(page_no - 1) * PAGE_SIZE, downloaded=downloaded)
    more = page_no * PAGE_SIZE < total
    query = {k: v for k, v in (("view", view if view != "active" else ""), ("q", q)) if v}
    context = dict(
        podcast=pod, episodes=rows, total=total, view=view, q=q, page_no=page_no,
        next_url=f"/podcasts/{podcast_id}?{urlencode({**query, 'page': page_no + 1})}" if more else None,
        here=f"/podcasts/{podcast_id}" + (f"?{urlencode(query)}" if query else ""),
    )
    if partial:
        return page(request, "_episodes.html", **context)
    private = publish.feed_url(request, publish.ensure_token(db, podcast_id))
    return page(request, "podcast.html", title=pod["name"], nav="podcasts", user=user,
                counts=library.counts(db, user["id"], podcast_id), private_url=private,
                private_count=len(publish.published(db, podcast_id)),
                private_qr=segno.make(private, error="m").svg_inline(scale=4, border=2, dark="#0d0f13", light="#ffffff"),
                **context)


@router.post("/podcasts/{podcast_id}/settings")
def podcast_settings(request: Request, podcast_id: int, retention_days: int = Form(...),
                     refresh_minutes: int = Form(...), user=Depends(current_user),
                     db: sqlite3.Connection = Depends(get_db)) -> Response:
    _podcast_or_404(db, podcast_id)
    if not (RETENTION_DAYS[0] <= retention_days <= RETENTION_DAYS[1]
            and REFRESH_MINUTES[0] <= refresh_minutes <= REFRESH_MINUTES[1]):
        flash(request, "Those settings are out of range.", "error")
    else:
        db.execute("UPDATE podcasts SET retention_days = ?, refresh_minutes = ? WHERE id = ?",
                   (retention_days, refresh_minutes, podcast_id))
        db.commit()
        flash(request, "Saved.")
    return RedirectResponse(f"/podcasts/{podcast_id}", status_code=303)


@router.post("/podcasts/{podcast_id}/feed-token")
def new_feed_address(request: Request, podcast_id: int, user=Depends(current_user),
                     db: sqlite3.Connection = Depends(get_db)) -> Response:
    _podcast_or_404(db, podcast_id)
    publish.new_feed_token(db, podcast_id)
    flash(request, "The private feed has a new address. The old one no longer works.")
    return RedirectResponse(f"/podcasts/{podcast_id}?feed=1", status_code=303)


@router.post("/podcasts/{podcast_id}/refresh")
def refresh_podcast(request: Request, podcast_id: int, user=Depends(current_user),
                    db: sqlite3.Connection = Depends(get_db)) -> Response:
    _podcast_or_404(db, podcast_id)
    try:
        flash(request, feeds.refresh(db, podcast_id, conditional=False).describe())
    except feeds.RefreshError as e:
        flash(request, f"Refresh failed: {e}.", "error")
    return RedirectResponse(f"/podcasts/{podcast_id}", status_code=303)


@router.post("/podcasts/{podcast_id}/episodes")
def change_episodes(request: Request, podcast_id: int, action: str = Form(...), ids: list[int] = Form([]),
                    next: str = Form(""), user=Depends(current_user),
                    db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Archive, unarchive, keep, unkeep or download a selection; archive one episode and everything before it,
    or every cleaned-up one."""
    _podcast_or_404(db, podcast_id)
    if action in {"download", "reprocess"}:
        mine = [r["id"] for r in db.execute(
            f"SELECT id FROM episodes WHERE podcast_id = ? AND id IN ({', '.join('?' * len(ids))})",
            (podcast_id, *ids))] if ids else []
        changed = jobs.enqueue(db, "process", mine, user["id"], skip_ready=action == "download")
        if changed:
            wake_worker(request)
        if _wants_json(request):
            return JSONResponse({"action": action, "changed": changed})
        skipped = len(mine) - len(changed)
        note = f" {skipped:,} already ready or queued." if skipped else ""
        flash(request, f"Queued {len(changed):,} {'episode' if len(changed) == 1 else 'episodes'}.{note}"
              if changed else "Those are already ready or queued.")
        back = next if next.startswith(f"/podcasts/{podcast_id}") else f"/podcasts/{podcast_id}"
        return RedirectResponse(back, status_code=303)
    if action == "archive_older" and ids:
        changed = library.archive_older(db, user["id"], podcast_id, ids[0])
        verb = "Archived"
    elif action == "archive_expired":
        changed = library.archive_expired(db, user["id"], podcast_id)
        verb = "Archived"
    elif action in {"keep", "unkeep"}:
        changed = library.set_kept(db, user["id"], podcast_id, ids, action == "keep")
        verb = "Keeping" if action == "keep" else "No longer keeping"
    elif action in {"archive", "unarchive"}:
        changed = library.set_archived(db, user["id"], podcast_id, ids, action == "archive")
        verb = "Archived" if action == "archive" else "Unarchived"
    else:
        raise HTTPException(400, "Unknown action")
    if _wants_json(request):
        return JSONResponse({"action": action, "changed": changed})
    flash(request, f"{verb} {len(changed):,} {'episode' if len(changed) == 1 else 'episodes'}.")
    back = next if next.startswith(f"/podcasts/{podcast_id}") else f"/podcasts/{podcast_id}"
    return RedirectResponse(back, status_code=303)


@router.post("/podcasts/{podcast_id}/remove")
def remove_podcast(request: Request, podcast_id: int, user=Depends(current_user),
                   db: sqlite3.Connection = Depends(get_db)) -> Response:
    pod = _podcast_or_404(db, podcast_id)
    stayed = feeds.remove_podcast(db, podcast_id)
    note = f" Its {stayed:,} downloaded episodes stay." if stayed else ""
    flash(request, f"Removed {pod['name']}.{note}")
    return RedirectResponse("/", status_code=303)
