"""A queue of background jobs, kept in the database so it survives restarts.

Pages add jobs; worker threads (background.py) claim them one at a time and
run them. A job reports what it is doing through its `Context`, which also
stops it, between steps or mid-download, when someone cancels it.

Only one job works on an episode at a time. A running job holds its own
episode and the ones it takes along (`episodes.held_by`) until it finishes.
A job asked for meanwhile waits in the queue until then, and an episode has
at most one job waiting. `episodes.state` is only what pages show: a job
moves it back and forth as it goes, so it can't say who may start one.
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


HELD = "COALESCE(e.held_by IN (SELECT id FROM jobs WHERE state = 'running'), 0)"
WAITING = "EXISTS (SELECT 1 FROM jobs w WHERE w.episode_id = e.id AND w.state = 'queued')"


def _immediate(db: sqlite3.Connection) -> None:
    """Start a write transaction now, so what is read next can't change before it is written."""
    db.commit()
    db.execute("BEGIN IMMEDIATE")


def enqueue(db: sqlite3.Connection, kind: str, episode_ids: list[int], user_id: int | None,
            skip_ready: bool = True, payload: str | None = None, note: str | None = None) -> list[int]:
    """Queue a job per episode, unless one is already waiting. An episode a running job holds gets one that
    waits for it, unless `skip_ready` (a plain download: that job is getting it ready anyway), which also
    skips ready ones. Returns the episodes queued."""
    ids = list(dict.fromkeys(episode_ids))
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    queued, now = [], time.time()
    _immediate(db)
    try:
        rows = db.execute(f"SELECT e.id, e.state, {HELD} AS held, {WAITING} AS waiting FROM episodes e "
                          f"WHERE e.id IN ({marks})", ids).fetchall()
        found = {r["id"]: r for r in rows}
        for ep_id in ids:
            r = found.get(ep_id)
            if r is None or r["waiting"] or (skip_ready and (r["held"] or r["state"] in (*ACTIVE, "ready"))):
                continue
            db.execute("INSERT INTO jobs (kind, episode_id, requested_by, created_at, payload) VALUES (?, ?, ?, ?, ?)",
                       (kind, ep_id, user_id, now, payload))
            if not r["held"]:  # a held one shows what its job is doing, and goes to queued when that finishes
                db.execute("UPDATE episodes SET state = 'queued', state_note = ? WHERE id = ?", (note, ep_id))
            queued.append(ep_id)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return queued


def take_along(db: sqlite3.Connection, job_id: int, episode_ids: list[int], note: str) -> list[int]:
    """Have a running job hold more episodes (comparison episodes), unless another job holds them or waits for
    them. Returns the ones it took."""
    if not episode_ids:
        return []
    _immediate(db)
    try:
        taken = [r["id"] for r in db.execute(
            f"""SELECT e.id FROM episodes e WHERE e.id IN ({', '.join('?' * len(episode_ids))})
                AND NOT {HELD} AND NOT {WAITING} AND COALESCE(e.state, '') NOT IN ('queued', 'running')""",
            episode_ids)]
        taken.sort(key=episode_ids.index)
        db.executemany("UPDATE episodes SET held_by = ?, state = 'queued', state_note = ? WHERE id = ?",
                       [(job_id, note, ep_id) for ep_id in taken])
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return taken


def release(db: sqlite3.Connection, job_id: int) -> None:
    """Let go of a job's episodes. Those with a job waiting show as queued. The caller commits."""
    db.execute("""UPDATE episodes SET state = 'queued', state_note = NULL
                   WHERE held_by = ? AND EXISTS (SELECT 1 FROM jobs w WHERE w.episode_id = episodes.id
                                                 AND w.state = 'queued')""", (job_id,))
    db.execute("UPDATE episodes SET held_by = NULL WHERE held_by = ?", (job_id,))


def enqueue_task(db: sqlite3.Connection, kind: str, user_id: int | None, payload: str | None = None) -> int:
    """Queue a job that works on no one episode, such as `unlearn`."""
    cur = db.execute("INSERT INTO jobs (kind, requested_by, created_at, payload) VALUES (?, ?, ?, ?)",
                     (kind, user_id, time.time(), payload))
    db.commit()
    return cur.lastrowid


def claim(db: sqlite3.Connection) -> sqlite3.Row | None:
    """Take the oldest queued job whose episode no running job holds, mark it running and have it hold its
    episode; None when there is none."""
    if db.execute("SELECT 1 FROM jobs WHERE state = 'queued' LIMIT 1").fetchone() is None:
        return None  # a read, so an idle worker looking every few seconds takes no write lock
    _immediate(db)
    try:
        row = db.execute(
            f"""UPDATE jobs SET state = 'running', started_at = ?, attempts = attempts + 1, step = 'Starting',
                      progress = NULL, error = NULL
               WHERE id = (SELECT j.id FROM jobs j LEFT JOIN episodes e ON e.id = j.episode_id
                           WHERE j.state = 'queued' AND (e.id IS NULL OR NOT {HELD}) ORDER BY j.id LIMIT 1)
                 AND state = 'queued'
               RETURNING *""", (time.time(),)).fetchone()
        if row is not None and row["episode_id"]:
            db.execute("UPDATE episodes SET held_by = ? WHERE id = ?", (row["id"], row["episode_id"]))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return row


def finish(db: sqlite3.Connection, job_id: int, state: str, error: str | None = None) -> None:
    db.execute("UPDATE jobs SET state = ?, error = ?, finished_at = ?, progress = NULL WHERE id = ?",
               (state, error, time.time(), job_id))
    release(db, job_id)
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
        held = db.execute(f"SELECT {HELD} FROM episodes e WHERE e.id = ?", (job["episode_id"],)).fetchone()
        if job["episode_id"] and not (held and held[0]):  # a held one is its job's to settle
            settle_episode(db, job["episode_id"])
    else:
        db.execute("UPDATE jobs SET cancel_requested = 1, step = 'Stopping' WHERE id = ?", (job_id,))
        db.commit()
    return True


def retry(db: sqlite3.Connection, job_id: int, user_id: int | None) -> bool:
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None or job["state"] not in ("failed", "cancelled"):
        return False
    if not job["episode_id"]:
        return bool(enqueue_task(db, job["kind"], user_id, job["payload"]))
    return bool(enqueue(db, job["kind"], [job["episode_id"]], user_id, skip_ready=False, payload=job["payload"]))


def recover(db: sqlite3.Connection) -> int:
    """After a restart: jobs that were running go back in the queue, unless they were being cancelled."""
    stopping = [r["id"] for r in db.execute("SELECT id FROM jobs WHERE state = 'running' AND cancel_requested = 1")]
    for job_id in stopping:
        finish(db, job_id, "cancelled")
    cur = db.execute("UPDATE jobs SET state = 'queued', step = NULL, progress = NULL WHERE state = 'running'")
    db.execute("UPDATE episodes SET held_by = NULL WHERE held_by IS NOT NULL")
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
