"""Pages for following feeds and going through their episodes."""

from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

import segno
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ...app import directory, feeds, filters, jobs, library, private_feeds, storage
from ...app.db import transaction
from ..deps import admin_user, current_user, feed_url, get_db, wake_worker
from ..templating import flash, page

PAGE_SIZE = 50
VIEWS = {"active": (False, False), "archived": (True, False), "downloaded": (False, True),  # (archived, downloaded)
         "grabbed": (False, False), "filtered": (False, False)}
TABS = ("episodes", "feed", "filters", "settings")
RETENTION_DAYS = (0, 3650)
REFRESH_MINUTES = (15, 7 * 24 * 60)
FILL_GAPS = (0, 120)

router = APIRouter()


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _podcast_or_404(db: sqlite3.Connection, podcast_id: int) -> sqlite3.Row:
    pod = library.podcast(db, podcast_id)
    if pod is None:
        raise HTTPException(404, "No such podcast")
    return pod


def _qr(url: str) -> str:
    return segno.make(url, error="m").svg_inline(scale=4, border=2, dark="#0d0f13", light="#ffffff")


def _addresses(request: Request, token: str) -> list[dict]:
    """A feed's two addresses, newest released first and newest downloaded first, each with its QR code."""
    out = []
    for key, label, by_download in (("released", "By release date", False), ("downloaded", "By download date", True)):
        url = feed_url(request, token, by_download)
        out.append({"key": key, "label": label, "url": url, "qr": _qr(url)})
    return out


def _home(request: Request, user, db: sqlite3.Connection, view: str = "mine", status_code: int = 200,
          search: str = "", **context) -> Response:
    view = view if view in {"mine", "all"} else "mine"
    if search := search.strip():
        try:
            found = directory.search(search)
            context.update(found=found, known=library.by_feed(db, user["id"], [s.feed_url for s in found]))
        except directory.DirectoryError as e:
            context.update(found=[], known={}, error=f"Couldn't search: {e}.")
        context.setdefault("url", search)
    shown = library.podcasts(db, user["id"], mine=view == "mine")
    everything = db.execute("SELECT COUNT(*) FROM podcasts WHERE followed = 1").fetchone()[0]
    mine = len(shown) if view == "mine" else sum(p["subscribed"] for p in shown)
    expired_total = sum(p["expired_count"] for p in shown if p["subscribed"])
    return page(request, "home.html", status_code=status_code, title="Podcasts", nav="podcasts", user=user,
                podcasts=shown, view=view, mine_count=mine, all_count=everything, expired_total=expired_total,
                omni_addresses=_addresses(request, private_feeds.omni_token(db, user["id"])),
                omni_count=len(private_feeds.omni_published(db, user["id"])),
                space=storage.space(db, request.app.state.settings.media_dir), search=search, **context)


@router.get("/")
def home(request: Request, view: str = "mine", search: str = "", user=Depends(current_user),
         db: sqlite3.Connection = Depends(get_db)) -> Response:
    return _home(request, user, db, view, search=search)


@router.post("/podcasts/{podcast_id}/subscribe")
def subscribe(request: Request, podcast_id: int, on: bool = Form(...), next: str = Form(""),
              user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Put a podcast on your home page, or take it off. Nobody else's list changes."""
    pod = _podcast_or_404(db, podcast_id)
    library.subscribe(db, user["id"], podcast_id, on)
    if _wants_json(request):
        return JSONResponse({"subscribed": on})
    flash(request, f"{pod['name']} is {'on' if on else 'off'} your home page.")
    back = next if next in {"/", "/?view=all"} or next.startswith(f"/podcasts/{podcast_id}") else "/"
    return RedirectResponse(back, status_code=303)


@router.post("/omni-token")
def new_omni_address(request: Request, user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Give your omni feed a new address."""
    private_feeds.new_omni_token(db, user["id"])
    flash(request, "Your omni feed has a new address. The old one no longer works.")
    return RedirectResponse("/?omni=1", status_code=303)


@router.post("/episodes/archive-expired")
def archive_all_expired(request: Request, user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Archive every cleaned-up episode, of every podcast, for this user."""
    changed = library.archive_expired(db, user["id"])
    flash(request, f"Archived {len(changed):,} cleaned-up {'episode' if len(changed) == 1 else 'episodes'}.")
    return RedirectResponse("/", status_code=303)


@router.post("/podcasts")
def add_podcast(request: Request, url: str = Form(""), user=Depends(current_user),
                db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Follow the feed at a web address, or search the podcast directory for anything else."""
    address = directory.as_address(url)
    if address is None:
        return RedirectResponse(f"/?{urlencode({'search': url.strip()})}" if url.strip() else "/", status_code=303)
    try:
        result = feeds.add_podcast(db, directory.apple_feed(address) or address)
    except (feeds.RefreshError, directory.DirectoryError) as e:
        return _home(request, user, db, status_code=422, url=url, error=f"Couldn't add that feed: {e}.")
    library.subscribe(db, user["id"], result.podcast_id)
    flash(request, f"Added, with {result.added:,} episodes." if result.added else "Added.")
    return RedirectResponse(f"/podcasts/{result.podcast_id}", status_code=303)


@router.get("/podcasts/{podcast_id}")
def show_podcast(request: Request, podcast_id: int, view: str = "active", q: str = "", tab: str = "",
                 page_no: int = Query(1, alias="page", ge=1), partial: bool = False,
                 user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The show's header, then one of its tabs: the episode list, your private feed, filters or settings."""
    pod = _podcast_or_404(db, podcast_id)
    params = request.query_params
    tab = tab if tab in TABS else "feed" if params.get("feed") else "filters" if params.get("filters") else "episodes"
    view = view if view in VIEWS else "active"
    query = {k: v for k, v in (("view", view if view != "active" else ""), ("q", q)) if v}
    context = dict(podcast=pod, view=view, q=q, page_no=page_no, tab=tab,
                   here=f"/podcasts/{podcast_id}" + (f"?{urlencode(query)}" if query else ""))
    if tab == "episodes":
        archived, downloaded = VIEWS[view]
        rows, total = library.episodes(db, user["id"], podcast_id, archived, q, limit=PAGE_SIZE,
                                       offset=(page_no - 1) * PAGE_SIZE, downloaded=downloaded,
                                       filtered=view == "filtered", grabbed=view == "grabbed",
                                       oldest_first=bool(pod["sort_oldest"]))
        more = page_no * PAGE_SIZE < total
        context.update(episodes=rows, total=total,
                       next_url=f"/podcasts/{podcast_id}?{urlencode({**query, 'page': page_no + 1})}" if more else None)
        if partial:
            return page(request, "_episodes.html", **context)
    if tab == "feed":
        context.update(private_addresses=_addresses(request, private_feeds.feed_token(db, user["id"], podcast_id)),
                       private_count=len(private_feeds.published(db, podcast_id, user["id"])))
    cut_sec = db.execute("SELECT COALESCE(SUM(duration - cut_duration), 0) FROM episodes WHERE podcast_id = ? "
                         "AND cut_duration IS NOT NULL", (podcast_id,)).fetchone()[0]
    return page(request, "podcast.html", title=pod["name"], nav="podcasts", user=user, cut_sec=cut_sec,
                subscribed=library.subscribed(db, user["id"], podcast_id),
                filters=filters.for_podcast(db, user["id"], podcast_id),
                counts=library.counts(db, user["id"], podcast_id), **context)


@router.post("/podcasts/{podcast_id}/settings")
def podcast_settings(request: Request, podcast_id: int, retention_days: int = Form(...),
                     refresh_minutes: int = Form(...), fill_gaps: float = Form(4.0), auto_download: bool = Form(False),
                     sort: str = Form("newest"), user=Depends(current_user),
                     db: sqlite3.Connection = Depends(get_db)) -> Response:
    pod = _podcast_or_404(db, podcast_id)
    if not (RETENTION_DAYS[0] <= retention_days <= RETENTION_DAYS[1]
            and REFRESH_MINUTES[0] <= refresh_minutes <= REFRESH_MINUTES[1]
            and FILL_GAPS[0] <= fill_gaps <= FILL_GAPS[1]):
        flash(request, "Those settings are out of range.", "error")
    else:
        with transaction(db):
            db.execute("UPDATE podcasts SET retention_days = ?, refresh_minutes = ?, fill_gaps = ?, "
                       "auto_download = ?, sort_oldest = ? WHERE id = ?",
                       (retention_days, refresh_minutes, fill_gaps, int(auto_download), int(sort == "oldest"),
                        podcast_id))
        note = " Process episodes again to cut them with the new gap." if fill_gaps != pod["fill_gaps"] else ""
        flash(request, "Saved." + note)
    return RedirectResponse(f"/podcasts/{podcast_id}?tab=settings#tabs", status_code=303)


@router.post("/podcasts/{podcast_id}/filters")
def add_filter(request: Request, podcast_id: int, pattern: str = Form(""), scope: str = Form("podcast"),
               processing: bool = Form(False), user=Depends(current_user),
               db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Hide the episodes whose titles match a pattern; with `processing`, for everyone, and keep them out
    of finding repeats."""
    _podcast_or_404(db, podcast_id)
    everywhere = scope == "all"
    try:
        filters.add(db, user["id"], None if everywhere else podcast_id, pattern, processing)
    except filters.FilterError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/podcasts/{podcast_id}?{urlencode({'tab': 'filters', 'pattern': pattern})}#tabs",
                                status_code=303)
    if processing:
        wake_worker(request)  # an unlearn job may be waiting
    here = db.execute("SELECT COUNT(*) FROM episodes WHERE podcast_id = ? AND title REGEXP ?",
                      (podcast_id, pattern.strip())).fetchone()[0]
    who = "everyone" if processing else "you"
    flash(request, f"Filter added. It hides {here:,} {'episode' if here == 1 else 'episodes'} of this podcast "
                   f"from {who}{', and the ones it matches in other podcasts' if everywhere else ''}.")
    return RedirectResponse(f"/podcasts/{podcast_id}?tab=filters#tabs", status_code=303)


@router.post("/filters/{filter_id}/remove")
def remove_filter(request: Request, filter_id: int, next: str = Form("/"), user=Depends(current_user),
                  db: sqlite3.Connection = Depends(get_db)) -> Response:
    row = filters.get(db, filter_id)
    if row is None:
        raise HTTPException(404, "No such filter")
    if not filters.may_remove(user, row):
        raise HTTPException(403, "Only its owner or the admin can remove that filter.")
    filters.remove(db, filter_id)
    flash(request, "Filter removed.")
    return RedirectResponse(next if next.startswith("/podcasts/") else "/", status_code=303)


@router.post("/podcasts/{podcast_id}/feed-token")
def new_feed_address(request: Request, podcast_id: int, user=Depends(current_user),
                     db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Give your own private feed of a podcast a new address. Everyone else's stays."""
    _podcast_or_404(db, podcast_id)
    private_feeds.new_feed_token(db, user["id"], podcast_id)
    flash(request, "Your private feed has a new address. The old one no longer works.")
    return RedirectResponse(f"/podcasts/{podcast_id}?tab=feed#tabs", status_code=303)


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
def remove_podcast(request: Request, podcast_id: int, user=Depends(admin_user),
                   db: sqlite3.Connection = Depends(get_db)) -> Response:
    pod = _podcast_or_404(db, podcast_id)
    stayed = feeds.remove_podcast(db, podcast_id)
    note = f" Its {stayed:,} downloaded episodes stay." if stayed else ""
    flash(request, f"Removed {pod['name']}.{note}")
    return RedirectResponse("/", status_code=303)
