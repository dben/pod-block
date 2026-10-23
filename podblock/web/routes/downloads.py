"""The admin's page of every download on disk: how much room they take, and a way to delete them."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from ...app import jobs, library, storage
from ..deps import admin_user, get_db, wake_worker
from ..templating import filesize, flash, page, plural

SORTS = {"size": "Largest", "recent": "Newest", "podcast": "Podcast"}

router = APIRouter()


@router.get("/downloads")
def downloads_page(request: Request, sort: str = "size", user=Depends(admin_user),
                   db: sqlite3.Connection = Depends(get_db)) -> Response:
    sort = sort if sort in SORTS else "size"
    items = storage.on_disk(db, sort)
    return page(request, "downloads.html", title="Downloads", nav="downloads", user=user, items=items, sort=sort,
                sorts=SORTS, total=sum(d["total"] for d in items), originals=sum(d["original"] for d in items),
                cuts=sum(d["cut"] for d in items), disk=storage.disk(request.app.state.settings.media_dir))


@router.post("/downloads/delete")
def delete_downloads(request: Request, ids: list[int] = Form([]), next: str = Form("/downloads"),
                     user=Depends(admin_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Delete the files of the episodes picked. Ready ones show as cleaned up and can be downloaded again;
    ones being processed are left alone."""
    done = storage.delete(db, ids)
    note = (f" {done.skipped:,} being processed {'was' if done.skipped == 1 else 'were'} left alone."
            if done.skipped else "")
    flash(request, f"Deleted {plural(done.deleted, 'download')}, freeing {filesize(done.freed)}.{note}"
          if done.deleted else "Nothing deleted." + note)
    return RedirectResponse(next if next.startswith("/downloads") else "/downloads", status_code=303)


@router.post("/downloads/reprocess")
def reprocess_downloads(request: Request, ids: list[int] = Form([]), next: str = Form("/downloads"),
                        user=Depends(admin_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Download and cut the episodes picked again; ones already queued are left as they are."""
    ids = list(dict.fromkeys(ids))[:library.MAX_IDS]
    queued = jobs.enqueue(db, "process", ids, user["id"], skip_ready=False)
    if queued:
        wake_worker(request)
    skipped = len(ids) - len(queued)
    note = f" {skipped:,} already queued." if skipped else ""
    flash(request, f"Queued {plural(len(queued), 'episode')}.{note}" if queued else "Those are already queued.")
    return RedirectResponse(next if next.startswith("/downloads") else "/downloads", status_code=303)
