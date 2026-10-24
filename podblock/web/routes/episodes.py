"""Pages for one episode, and its audio."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from ... import cut
from ...app import chapters, grabs, jobs, library, processing, states
from ...app.text import show_notes
from ..deps import current_user, get_db, wake_worker
from ..templating import flash, page
from .podcasts import list_return_url

router = APIRouter()


class TypeChange(BaseModel):
    """A section of the timeline given a type: the blocks in it, and for an ad break, the bumpers around it."""
    type: str
    blocks: list[int] = []
    bumpers: list[int] = []
    start: float = 0  # the section, to find the block at its edge (processing.check_edits)
    end: float = 1e12


class Addition(BaseModel):
    """A stretch cut by hand; with `as_block`, saved as a block of this podcast, to be looked for in its other
    episodes too, as `type`: local, intro or outro."""
    start: float
    end: float
    as_block: bool = False
    type: str = "show"


class MissingKeep(BaseModel):
    """A stretch left out of the feed's transcript, kept (not cut) or cut again."""
    start: float
    end: float
    keep: bool


class Edits(BaseModel):
    """The changes the timeline saves (`changes()` in timeline.js)."""
    types: list[TypeChange] = []
    add: list[Addition] = []
    remove: list[int] = []  # hand-made cuts to undo, by id
    delete: list[int] = []  # blocks marked by hand to delete, by id
    missing: list[MissingKeep] = []


def _episode(db: sqlite3.Connection, user_id: int, episode_id: int) -> sqlite3.Row:
    row = library.episode(db, user_id, episode_id)
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
    podcast = library.podcast(db, ep["podcast_id"])
    report = library.report(db, episode_id)
    job = jobs.latest(db, episode_id)
    data = None
    if report is not None and ep["original_path"] and Path(ep["original_path"]).exists():
        data = {**processing.timeline(db, episode_id, report), "duration": ep["duration"],
                "editable": ep["state"] == states.READY, "audio": f"/episodes/{episode_id}/original",
                "peaks": f"/episodes/{episode_id}/peaks", "save": f"/episodes/{episode_id}/edits"}
    return page(request, "episode.html", title=ep["title"], nav="podcasts", user=user, episode=ep,
                podcast=podcast, back=list_return_url(request, podcast["id"]), report=report, job=job,
                notes=show_notes(ep["item_xml"]), timeline=data,
                captions=next(iter(chapters.transcripts(ep)), None),
                play_needed=round(grabs.play_needed(ep["cut_duration"] or ep["duration"])))


@router.get("/episodes/{episode_id}/peaks")
def episode_peaks(request: Request, episode_id: int, user=Depends(current_user),
                  db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The waveform of the original, made on first ask for episodes processed before waveforms were."""
    _episode(db, user["id"], episode_id)
    try:
        path = processing.ensure_peaks(db, request.app.state.settings, episode_id)
    except cut.CutError as e:
        raise HTTPException(500, str(e)) from e
    if path is None:
        raise HTTPException(404, "No audio to draw")
    return FileResponse(path, media_type="application/json", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/episodes/{episode_id}/edits")
def save_edits(request: Request, episode_id: int, edits: Edits, user=Depends(current_user),
               db: sqlite3.Connection = Depends(get_db)) -> Response:
    """Save the changes made on the timeline: queue a job to apply them and cut the episode again."""
    _episode(db, user["id"], episode_id)
    try:
        changes = processing.check_edits(db, episode_id, edits.model_dump())
    except processing.ProcessError as e:
        busy = isinstance(e, processing.Busy)
        return JSONResponse({"error": f"Nothing saved: {e}."}, status_code=409 if busy else 400)
    processing.queue_edits(db, episode_id, user["id"], changes)
    wake_worker(request)
    flash(request, "Saved. Cutting it again with your changes.")
    return JSONResponse({"ok": True, "changes": changes})


@router.get("/episodes/{episode_id}/row")
def episode_row(request: Request, episode_id: int, user=Depends(current_user),
                db: sqlite3.Connection = Depends(get_db)) -> Response:
    """One row of the episode list, so a row being processed can keep itself up to date."""
    ep = _episode(db, user["id"], episode_id)
    podcast = library.podcast(db, ep["podcast_id"])
    return page(request, "_episode_row.html", e=ep, podcast=podcast, here=f"/podcasts/{podcast['id']}")


@router.get("/episodes/{episode_id}/status")
def episode_status(request: Request, episode_id: int, user=Depends(current_user),
                   db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The status card of the episode page, polled while it is processed."""
    ep = _episode(db, user["id"], episode_id)
    if ep["state"] in states.ACTIVE:
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


@router.get("/episodes/{episode_id}/captions.vtt")
def episode_captions(episode_id: int, user=Depends(current_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The captions for the page's player: the episode's transcript as WebVTT, moved to fit the cut."""
    ep = _episode(db, user["id"], episode_id)
    text = chapters.captions(ep)
    if text is None:
        raise HTTPException(404, "No captions for this episode")
    return Response(text.encode(), media_type="text/vtt; charset=utf-8", headers={"Cache-Control": "no-cache"})


@router.get("/episodes/{episode_id}/transcript")
def episode_transcript(request: Request, episode_id: int, user=Depends(current_user),
                       db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The transcript card's paragraphs, loaded when it's opened."""
    ep = _episode(db, user["id"], episode_id)
    return page(request, "_transcript.html", paragraphs=chapters.paragraphs(ep), seek=ep["state"] == states.READY)


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
        flash(request, "Processing again." if ep["state"] == states.READY else "Queued.")
    else:
        flash(request, "It's already queued.")
    return RedirectResponse(f"/episodes/{episode_id}", status_code=303)
