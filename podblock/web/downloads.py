"""The admin's page of every download on disk: how much room they take, and a way to delete them."""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from . import jobs, retention
from .deps import admin_user, get_db
from .library import MAX_IDS
from .templating import filesize, flash, page

SORTS = {"size": "Largest", "recent": "Newest", "podcast": "Podcast"}

router = APIRouter()


def _size(path: str | None) -> int:
    try:
        return Path(path).stat().st_size if path else 0
    except OSError:
        return 0


def on_disk(db: sqlite3.Connection, sort: str = "size") -> list[dict]:
    """Every episode with a file under the media folder, with the size of each file, sorted."""
    rows = db.execute(
        """SELECT e.*, p.name AS podcast_name, p.image_url AS podcast_image, p.followed
           FROM episodes e JOIN podcasts p ON p.id = e.podcast_id
           WHERE e.original_path IS NOT NULL OR e.cut_path IS NOT NULL OR e.peaks_path IS NOT NULL""").fetchall()
    out = []
    for r in rows:
        original, cut, peaks = _size(r["original_path"]), _size(r["cut_path"]), _size(r["peaks_path"])
        out.append({"e": r, "original": original, "cut": cut, "total": original + cut + peaks,
                    "when": r["processed_at"] or r["ready_at"] or 0})
    key = {"size": lambda d: (-d["total"], -d["when"]), "recent": lambda d: -d["when"],
           "podcast": lambda d: ((d["e"]["podcast_name"] or "").lower(), -d["when"])}[sort]
    return sorted(out, key=key)


@router.get("/downloads")
def downloads_page(request: Request, sort: str = "size", user=Depends(admin_user),
                   db: sqlite3.Connection = Depends(get_db)) -> Response:
    sort = sort if sort in SORTS else "size"
    items = on_disk(db, sort)
    media = request.app.state.settings.media_dir
    try:
        disk = shutil.disk_usage(media)
    except OSError:
        disk = None
    return page(request, "downloads.html", title="Downloads", nav="downloads", user=user, items=items, sort=sort,
                sorts=SORTS, total=sum(d["total"] for d in items), originals=sum(d["original"] for d in items),
                cuts=sum(d["cut"] for d in items), disk=disk, busy=jobs.ACTIVE)


@router.post("/downloads/delete")
def delete_downloads(request: Request, ids: list[int] = Form([]), next: str = Form("/downloads"),
                     user=Depends(admin_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Delete the files of the episodes picked. Ready ones show as cleaned up and can be downloaded again;
    ones being processed are left alone."""
    ids = list(dict.fromkeys(ids))[:MAX_IDS]
    rows = db.execute(f"SELECT * FROM episodes WHERE id IN ({', '.join('?' * len(ids))})", ids).fetchall() if ids else []
    now, freed, deleted, skipped = time.time(), 0, 0, 0
    for ep in rows:
        if ep["state"] in jobs.ACTIVE:
            skipped += 1
            continue
        freed += sum(_size(ep[k]) for k in ("original_path", "cut_path", "peaks_path"))
        retention.clear(db, ep, now)
        deleted += 1
    db.commit()
    note = f" {skipped:,} being processed {'was' if skipped == 1 else 'were'} left alone." if skipped else ""
    flash(request, f"Deleted {deleted:,} {'download' if deleted == 1 else 'downloads'}, freeing {filesize(freed)}.{note}"
          if deleted else "Nothing deleted." + note)
    return RedirectResponse(next if next.startswith("/downloads") else "/downloads", status_code=303)
