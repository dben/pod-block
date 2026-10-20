"""The admin's page of every download on disk: how much room they take, and a way to delete them."""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from ...app import jobs, retention
from ...app.db import transaction
from ...app.library import MAX_IDS
from ...app.storage import disk, on_disk, size
from ..deps import admin_user, get_db
from ..templating import filesize, flash, page

SORTS = {"size": "Largest", "recent": "Newest", "podcast": "Podcast"}

router = APIRouter()


@router.get("/downloads")
def downloads_page(request: Request, sort: str = "size", user=Depends(admin_user),
                   db: sqlite3.Connection = Depends(get_db)) -> Response:
    sort = sort if sort in SORTS else "size"
    items = on_disk(db, sort)
    return page(request, "downloads.html", title="Downloads", nav="downloads", user=user, items=items, sort=sort,
                sorts=SORTS, total=sum(d["total"] for d in items), originals=sum(d["original"] for d in items),
                cuts=sum(d["cut"] for d in items), disk=disk(request.app.state.settings.media_dir),
                busy=jobs.ACTIVE)


@router.post("/downloads/delete")
def delete_downloads(request: Request, ids: list[int] = Form([]), next: str = Form("/downloads"),
                     user=Depends(admin_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Delete the files of the episodes picked. Ready ones show as cleaned up and can be downloaded again;
    ones being processed are left alone."""
    ids = list(dict.fromkeys(ids))[:MAX_IDS]
    rows = db.execute(f"SELECT * FROM episodes WHERE id IN ({', '.join('?' * len(ids))})", ids).fetchall() if ids else []
    now, freed, deleted, skipped = time.time(), 0, 0, 0
    with transaction(db):
        for ep in rows:
            if ep["state"] in jobs.ACTIVE:
                skipped += 1
                continue
            freed += sum(size(ep[k]) for k in ("original_path", "cut_path", "peaks_path"))
            retention.clear(db, ep, now)
            deleted += 1
    note = f" {skipped:,} being processed {'was' if skipped == 1 else 'were'} left alone." if skipped else ""
    flash(request, f"Deleted {deleted:,} {'download' if deleted == 1 else 'downloads'}, freeing {filesize(freed)}.{note}"
          if deleted else "Nothing deleted." + note)
    return RedirectResponse(next if next.startswith("/downloads") else "/downloads", status_code=303)
