"""The admin's timings page: how long each step of the jobs takes, and every timing as CSV."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ...app import timings
from ..deps import admin_user, get_db
from ..templating import page

PERIODS = {7: "7 days", 30: "30 days", 90: "90 days"}

router = APIRouter()


def _days(days: int) -> int:
    return days if days in PERIODS else 30


@router.get("/timings")
def timings_page(request: Request, days: int = 30, user=Depends(admin_user),
                 db: sqlite3.Connection = Depends(get_db)) -> Response:
    days = _days(days)
    return page(request, "timings.html", title="Timings", nav="jobs", user=user, days=days, periods=PERIODS,
                kinds=timings.summary(db, days), recent=timings.recent(db), keep_days=timings.KEEP_DAYS)


@router.get("/timings.csv")
def timings_csv(days: int = 30, user=Depends(admin_user), db: sqlite3.Connection = Depends(get_db)) -> Response:
    days = _days(days)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["at", "job", "kind", "step", "episode_id", "episode", "podcast", "seconds", "audio_seconds",
                     "count", "detail"])
    for r in timings.rows(db, days):
        writer.writerow([datetime.fromtimestamp(r["at"], timezone.utc).isoformat(timespec="seconds"), r["job_id"],
                         r["kind"], r["step"], r["episode_id"], r["episode"], r["podcast"], f"{r['seconds']:.3f}",
                         "" if r["audio"] is None else f"{r['audio']:.1f}", r["count"], r["detail"]])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="pod-block-timings-{days}d.csv"'})
