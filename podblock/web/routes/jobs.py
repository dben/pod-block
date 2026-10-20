"""The job queue: what is running, waiting and finished, with a way to cancel or retry each."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from ...app import jobs
from ..deps import current_user, get_db, wake_worker
from ..templating import flash, page

router = APIRouter()


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
