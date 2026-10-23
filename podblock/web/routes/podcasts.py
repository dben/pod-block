"""Pages for following feeds and going through their episodes."""

from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

import segno
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ...app import directory, filters, following, library, phrases, private_feeds, storage
from ..deps import admin_user, current_user, feed_url, get_db, wake_worker
from ..templating import flash, page, plural

PAGE_SIZE = 50
VIEWS = ("active", "archived", "filtered")  # which list
NARROWS = ("downloaded", "grabbed")  # narrow any list; on together or alone
TABS = ("episodes", "feed", "filters", "settings")

router = APIRouter()


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _podcast_or_404(db: sqlite3.Connection, podcast_id: int) -> sqlite3.Row:
    pod = library.podcast(db, podcast_id)
    if pod is None:
        raise HTTPException(404, "No such podcast")
    return pod


def _qr(url: str) -> str:
    return segno.make(url, error="m").svg_inline(scale=4, border=2, omitsize=True, dark="#0d0f13", light="#ffffff")


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
    everything = library.followed_count(db)
    mine = len(shown) if view == "mine" else sum(p["subscribed"] for p in shown)
    expired_total = sum(p["expired_count"] for p in shown if p["subscribed"])
    return page(request, "home.html", status_code=status_code, title="Podcasts", nav="podcasts", user=user,
                podcasts=shown, view=view, mine_count=mine, all_count=everything, expired_total=expired_total,
                omni_addresses=_addresses(request, private_feeds.omni_token(db, user["id"])),
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
def new_omni_address(request: Request, user=Depends(current_user),
                     db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Give your omni feed a new address."""
    private_feeds.new_omni_token(db, user["id"])
    flash(request, "Your omni feed has a new address. The old one no longer works.")
    return RedirectResponse("/?omni=1", status_code=303)


@router.post("/episodes/archive-expired")
def archive_all_expired(request: Request, user=Depends(current_user),
                        db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Archive every cleaned-up episode, of every podcast, for this user."""
    changed = library.archive_expired(db, user["id"])
    flash(request, f"Archived {plural(len(changed), 'cleaned-up episode')}.")
    return RedirectResponse("/", status_code=303)


@router.post("/podcasts")
def add_podcast(request: Request, url: str = Form(""), user=Depends(current_user),
                db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Follow the feed at a web address, or search the podcast directory for anything else."""
    address = directory.as_address(url)
    if address is None:
        return RedirectResponse(f"/?{urlencode({'search': url.strip()})}" if url.strip() else "/", status_code=303)
    try:
        result = following.add_podcast(db, directory.apple_feed(address) or address)
    except (following.RefreshError, directory.DirectoryError) as e:
        return _home(request, user, db, status_code=422, url=url, error=f"Couldn't add that feed: {e}.")
    library.subscribe(db, user["id"], result.podcast_id)
    flash(request, f"Added, with {result.added:,} episodes." if result.added else "Added.")
    return RedirectResponse(f"/podcasts/{result.podcast_id}", status_code=303)


def _list_url(podcast_id: int, view: str, narrow: set[str], q: str = "", **extra) -> str:
    query = {"view": view if view != "active" else "", **{n: "1" for n in NARROWS if n in narrow}, "q": q, **extra}
    query = {k: v for k, v in query.items() if v}
    return f"/podcasts/{podcast_id}" + (f"?{urlencode(query)}" if query else "")


@router.get("/podcasts/{podcast_id}")
def show_podcast(request: Request, podcast_id: int, view: str = "active", q: str = "", tab: str = "",
                 downloaded: bool = False, grabbed: bool = False,
                 page_no: int = Query(1, alias="page", ge=1), partial: bool = False,
                 user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The show's header, then one of its tabs: the episode list, your private feed, filters or settings.
    The list is one of `VIEWS`, narrowed to the episodes downloaded, grabbed, or both."""
    pod = _podcast_or_404(db, podcast_id)
    params = request.query_params
    tab = tab if tab in TABS else "feed" if params.get("feed") else "filters" if params.get("filters") else "episodes"
    narrow = {n for n, on in (("downloaded", downloaded), ("grabbed", grabbed)) if on}
    if view in NARROWS:  # the old ?view=downloaded and ?view=grabbed
        narrow.add(view)
    view = view if view in VIEWS else "active"
    context = dict(podcast=pod, view=view, q=q, page_no=page_no, tab=tab, narrow=narrow,
                   here=_list_url(podcast_id, view, narrow, q),
                   view_urls={v: _list_url(podcast_id, v, narrow) for v in VIEWS},
                   narrow_urls={n: _list_url(podcast_id, view, narrow ^ {n}, q) for n in NARROWS})
    if tab == "episodes":
        rows, total = library.episodes(db, user["id"], podcast_id, view == "archived", q, limit=PAGE_SIZE,
                                       offset=(page_no - 1) * PAGE_SIZE, downloaded="downloaded" in narrow,
                                       filtered=view == "filtered", grabbed="grabbed" in narrow,
                                       oldest_first=bool(pod["sort_oldest"]))
        more = page_no * PAGE_SIZE < total
        context.update(episodes=rows, total=total,
                       next_url=_list_url(podcast_id, view, narrow, q, page=page_no + 1) if more else None)
        if partial:
            return page(request, "_episodes.html", **context)
    if tab == "feed":
        context.update(private_addresses=_addresses(request, private_feeds.feed_token(db, user["id"], podcast_id)))
    if tab == "settings":
        intro, outro = phrases.of_podcast(db, podcast_id)
        context.update(intro_phrases=params.get("intro", "\n".join(intro)),
                       outro_phrases=params.get("outro", "\n".join(outro)))
    return page(request, "podcast.html", title=pod["name"], nav="podcasts", user=user,
                cut_sec=library.cut_seconds(db, podcast_id),
                subscribed=library.subscribed(db, user["id"], podcast_id),
                filters=filters.for_podcast(db, user["id"], podcast_id),
                counts=library.counts(db, user["id"], podcast_id, "downloaded" in narrow, "grabbed" in narrow),
                **context)


@router.post("/podcasts/{podcast_id}/settings")
def podcast_settings(request: Request, podcast_id: int, retention_days: int = Form(...),
                     refresh_minutes: int = Form(...), fill_gaps: float = Form(4.0), auto_download: bool = Form(False),
                     sort: str = Form("newest"), user=Depends(current_user),
                     db: sqlite3.Connection = Depends(get_db)) -> Response:
    _podcast_or_404(db, podcast_id)
    try:
        new_gap = library.save_settings(db, podcast_id, retention_days, refresh_minutes, fill_gaps, auto_download,
                                        oldest_first=sort == "oldest")
    except library.SettingsError as e:
        flash(request, str(e), "error")
    else:
        flash(request, "Saved." + (" Process episodes again to cut them with the new gap." if new_gap else ""))
    return RedirectResponse(f"/podcasts/{podcast_id}?tab=settings#tabs", status_code=303)


@router.post("/podcasts/{podcast_id}/phrases")
def podcast_phrases(request: Request, podcast_id: int, intro: str = Form(""), outro: str = Form(""),
                    user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Save the phrases that end the show's intro and begin its outro, and cut its episodes again to fit."""
    _podcast_or_404(db, podcast_id)
    try:
        queued = phrases.save(db, podcast_id, phrases.parse(intro), phrases.parse(outro), user["id"])
    except phrases.PhraseError as e:
        flash(request, str(e), "error")
        typed = urlencode({"tab": "settings", "intro": intro, "outro": outro})
        return RedirectResponse(f"/podcasts/{podcast_id}?{typed}#phrases", status_code=303)
    if queued:
        wake_worker(request)
    flash(request, "Saved." + (f" Cutting {plural(queued, 'episode')} again to fit." if queued else ""))
    return RedirectResponse(f"/podcasts/{podcast_id}?tab=settings#phrases", status_code=303)


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
    here = filters.matches(db, podcast_id, pattern)
    who = "everyone" if processing else "you"
    flash(request, f"Filter added. It hides {plural(here, 'episode')} of this podcast "
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
        flash(request, following.refresh(db, podcast_id, conditional=False).describe())
    except following.RefreshError as e:
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
        changed, picked = library.download(db, user["id"], podcast_id, ids, again=action == "reprocess")
        if changed:
            wake_worker(request)
        if _wants_json(request):
            return JSONResponse({"action": action, "changed": changed})
        skipped = picked - len(changed)
        note = f" {skipped:,} already ready or queued." if skipped else ""
        flash(request, f"Queued {plural(len(changed), 'episode')}.{note}"
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
    flash(request, f"{verb} {plural(len(changed), 'episode')}.")
    back = next if next.startswith(f"/podcasts/{podcast_id}") else f"/podcasts/{podcast_id}"
    return RedirectResponse(back, status_code=303)


@router.post("/podcasts/{podcast_id}/remove")
def remove_podcast(request: Request, podcast_id: int, user=Depends(admin_user),
                   db: sqlite3.Connection = Depends(get_db)) -> Response:
    pod = _podcast_or_404(db, podcast_id)
    stayed = following.remove_podcast(db, podcast_id)
    note = f" Its {stayed:,} downloaded episodes stay." if stayed else ""
    flash(request, f"Removed {pod['name']}.{note}")
    return RedirectResponse("/", status_code=303)
