"""Cleaning up: downloads go a set time after they were made ready, unless someone keeps them.

Each podcast has `retention_days` (0 keeps downloads for good). When an
episode's time is up, its download, cut file and waveform are deleted and it
becomes `expired`: its metadata, report and timeline marks stay, so it can be
downloaded again and cut the same way. An episode any user keeps never
expires. Once a day the detector's own clean-up runs too, after making sure
the fingerprints of ready and kept episodes look fresh to it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from ..pipeline import prune
from ..store import Store
from . import chapters
from .db import set_setting, setting, transaction
from .locks import detecting
from .states import IN_USE, READY, sql

log = logging.getLogger(__name__)

DAY = 86400.0
PRUNE_EVERY = DAY
FINGERPRINT_DAYS = 30  # fingerprints of episodes no longer on hand are deleted this long after
BLOCK_DAYS = 90  # blocks not heard in this long go, unless typed by hand
BUMPER_DAYS = 365
NO_AUDIO_PRUNE = 36500  # the app never stores audio where the detector's prune looks; leave the CLI's alone

KEPT = "EXISTS (SELECT 1 FROM user_episodes ue WHERE ue.episode_id = e.id AND ue.kept = 1)"


def _unlink(path: str | None) -> None:
    if path:
        Path(path).unlink(missing_ok=True)


def clear(db: sqlite3.Connection, ep: sqlite3.Row, now: float | None = None) -> None:
    """Delete an episode's download, cut file, waveform and transcripts. A ready one becomes `expired`, to be
    downloaded again and cut the same way; any other keeps its state."""
    for path in (ep["original_path"], ep["cut_path"], ep["peaks_path"]):
        _unlink(path)
    chapters.forget(ep)
    gone = ("original_path = NULL, cut_path = NULL, peaks_path = NULL, cut_spec = NULL, cut_chapters = NULL, "
            "source_chapters = NULL, transcripts = NULL")  # chapters are looked for again on the next download
    with transaction(db):
        if ep["state"] == READY:
            db.execute(f"UPDATE episodes SET state = 'expired', state_note = NULL, {gone}, ready_at = NULL, "
                       "expired_at = ? WHERE id = ?", (now or time.time(), ep["id"]))
        else:
            db.execute(f"UPDATE episodes SET {gone} WHERE id = ?", (ep["id"],))


def expire_due(db: sqlite3.Connection, now: float | None = None) -> list[int]:
    """Delete the files of every episode whose time is up. Returns the ids that expired."""
    now = now or time.time()
    due = db.execute(
        f"""SELECT e.* FROM episodes e JOIN podcasts p ON p.id = e.podcast_id
            WHERE e.state = 'ready' AND e.ready_at IS NOT NULL AND p.retention_days > 0
              AND e.ready_at + p.retention_days * {DAY} <= ? AND NOT {KEPT}""", (now,)).fetchall()
    with transaction(db):
        for ep in due:
            clear(db, ep, now)
    # Downloads that never became ready (a failed job, companions of a cancelled one) go on the same clock.
    leftovers = db.execute(
        f"""SELECT e.id, e.original_path, p.retention_days FROM episodes e JOIN podcasts p ON p.id = e.podcast_id
            WHERE e.original_path IS NOT NULL AND COALESCE(e.state, '') NOT IN ({sql(IN_USE)})
              AND p.retention_days > 0 AND NOT {KEPT}""").fetchall()
    with transaction(db):
        for ep in leftovers:
            path = Path(ep["original_path"])
            if not path.exists() or path.stat().st_mtime + ep["retention_days"] * DAY <= now:
                _unlink(ep["original_path"])
                db.execute("UPDATE episodes SET original_path = NULL WHERE id = ?", (ep["id"],))
    if due:
        log.info("cleaned up %d episode(s)", len(due))
    return [ep["id"] for ep in due]


def prune_store(db: sqlite3.Connection, now: float | None = None) -> dict | None:
    """Run the detector's clean-up, at most once a day. Returns what it removed, or None if it wasn't due."""
    now = now or time.time()
    last = float(setting(db, "last_prune") or 0)
    if now - last < PRUNE_EVERY:
        return None
    set_setting(db, "last_prune", str(now))
    # The detector ages fingerprints by file time; ones still in use must not look old to it.
    for r in db.execute(f"""SELECT fingerprint_path FROM episodes e WHERE fingerprint_path IS NOT NULL
                            AND (state IN ({sql(IN_USE)}) OR {KEPT})"""):
        try:
            os.utime(r["fingerprint_path"], (now, now))
        except FileNotFoundError:
            pass
    with detecting:
        gone = prune(Store.on(db), audio_days=NO_AUDIO_PRUNE, fingerprint_days=FINGERPRINT_DAYS,
                     block_days=BLOCK_DAYS, bumper_days=BUMPER_DAYS)
    if any(gone.values()):
        log.info("pruned the store: %s", dict(gone))
    return dict(gone)


def expires_at(ready_at: float | None, retention_days: int | None) -> float | None:
    if not ready_at or not retention_days:
        return None
    return ready_at + retention_days * DAY
