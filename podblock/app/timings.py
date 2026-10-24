"""How long each step of each job took, kept for the timings page.

Each job's `Context` has a `Timer` (podblock/timing.py) that its steps report
to; when the job ends, whatever steps finished are saved in `timings`, one
row each, done or not. A job that finishes also gets a `job` row, for the
whole of it. A `process` job cutting a ready episode again is saved as kind
`recut`, so re-cuts can be told from first downloads. Rows are kept KEEP_DAYS.

The page (/timings, the admin's) sums them up per kind and step: the median
and the slow end (p90), and for steps over audio, the median time per hour
of audio. It also lists the latest jobs step by step, and gives every row as
CSV.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from ..timing import Timer
from .db import transaction

KEEP_DAYS = 90

# Every step, in the order the page lists them: (label, what `count` counts). Indented ones are part of the
# step above them.
STEPS = {
    "job": ("Whole job", None),
    "download": ("Download", "bytes"),
    "fingerprint": ("Fingerprint", None),
    "repeats.wait": ("Wait for another job's repeats", None),
    "repeats": ("Find repeats", "episodes"),
    "repeats.load": ("Load fingerprints", "fingerprints"),
    "repeats.compare": ("Compare pairs", "pairs"),
    "repeats.add": ("Store new blocks", "blocks"),
    "repeats.rescan": ("Rescan", "episodes"),
    "repeats.split": ("Split blocks", "split"),
    "repeats.nested": ("Drop nested blocks", "dropped"),
    "repeats.short": ("Drop short blocks", "dropped"),
    "repeats.bumpers": ("Tag bumpers", None),
    "repeats.edit": ("Apply edits", None),
    "repeats.unlearn": ("Forget blocks", "blocks"),
    "repeats.reports": ("Reports", "episodes"),
    "chapters": ("Read chapters", None),
    "cut": ("Cut", None),
    "waveform": ("Draw the waveform", None),
    "captions.upload": ("Send the audio", "bytes"),
    "captions.wait": ("Wait for a transcriber", "tries"),
    "captions.transcribe": ("Transcribe", None),
    "captions.speakers": ("Who speaks when", None),
}
KINDS = {"process": "Download and cut", "recut": "Cut again", "edit": "Edits", "unlearn": "Forget filtered",
         "captions": "Captions"}


def kind_of(job: sqlite3.Row) -> str:
    if job["kind"] == "process" and json.loads(job["payload"] or "{}").get("recut"):
        return "recut"
    return job["kind"]


def save(db: sqlite3.Connection, job: sqlite3.Row, timer: Timer, finished: bool) -> None:
    """Keep what the job's steps reported, and with `finished`, a row for the whole job."""
    now = time.time()
    rows = list(timer.rows)
    if finished and job["started_at"]:
        rows.append({"step": "job", "seconds": now - job["started_at"], "episode_id": job["episode_id"],
                     "audio": None, "count": None, "detail": None})
    if not rows:
        return
    kind = kind_of(job)
    with transaction(db):
        db.executemany(
            "INSERT INTO timings (job_id, kind, step, episode_id, seconds, audio, count, detail, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(job["id"], kind, r["step"], r["episode_id"], r["seconds"], r["audio"], r["count"],
              json.dumps(r["detail"]) if r["detail"] else None, now) for r in rows])


def prune(db: sqlite3.Connection, now: float | None = None) -> int:
    with transaction(db):
        cur = db.execute("DELETE FROM timings WHERE at < ?", ((now or time.time()) - KEEP_DAYS * 86400,))
    return cur.rowcount


# ---- for the page ----------------------------------------------------------------

def _quantile(values: list[float], q: float) -> float:
    """The `q` quantile, between the two nearest values."""
    values = sorted(values)
    at = (len(values) - 1) * q
    lo = int(at)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (at - lo)


@dataclass
class Summary:
    step: str
    label: str
    part: bool  # part of the step above it
    runs: int
    median: float
    p90: float
    total: float
    per_hour: float | None  # median seconds per hour of audio, for steps over audio
    count: float | None  # median of what `count` counts
    unit: str | None


@dataclass
class Kind:
    kind: str
    label: str
    steps: list[Summary] = field(default_factory=list)


def summary(db: sqlite3.Connection, days: int) -> list[Kind]:
    """Each kind's steps over the last `days`, in STEPS' order."""
    rows = db.execute("SELECT kind, step, seconds, audio, count FROM timings WHERE at >= ?",
                      (time.time() - days * 86400,)).fetchall()
    grouped: dict[str, dict[str, list[sqlite3.Row]]] = {}
    for r in rows:
        grouped.setdefault(r["kind"], {}).setdefault(r["step"], []).append(r)
    order = {step: n for n, step in enumerate(STEPS)}
    found = []
    for kind in sorted(grouped, key=lambda k: (list(KINDS).index(k) if k in KINDS else len(KINDS), k)):
        out = Kind(kind, KINDS.get(kind, kind.capitalize()))
        for step in sorted(grouped[kind], key=lambda s: (order.get(s, len(order)), s)):
            these = grouped[kind][step]
            secs = [r["seconds"] for r in these]
            rates = [r["seconds"] / r["audio"] * 3600 for r in these if r["audio"]]
            counts = [r["count"] for r in these if r["count"] is not None]
            label, unit = STEPS.get(step, (step, None))
            out.steps.append(Summary(step, label, "." in step and step.split(".")[0] in STEPS, len(these),
                                     _quantile(secs, 0.5), _quantile(secs, 0.9), sum(secs),
                                     _quantile(rates, 0.5) if rates else None,
                                     _quantile(counts, 0.5) if counts else None, unit))
        found.append(out)
    return found


def recent(db: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """The latest jobs with timings, each with its steps in the order they ran."""
    jobs = db.execute(
        """SELECT t.job_id, t.kind, MAX(t.at) AS at, j.state, j.payload, e.id AS episode_id, e.title
           FROM timings t LEFT JOIN jobs j ON j.id = t.job_id
           LEFT JOIN episodes e ON e.id = COALESCE(j.episode_id, json_extract(j.payload, '$.episode'))
           GROUP BY t.job_id ORDER BY MAX(t.at) DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for j in jobs:
        steps = db.execute("SELECT step, seconds, audio, count, episode_id FROM timings WHERE job_id = ? ORDER BY id",
                           (j["job_id"],)).fetchall()
        whole = next((s["seconds"] for s in steps if s["step"] == "job"), None)
        out.append({"job": j, "whole": whole, "label": KINDS.get(j["kind"], j["kind"]),
                    "steps": [{"label": STEPS.get(s["step"], (s["step"],))[0], "part": "." in s["step"],
                               "seconds": s["seconds"], "audio": s["audio"], "count": s["count"],
                               "unit": STEPS.get(s["step"], (None, None))[1], "episode_id": s["episode_id"]}
                              for s in steps if s["step"] != "job"]})
    return out


def rows(db: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    """Every row of the last `days`, for CSV."""
    return db.execute(
        """SELECT t.at, t.job_id, t.kind, t.step, t.episode_id, e.title AS episode, p.name AS podcast, t.seconds,
                  t.audio, t.count, t.detail
           FROM timings t LEFT JOIN episodes e ON e.id = t.episode_id LEFT JOIN podcasts p ON p.id = e.podcast_id
           WHERE t.at >= ? ORDER BY t.id""", (time.time() - days * 86400,)).fetchall()
