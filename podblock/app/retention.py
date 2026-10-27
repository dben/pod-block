"""Cleaning up: downloads go a set time after they were last processed or grabbed, unless someone keeps them.

Each podcast has `retention_days` (0 keeps downloads for good), counted from
whichever came last: the episode being made ready, processed again, or
grabbed by anyone (grabs.py). When an
episode's time is up, its download, cut file and waveform are deleted and it
becomes `expired`: its metadata, report and timeline marks stay, so it can be
downloaded again and cut the same way. An episode any user keeps never
expires.

Captions made by the transcription service (captions.py) stay too, for
CAPTIONS_DAYS: they are small and slow to make, and the next download uses
them again instead of transcribing it once more, if it is the same file
(processing.py's `_new_download`, which also drops the fingerprint and hand
cuts timed to another). The feed's transcripts go, as they are fetched again
with the download.

An episode nobody wants goes sooner: once everyone who has its podcast on
their home page has archived it or filters it out, its files go a day after
it was last processed or grabbed (the day leaves time to play one downloaded
from the archive), whatever the podcast's `retention_days`. Once a day the
detector's own clean-up runs too, after making sure the fingerprints of ready
and kept episodes look fresh to it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from ..pipeline import prune
from ..store import Store
from . import chapters
from .db import set_setting, setting, transaction
from .filters import hidden_from
from .locks import detecting
from .states import IN_USE, READY, sql

log = logging.getLogger(__name__)

DAY = 86400.0
PRUNE_EVERY = DAY
FINGERPRINT_DAYS = 30  # fingerprints of episodes no longer on hand are deleted this long after
BLOCK_DAYS = 90  # blocks not heard in this long go, unless typed by hand
BUMPER_DAYS = 365
CAPTIONS_DAYS = 365  # captions of a cleaned-up episode stay this long after, for its next download
NO_AUDIO_PRUNE = 36500  # the app never stores audio where the detector's prune looks; leave the CLI's alone

UNWANTED_AFTER = DAY  # an episode nobody wants goes this long after it was made ready

LAST_GRAB = "(SELECT MAX(g.last_at) FROM grabs g WHERE g.episode_id = e.id)"  # by anyone
SINCE = f"MAX(COALESCE(e.ready_at, 0), COALESCE(e.processed_at, 0), COALESCE({LAST_GRAB}, 0))"  # the clock's start
KEPT = "EXISTS (SELECT 1 FROM user_episodes ue WHERE ue.episode_id = e.id AND ue.kept = 1)"
# Someone has the podcast, and everyone who has it archived the episode or filters it out.
UNWANTED = f"""(EXISTS (SELECT 1 FROM user_podcasts up WHERE up.podcast_id = e.podcast_id)
    AND NOT EXISTS (
        SELECT 1 FROM user_podcasts up
        LEFT JOIN user_episodes ua ON ua.user_id = up.user_id AND ua.episode_id = e.id
        WHERE up.podcast_id = e.podcast_id AND COALESCE(ua.archived, 0) = 0 AND NOT {hidden_from("up.user_id")}))"""


def _unlink(path: str | None) -> None:
    if path:
        Path(path).unlink(missing_ok=True)


def clear(db: sqlite3.Connection, ep: sqlite3.Row, now: float | None = None) -> None:
    """Delete an episode's download, cut file, waveform and transcripts. A ready one becomes `expired`, to be
    downloaded again and cut the same way, and keeps its captions (CAPTIONS_DAYS); any other keeps its state."""
    for path in (ep["original_path"], ep["cut_path"], ep["peaks_path"]):
        _unlink(path)
    saved = json.loads(ep["transcripts"] or "[]")
    heard = [t for t in saved if t.get("made")] if ep["state"] == READY else []
    chapters.forget({"transcripts": json.dumps([t for t in saved if t not in heard])})
    gone = ("original_path = NULL, cut_path = NULL, peaks_path = NULL, cut_spec = NULL, cut_chapters = NULL, "
            "source_chapters = NULL, transcripts = ?")  # chapters are looked for again on the next download
    with transaction(db):
        if ep["state"] == READY:
            db.execute(f"UPDATE episodes SET state = 'expired', state_note = NULL, {gone}, ready_at = NULL, "
                       "expired_at = ? WHERE id = ?", (json.dumps(heard) if heard else None, now or time.time(),
                                                       ep["id"]))
        else:
            db.execute(f"UPDATE episodes SET {gone} WHERE id = ?", (None, ep["id"]))


def expire_due(db: sqlite3.Connection, now: float | None = None) -> list[int]:
    """Delete the files of every episode whose time is up, or that nobody wants. Returns the ids that expired."""
    now = now or time.time()
    due = db.execute(
        f"""SELECT e.* FROM episodes e JOIN podcasts p ON p.id = e.podcast_id
            WHERE e.state = 'ready' AND NOT {KEPT}
              AND ((e.ready_at IS NOT NULL AND p.retention_days > 0 AND {SINCE} + p.retention_days * {DAY} <= :now)
                   OR ({SINCE} + {UNWANTED_AFTER} <= :now AND {UNWANTED}))""",
        {"now": now}).fetchall()
    with transaction(db):
        for ep in due:
            clear(db, ep, now)
    # Downloads that never became ready (a failed job, companions of a cancelled one) go on the same clock,
    # or now if nobody wants them.
    leftovers = db.execute(
        f"""SELECT e.id, e.original_path, p.retention_days, {UNWANTED} AS unwanted
            FROM episodes e JOIN podcasts p ON p.id = e.podcast_id
            WHERE e.original_path IS NOT NULL AND COALESCE(e.state, '') NOT IN ({sql(IN_USE)})
              AND (p.retention_days > 0 OR {UNWANTED}) AND NOT {KEPT}""").fetchall()
    with transaction(db):
        for ep in leftovers:
            path = Path(ep["original_path"])
            if (ep["unwanted"] or not path.exists()
                    or path.stat().st_mtime + ep["retention_days"] * DAY <= now):
                _unlink(ep["original_path"])
                db.execute("UPDATE episodes SET original_path = NULL WHERE id = ?", (ep["id"],))
    stale = db.execute(
        f"""SELECT e.id, e.transcripts FROM episodes e WHERE e.state = 'expired' AND e.transcripts IS NOT NULL
              AND e.expired_at + {CAPTIONS_DAYS * DAY} <= ? AND NOT {KEPT}""", (now,)).fetchall()
    with transaction(db):
        for ep in stale:
            chapters.forget(ep)
            db.execute("UPDATE episodes SET transcripts = NULL WHERE id = ?", (ep["id"],))
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


def expires_at(ep: sqlite3.Row, retention_days: int | None) -> float | None:
    """When a ready episode's download goes: `retention_days` after it was last made ready, processed or grabbed
    (`last_grab_at`, as library.py's rows have it)."""
    if not ep["ready_at"] or not retention_days:
        return None
    return max(ep["ready_at"], ep["processed_at"] or 0, ep["last_grab_at"] or 0) + retention_days * DAY
