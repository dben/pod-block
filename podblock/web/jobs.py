"""A queue of background jobs, kept in the database so it survives restarts.

Pages add jobs; worker threads (background.py) claim them one at a time and
run them. A job reports what it is doing through its `Context`, which also
stops it, between steps or mid-download, when someone cancels it.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ACTIVE = ("queued", "running")
FINISHED = ("done", "failed", "cancelled")
PROGRESS_EVERY = 1.0  # seconds between progress writes


class Cancelled(Exception):
    pass


def enqueue(db: sqlite3.Connection, kind: str, episode_ids: list[int], user_id: int | None,
            skip_ready: bool = True) -> list[int]:
    """Queue a job per episode, unless it already has one queued or running. Returns the episodes queued."""
    ids = list(dict.fromkeys(episode_ids))
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    rows = db.execute(f"SELECT id, state FROM episodes WHERE id IN ({marks})", ids).fetchall()
    states = {r["id"]: r["state"] for r in rows}
    queued, now = [], time.time()
    for ep_id in ids:
        if ep_id not in states or states[ep_id] in ACTIVE or (skip_ready and states[ep_id] == "ready"):
            continue
        db.execute("INSERT INTO jobs (kind, episode_id, requested_by, created_at) VALUES (?, ?, ?, ?)",
                   (kind, ep_id, user_id, now))
        db.execute("UPDATE episodes SET state = 'queued', state_note = NULL WHERE id = ?", (ep_id,))
        queued.append(ep_id)
    db.commit()
    return queued


def claim(db: sqlite3.Connection) -> sqlite3.Row | None:
    """Take the oldest queued job and mark it running; None when there is none."""
    row = db.execute(
        """UPDATE jobs SET state = 'running', started_at = ?, attempts = attempts + 1, step = 'Starting',
                  progress = NULL, error = NULL
           WHERE id = (SELECT id FROM jobs WHERE state = 'queued' ORDER BY id LIMIT 1) AND state = 'queued'
           RETURNING *""", (time.time(),)).fetchone()
    db.commit()
    return row


def finish(db: sqlite3.Connection, job_id: int, state: str, error: str | None = None) -> None:
    db.execute("UPDATE jobs SET state = ?, error = ?, finished_at = ?, progress = NULL WHERE id = ?",
               (state, error, time.time(), job_id))
    db.commit()


def settle_episode(db: sqlite3.Connection, episode_id: int, failed: str | None = None) -> None:
    """Put an episode back at rest after its job stopped: ready if it has a cut file, else failed or nothing."""
    row = db.execute("SELECT cut_path FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        return
    if row["cut_path"] and Path(row["cut_path"]).exists():
        state, note = "ready", (f"Processing again failed: {failed}" if failed else None)
    else:
        state, note = ("failed", failed) if failed else (None, None)
    db.execute("UPDATE episodes SET state = ?, state_note = ? WHERE id = ?", (state, note, episode_id))
    db.commit()


def cancel(db: sqlite3.Connection, job_id: int) -> bool:
    """Cancel a queued job now, or ask a running one to stop. False if it already finished."""
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None or job["state"] not in ACTIVE:
        return False
    if job["state"] == "queued":
        finish(db, job_id, "cancelled")
        if job["episode_id"]:
            settle_episode(db, job["episode_id"])
    else:
        db.execute("UPDATE jobs SET cancel_requested = 1, step = 'Stopping' WHERE id = ?", (job_id,))
        db.commit()
    return True


def retry(db: sqlite3.Connection, job_id: int, user_id: int | None) -> bool:
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None or job["state"] not in ("failed", "cancelled") or not job["episode_id"]:
        return False
    return bool(enqueue(db, job["kind"], [job["episode_id"]], user_id, skip_ready=False))


def recover(db: sqlite3.Connection) -> int:
    """After a restart: jobs that were running go back in the queue, unless they were being cancelled."""
    stopping = [r["id"] for r in db.execute("SELECT id FROM jobs WHERE state = 'running' AND cancel_requested = 1")]
    for job_id in stopping:
        finish(db, job_id, "cancelled")
    cur = db.execute("UPDATE jobs SET state = 'queued', step = NULL, progress = NULL WHERE state = 'running'")
    db.execute("""UPDATE episodes SET state = 'queued', state_note = NULL
                  WHERE state = 'running' AND id IN (SELECT episode_id FROM jobs WHERE state = 'queued')""")
    for r in db.execute("SELECT id FROM episodes WHERE state IN ('queued', 'running') AND id NOT IN "
                        "(SELECT episode_id FROM jobs WHERE state = 'queued')").fetchall():
        settle_episode(db, r["id"])  # companions of a job that went back in the queue
    db.commit()
    return cur.rowcount


def active_count(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued', 'running')").fetchone()[0]


def listing(db: sqlite3.Connection, recent: int = 30) -> dict[str, list[sqlite3.Row]]:
    base = """SELECT j.*, e.title AS episode_title, e.podcast_id, p.name AS podcast_name
              FROM jobs j LEFT JOIN episodes e ON e.id = j.episode_id LEFT JOIN podcasts p ON p.id = e.podcast_id"""
    return {
        "running": db.execute(f"{base} WHERE j.state = 'running' ORDER BY j.id").fetchall(),
        "queued": db.execute(f"{base} WHERE j.state = 'queued' ORDER BY j.id").fetchall(),
        "finished": db.execute(f"{base} WHERE j.state IN ('done', 'failed', 'cancelled') "
                               "ORDER BY j.finished_at DESC LIMIT ?", (recent,)).fetchall(),
    }


class Context:
    """What a running job uses to say what it is doing, and to learn it should stop."""

    def __init__(self, db: sqlite3.Connection, job: sqlite3.Row):
        self.db = db
        self.job = job
        self._step = ""
        self._episode: int | None = None
        self._last = 0.0

    def check(self) -> None:
        row = self.db.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (self.job["id"],)).fetchone()
        if row is None or row["cancel_requested"]:
            raise Cancelled()

    def step(self, text: str, episode_id: int | None = None) -> None:
        """Start a step, on the job and on the episode it is working on."""
        self.check()
        self._step, self._episode, self._last = text, episode_id, time.monotonic()
        on_job = text
        if episode_id is not None and episode_id != self.job["episode_id"]:
            row = self.db.execute("SELECT title FROM episodes WHERE id = ?", (episode_id,)).fetchone()
            on_job = f"{text} “{row['title']}”" if row and row["title"] else text
        self.db.execute("UPDATE jobs SET step = ?, progress = NULL WHERE id = ?", (on_job, self.job["id"]))
        if episode_id is not None:
            self.db.execute("UPDATE episodes SET state = 'running', state_note = ? WHERE id = ?", (text, episode_id))
        self.db.commit()

    def progress(self, fraction: float | None) -> None:
        """How far through the step; written at most once a second."""
        now = time.monotonic()
        if now - self._last < PROGRESS_EVERY:
            return
        self._last = now
        self.check()
        note = f"{self._step} {round(fraction * 100)}%" if fraction is not None else self._step
        self.db.execute("UPDATE jobs SET progress = ? WHERE id = ?", (fraction, self.job["id"]))
        if self._episode is not None:
            self.db.execute("UPDATE episodes SET state_note = ? WHERE id = ?", (note, self._episode))
        self.db.commit()
