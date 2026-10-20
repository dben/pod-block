"""Pages for one episode, its audio, and the job queue."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .. import cut
from . import grabs, jobs, library, processing
from .deps import current_user, get_db
from .feeds import show_notes
from .templating import flash, page

router = APIRouter()


def wake_worker(request: Request) -> None:
    if background := getattr(request.app.state, "background", None):
        background.wake()


def _episode(db: sqlite3.Connection, user_id: int, episode_id: int) -> sqlite3.Row:
    row = db.execute(
        f"""SELECT e.*, COALESCE(ue.archived, 0) AS archived, COALESCE(ue.kept, 0) AS kept,
                  {library.KEPT_ANY} AS kept_any, {library.GRABS}, p.retention_days
           FROM episodes e JOIN podcasts p ON p.id = e.podcast_id AND p.followed = 1
           {library.USER_JOIN}
           WHERE e.id = :episode_id""", {"user_id": user_id, "episode_id": episode_id}).fetchone()
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
    data = None
    if report is not None and ep["original_path"] and Path(ep["original_path"]).exists():
        data = {**processing.timeline(db, episode_id, report), "duration": ep["duration"],
                "editable": ep["state"] == "ready", "audio": f"/episodes/{episode_id}/original",
                "peaks": f"/episodes/{episode_id}/peaks", "save": f"/episodes/{episode_id}/edits"}
    return page(request, "episode.html", title=ep["title"], nav="podcasts", user=user, episode=ep,
                podcast=podcast, report=report, job=job, notes=show_notes(ep["item_xml"]), timeline=data,
                play_needed=round(grabs.play_needed(ep["cut_duration"] or ep["duration"])))


@router.get("/episodes/{episode_id}/peaks")
def episode_peaks(request: Request, episode_id: int, user=Depends(current_user),
                  db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The waveform of the original, made on first ask for episodes processed before waveforms were."""
    _episode(db, user["id"], episode_id)
    try:
        path = processing.ensure_peaks(db, request.app.state.settings, episode_id)
    except cut.CutError as e:
        raise HTTPException(500, str(e))
    if path is None:
        raise HTTPException(404, "No audio to draw")
    return FileResponse(path, media_type="application/json", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/episodes/{episode_id}/edits")
async def save_edits(request: Request, episode_id: int, user=Depends(current_user),
                     db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Save the changes made on the timeline: queue a job to apply them and cut the episode again."""
    ep = _episode(db, user["id"], episode_id)
    if ep["state"] in jobs.ACTIVE:
        return JSONResponse({"error": "It's being processed; save again when that's done."}, status_code=409)
    try:
        changes = processing.check_edits(db, episode_id, await request.json())
    except processing.ProcessError as e:
        return JSONResponse({"error": f"Nothing saved: {e}."}, status_code=400)
    except (ValueError, TypeError, KeyError, AttributeError):
        return JSONResponse({"error": "Those changes didn't make sense."}, status_code=400)
    jobs.enqueue(db, "edit", [episode_id], user["id"], skip_ready=False, payload=json.dumps(changes),
                 note="Saving your changes")
    wake_worker(request)
    flash(request, "Saved. Cutting it again with your changes.")
    return JSONResponse({"ok": True, "changes": changes})


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
def cut_audio(request: Request, episode_id: int, download: bool = False, user=Depends(current_user),
              db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The cut audio, for the page's player; with `download`, as a file to save, which counts as a grab."""
    ep = _episode(db, user["id"], episode_id)
    response = _audio(ep["cut_path"], ep["title"], download)
    if download:
        grabs.record(db, user["id"], episode_id, "download", resumed=grabs.resumed(request.headers.get("range")))
    return response


@router.post("/episodes/{episode_id}/played")
def played(episode_id: int, user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The page's player has played enough of the episode to count as a grab (see `grabs.play_needed`)."""
    _episode(db, user["id"], episode_id)
    return JSONResponse({"new": grabs.record(db, user["id"], episode_id, "app")})


@router.get("/episodes/{episode_id}/original")
def original_audio(episode_id: int, user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    ep = _episode(db, user["id"], episode_id)
    return _audio(ep["original_path"], ep["title"], False)


@router.post("/episodes/{episode_id}/keep")
def keep_episode(request: Request, episode_id: int, keep: bool = Form(...), user=Depends(current_user),
                 db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Keep an episode, so it is never cleaned up, or stop keeping it."""
    ep = _episode(db, user["id"], episode_id)
    library.set_kept(db, user["id"], ep["podcast_id"], [episode_id], keep)
    flash(request, "Kept: it won't be cleaned up." if keep else "No longer kept.")
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)


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
    background = getattr(request.app.state, "background", None)
    return page(request, "jobs.html", title="Jobs", nav="jobs", user=user, jobs=jobs.listing(db),
                worker_down=background is not None and not background.running())


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
