"""Episode filters: patterns for episode titles that a user wants hidden.

A filter belongs to a user, and to one podcast or to all of them. It hides
the episodes whose titles it matches from that user's lists; they show in
the Filtered tab instead. Patterns are regular expressions, matched anywhere
in the title, ignoring case.

A filter marked `processing` hides its episodes from everyone, and keeps
them out of finding repeats: a compilation or rewind episode replays whole
earlier episodes, so comparing it with them would mark everything as a
repeat. Such episodes are never downloaded on their own, never taken along
as comparison episodes, and never compared with others. Someone can still
download one by hand; it is then cut using the repeats found elsewhere.
`episodes.skip_processing` marks the episodes such filters match, and when
one that was already compared is marked, an `unlearn` job forgets the
repeats it taught the detector.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

from . import jobs
from .db import title_pattern, transaction

MAX_PATTERN = 200


def hidden_from(user: str) -> str:
    """SQL for: the episode `e` is hidden from `user`, a column or a named parameter."""
    return f"""(e.skip_processing = 1 OR EXISTS (
    SELECT 1 FROM episode_filters f WHERE f.user_id = {user} AND (f.podcast_id IS NULL OR f.podcast_id = e.podcast_id)
    AND e.title REGEXP f.pattern))"""


# An episode is hidden from the user `:user_id` (a named parameter; see library.py).
HIDDEN = hidden_from(":user_id")
_PROCESSING = """EXISTS (
    SELECT 1 FROM episode_filters f WHERE f.processing = 1
    AND (f.podcast_id IS NULL OR f.podcast_id = episodes.podcast_id) AND episodes.title REGEXP f.pattern)"""


class FilterError(ValueError):
    """A filter can't be saved; the message says why, for the page."""


def check(pattern: str) -> str:
    pattern = pattern.strip()
    if not pattern:
        raise FilterError("Type a pattern to match titles against.")
    if len(pattern) > MAX_PATTERN:
        raise FilterError(f"Patterns can be at most {MAX_PATTERN} characters.")
    try:
        compiled = title_pattern(pattern)
    except re.error as e:
        raise FilterError(f"That isn't a valid regular expression: {e}.") from None
    if compiled.search(""):
        raise FilterError("That pattern matches every title.")
    return pattern


def add(db: sqlite3.Connection, user_id: int, podcast_id: int | None, pattern: str, processing: bool) -> int:
    pattern = check(pattern)
    with transaction(db):
        cur = db.execute("INSERT INTO episode_filters (user_id, podcast_id, pattern, processing, created_at) "
                         "VALUES (?, ?, ?, ?, ?)", (user_id, podcast_id, pattern, int(processing), time.time()))
        if processing:
            mark(db, podcast_id)
    return cur.lastrowid


def matches(db: sqlite3.Connection, podcast_id: int, pattern: str) -> int:
    """How many of a podcast's episodes a pattern matches."""
    return db.execute("SELECT COUNT(*) FROM episodes WHERE podcast_id = ? AND title REGEXP ?",
                      (podcast_id, pattern.strip())).fetchone()[0]


def get(db: sqlite3.Connection, filter_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM episode_filters WHERE id = ?", (filter_id,)).fetchone()


def remove(db: sqlite3.Connection, filter_id: int) -> None:
    with transaction(db):
        row = get(db, filter_id)
        if row is None:
            return
        db.execute("DELETE FROM episode_filters WHERE id = ?", (filter_id,))
        if row["processing"]:
            mark(db, row["podcast_id"])


def may_remove(user: sqlite3.Row, row: sqlite3.Row) -> bool:
    return bool(user["is_admin"]) or row["user_id"] == user["id"]


def for_podcast(db: sqlite3.Connection, user_id: int, podcast_id: int) -> list[sqlite3.Row]:
    """The filters that hide this podcast's episodes from a user: their own, and everyone's processing ones.
    Each comes with its owner's name and how many of the podcast's episodes it matches."""
    return db.execute(
        """SELECT f.*, u.username,
                  (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id = ? AND e.title REGEXP f.pattern) AS matches
           FROM episode_filters f LEFT JOIN users u ON u.id = f.user_id
           WHERE (f.user_id = ? OR f.processing = 1) AND (f.podcast_id IS NULL OR f.podcast_id = ?)
           ORDER BY f.processing DESC, f.id""", (podcast_id, user_id, podcast_id)).fetchall()


def mark(db: sqlite3.Connection, podcast_id: int | None = None) -> list[int]:
    """Bring `skip_processing` up to date with the processing filters, for one podcast's episodes or all.

    Returns the episodes newly marked. Those the detector already learned
    from are handed to an `unlearn` job.
    """
    scope, args = ("AND podcast_id = ?", [podcast_id]) if podcast_id is not None else ("", [])
    with transaction(db):
        newly = [r["id"] for r in db.execute(
            f"SELECT id FROM episodes WHERE skip_processing = 0 AND {_PROCESSING} {scope}", args)]
        db.execute(f"UPDATE episodes SET skip_processing = {_PROCESSING} WHERE 1 {scope}", args)
        if newly:
            marks = ", ".join("?" * len(newly))
            learned = [r["id"] for r in db.execute(
                f"""SELECT id FROM episodes WHERE id IN ({marks}) AND (fingerprint_path IS NOT NULL
                    OR id IN (SELECT episode_id FROM occurrences) OR id IN (SELECT source_episode_id FROM segments))""",
                newly)]
            if learned:
                jobs.enqueue_task(db, "unlearn", None, json.dumps({"episodes": learned}))
    return newly
