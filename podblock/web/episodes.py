"""Pages for one episode, its audio, and the job queue."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from . import jobs
from .deps import current_user, get_db
from .feeds import show_notes
from .templating import flash, page

router = APIRouter()


def wake_worker(request: Request) -> None:
    if background := getattr(request.app.state, "background", None):
        background.wake()


def _episode(db: sqlite3.Connection, user_id: int, episode_id: int) -> sqlite3.Row:
    row = db.execute(
        """SELECT e.*, COALESCE(ue.archived, 0) AS archived, COALESCE(ue.kept, 0) AS kept
           FROM episodes e JOIN podcasts p ON p.id = e.podcast_id AND p.followed = 1
           LEFT JOIN user_episodes ue ON ue.episode_id = e.id AND ue.user_id = ?
           WHERE e.id = ?""", (user_id, episode_id)).fetchone()
    if row is None:
        raise HTTPException(404, "No such episode")
    return row


def _file_name(title: str | None, path: str) -> str:
    stem = re.sub(r"[^\w\- .,()&']+", "", title or "").strip()[:120] or "episode"
    return stem + Path(path).suffix


def _audio(path: str | None, title: str | None, download: bool) -> Response:
    if not path or not Path(path).exists():
        raise HTTPException(404, "No audio for this episode")
    return FileResponse(path, filename=_file_name(title, path) if download else None,
                        content_disposition_type="attachment" if download else "inline")


@router.get("/episodes/{episode_id}")
def show_episode(request: Request, episode_id: int, user=Depends(current_user),
                 db: sqlite3.Connection = Depends(get_db)) -> Response:
    ep = _episode(db, user["id"], episode_id)
    podcast = db.execute("SELECT * FROM podcasts WHERE id = ?", (ep["podcast_id"],)).fetchone()
    saved = db.execute("SELECT report FROM reports WHERE episode_id = ?", (episode_id,)).fetchone()
    report = json.loads(saved["report"]) if saved else None
    job = db.execute("SELECT * FROM jobs WHERE episode_id = ? ORDER BY id DESC LIMIT 1", (episode_id,)).fetchone()
    return page(request, "episode.html", title=ep["title"], nav="podcasts", user=user, episode=ep,
                podcast=podcast, report=report, job=job, notes=show_notes(ep["item_xml"]))


@router.get("/episodes/{episode_id}/row")
def episode_row(request: Request, episode_id: int, user=Depends(current_user),
                db: sqlite3.Connection = Depends(get_db)) -> Response:
    """One row of the episode list, so a row being processed can keep itself up to date."""
    ep = _episode(db, user["id"], episode_id)
    podcast = db.execute("SELECT * FROM podcasts WHERE id = ?", (ep["podcast_id"],)).fetchone()
    return page(request, "_episode_row.html", e=ep, podcast=podcast, here=f"/podcasts/{podcast['id']}")


@router.get("/episodes/{episode_id}/status")
def episode_status(request: Request, episode_id: int, user=Depends(current_user),
                   db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The status card of the episode page, polled while it is processed."""
    ep = _episode(db, user["id"], episode_id)
    if ep["state"] in jobs.ACTIVE:
        return page(request, "_episode_status.html", episode=ep)
    return Response(headers={"HX-Refresh": "true"})  # finished: reload for the report and player


@router.get("/episodes/{episode_id}/audio")
def cut_audio(episode_id: int, download: bool = False, user=Depends(current_user),
              db: sqlite3.Connection = Depends(get_db)) -> Response:
    ep = _episode(db, user["id"], episode_id)
    return _audio(ep["cut_path"], ep["title"], download)


@router.get("/episodes/{episode_id}/original")
def original_audio(episode_id: int, user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    ep = _episode(db, user["id"], episode_id)
    return _audio(ep["original_path"], ep["title"], False)


@router.post("/episodes/{episode_id}/process")
def process_episode(request: Request, episode_id: int, user=Depends(current_user),
                    db: sqlite3.Connection = Depends(get_db)) -> Response:
    ep = _episode(db, user["id"], episode_id)
    if jobs.enqueue(db, "process", [episode_id], user["id"], skip_ready=False):
        wake_worker(request)
        flash(request, "Processing again." if ep["state"] == "ready" else "Queued.")
    else:
        flash(request, "It's already queued.")
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


@router.get("/jobs")
def jobs_page(request: Request, user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    return page(request, "jobs.html", title="Jobs", nav="jobs", user=user, jobs=jobs.listing(db))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: int, user=Depends(current_user),
               db: sqlite3.Connection = Depends(get_db)) -> Response:
    flash(request, "Cancelled." if jobs.cancel(db, job_id) else "That job had already finished.")
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: int, user=Depends(current_user),
              db: sqlite3.Connection = Depends(get_db)) -> Response:
    if jobs.retry(db, job_id, user["id"]):
        wake_worker(request)
        flash(request, "Queued again.")
    else:
        flash(request, "That can't be retried; it may be queued already.", "error")
    return RedirectResponse("/jobs", status_code=303)
