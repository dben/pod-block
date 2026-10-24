"""Podcasts and episodes as each user sees them: reading them for pages, each user's archived and kept flags,
the podcasts they picked, a podcast's settings, and queuing a selection to download."""

from __future__ import annotations

import json
import sqlite3
import time

from . import jobs
from .db import in_list, transaction
from .filters import HIDDEN
from .states import DOWNLOADED, sql

# Fragments of the queries below. Those for one user name them `:user_id`, so a query using them takes
# named parameters, and passes the user once however many fragments it uses.
ARCHIVED = "COALESCE(ue.archived, 0)"
KEPT_ANY = "EXISTS (SELECT 1 FROM user_episodes k WHERE k.episode_id = e.id AND k.kept = 1)"
WAS_DOWNLOADED = f"e.state IN ({sql(DOWNLOADED)})"
USER_JOIN = "LEFT JOIN user_episodes ue ON ue.episode_id = e.id AND ue.user_id = :user_id"
GRABBED = "EXISTS (SELECT 1 FROM grabs g WHERE g.episode_id = e.id AND g.user_id = :user_id)"
GRABS = ("(SELECT COUNT(*) FROM grabs g WHERE g.episode_id = e.id AND g.user_id = :user_id) AS grabs, "
         "(SELECT MAX(g.last_at) FROM grabs g WHERE g.episode_id = e.id AND g.user_id = :user_id) AS grabbed_at")
MAX_IDS = 5000  # per bulk action
RETENTION_DAYS = (0, 3650)  # the range of each podcast setting
REFRESH_MINUTES = (15, 7 * 24 * 60)
FILL_GAPS = (0, 120)
MAX_BREAK = (60, 1800)


class SettingsError(ValueError):
    """A podcast's settings can't be saved; the message says why, for the page."""


def podcasts(db: sqlite3.Connection, user_id: int, mine: bool = True) -> list[sqlite3.Row]:
    """The podcasts a user picked for their home page, or every followed one; `subscribed` says which are theirs.
    `episode_count` is how many are in the user's list: not archived by them, nor hidden by a filter."""
    return db.execute(
        f"""
        SELECT p.*, up.user_id IS NOT NULL AS subscribed, MAX(e.pub_ts) AS latest_ts,
               COALESCE(SUM(COALESCE(ue.archived, 0) = 0), 0) AS episode_count,
               COALESCE(SUM(e.state = 'ready'), 0) AS ready_count,
               COALESCE(SUM(e.state = 'expired' AND COALESCE(ue.archived, 0) = 0), 0) AS expired_count,
               COALESCE(SUM(CASE WHEN e.cut_duration IS NOT NULL THEN e.duration - e.cut_duration END), 0) AS cut_sec
        FROM podcasts p {'' if mine else 'LEFT '}JOIN user_podcasts up ON up.podcast_id = p.id AND up.user_id = :user_id
        LEFT JOIN episodes e ON e.podcast_id = p.id AND NOT {HIDDEN}
        {USER_JOIN}
        WHERE p.followed = 1
        GROUP BY p.id ORDER BY p.name COLLATE NOCASE
        """, {"user_id": user_id}
    ).fetchall()


def by_feed(db: sqlite3.Connection, user_id: int, feed_urls: list[str]) -> dict[str, sqlite3.Row]:
    """The followed podcasts at these feed addresses, by address; `subscribed` says which are the user's."""
    if not feed_urls:
        return {}
    rows = db.execute(
        f"""
        SELECT p.id, p.feed_url, up.user_id IS NOT NULL AS subscribed FROM podcasts p
        LEFT JOIN user_podcasts up ON up.podcast_id = p.id AND up.user_id = ?
        WHERE p.followed = 1 AND p.feed_url IN ({', '.join('?' * len(feed_urls))})
        """, (user_id, *feed_urls)).fetchall()
    return {r["feed_url"]: r for r in rows}


def subscribed(db: sqlite3.Connection, user_id: int, podcast_id: int) -> bool:
    return db.execute("SELECT 1 FROM user_podcasts WHERE user_id = ? AND podcast_id = ?",
                      (user_id, podcast_id)).fetchone() is not None


def subscribe(db: sqlite3.Connection, user_id: int, podcast_id: int, on: bool = True) -> None:
    """Put a podcast on a user's home page, or take it off."""
    with transaction(db):
        if on:
            db.execute("INSERT OR IGNORE INTO user_podcasts (user_id, podcast_id, added_at) VALUES (?, ?, ?)",
                       (user_id, podcast_id, time.time()))
        else:
            db.execute("DELETE FROM user_podcasts WHERE user_id = ? AND podcast_id = ?", (user_id, podcast_id))


def podcast(db: sqlite3.Connection, podcast_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM podcasts WHERE id = ? AND followed = 1", (podcast_id,)).fetchone()


def followed_count(db: sqlite3.Connection) -> int:
    """How many podcasts are followed, whoever picked them."""
    return db.execute("SELECT COUNT(*) FROM podcasts WHERE followed = 1").fetchone()[0]


def cut_seconds(db: sqlite3.Connection, podcast_id: int) -> float:
    """How much has been cut from a podcast's episodes so far."""
    return db.execute("SELECT COALESCE(SUM(duration - cut_duration), 0) FROM episodes WHERE podcast_id = ? "
                      "AND cut_duration IS NOT NULL", (podcast_id,)).fetchone()[0]


def save_settings(db: sqlite3.Connection, podcast_id: int, retention_days: int, refresh_minutes: int,
                  fill_gaps: float, auto_download: bool, oldest_first: bool, max_break: float = 480) -> bool:
    """Save a podcast's settings, shared by everyone. Returns whether a setting that decides the cuts changed
    (the gap of new audio cut between two repeats, the longest ad break): episodes cut before keep the old cuts
    until they are processed again."""
    if not (RETENTION_DAYS[0] <= retention_days <= RETENTION_DAYS[1]
            and REFRESH_MINUTES[0] <= refresh_minutes <= REFRESH_MINUTES[1]
            and FILL_GAPS[0] <= fill_gaps <= FILL_GAPS[1]
            and MAX_BREAK[0] <= max_break <= MAX_BREAK[1]):
        raise SettingsError("Those settings are out of range.")
    with transaction(db):
        old = db.execute("SELECT fill_gaps, max_break FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
        db.execute("UPDATE podcasts SET retention_days = ?, refresh_minutes = ?, fill_gaps = ?, max_break = ?, "
                   "auto_download = ?, sort_oldest = ? WHERE id = ?",
                   (retention_days, refresh_minutes, fill_gaps, max_break, int(auto_download), int(oldest_first),
                    podcast_id))
    return old is not None and (old["fill_gaps"], old["max_break"]) != (fill_gaps, max_break)


def episode(db: sqlite3.Connection, user_id: int, episode_id: int) -> sqlite3.Row | None:
    """An episode of a followed podcast as a user sees it: with their `archived` and `kept` flags, whether anyone
    keeps it (`kept_any`), their grabs (as `episodes` gives them), and its podcast's `retention_days`."""
    return db.execute(
        f"""SELECT e.*, {ARCHIVED} AS archived, COALESCE(ue.kept, 0) AS kept, {KEPT_ANY} AS kept_any, {GRABS},
                   p.retention_days
            FROM episodes e JOIN podcasts p ON p.id = e.podcast_id AND p.followed = 1
            {USER_JOIN}
            WHERE e.id = :episode_id""", {"user_id": user_id, "episode_id": episode_id}).fetchone()


def report(db: sqlite3.Connection, episode_id: int) -> dict | None:
    """What the detector found in an episode when it was last processed: its sections, and what each one is."""
    row = db.execute("SELECT report FROM reports WHERE episode_id = ?", (episode_id,)).fetchone()
    return json.loads(row["report"]) if row else None


def download(db: sqlite3.Connection, user_id: int, podcast_id: int, ids: list[int],
             again: bool = False) -> tuple[list[int], int]:
    """Queue a podcast's picked episodes to be downloaded and cut; `again` cuts the ready ones again too. Returns
    the ones queued, and how many of the podcast's were picked: the others were ready or queued already."""
    ids = list(dict.fromkeys(ids))[:MAX_IDS]
    if not ids:
        return [], 0
    marks, args = in_list("id", ids)
    query = f"SELECT id FROM episodes WHERE podcast_id = :podcast_id AND id IN ({marks})"
    mine = [r["id"] for r in db.execute(query, {**args, "podcast_id": podcast_id})]
    return jobs.enqueue(db, "process", mine, user_id, skip_ready=not again), len(mine)


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def episodes(db: sqlite3.Connection, user_id: int, podcast_id: int, archived: bool, q: str = "",
             limit: int = 50, offset: int = 0, downloaded: bool = False, filtered: bool = False,
             oldest_first: bool = False, grabbed: bool = False) -> tuple[list[sqlite3.Row], int]:
    """One page of a podcast's episodes, newest first (or oldest), and how many match in all. Each row says
    how often this user grabbed it (`grabs`), and when last (`grabbed_at`).

    `downloaded` keeps only episodes downloaded at some point: queued, ready, failed or cleaned up; `grabbed`
    only the ones this user grabbed. Both narrow any list, together or alone. Episodes a filter hides from this
    user are left out; `filtered` lists only those, archived or not."""
    args = {"user_id": user_id, "podcast_id": podcast_id, "archived": int(archived)}
    if filtered:
        where = f"e.podcast_id = :podcast_id AND {HIDDEN}"
    else:
        where = f"e.podcast_id = :podcast_id AND {ARCHIVED} = :archived AND NOT {HIDDEN}"
    if downloaded:
        where += f" AND {WAS_DOWNLOADED}"
    if grabbed:
        where += f" AND {GRABBED}"
    if q.strip():
        where += " AND e.title LIKE :q ESCAPE '\\'"
        args["q"] = _like(q.strip())
    total = db.execute(f"SELECT COUNT(*) FROM episodes e {USER_JOIN} WHERE {where}", args).fetchone()[0]
    order = "ASC" if oldest_first else "DESC"
    rows = db.execute(
        f"""SELECT e.*, {ARCHIVED} AS archived, COALESCE(ue.kept, 0) AS kept, {KEPT_ANY} AS kept_any, {GRABS}
            FROM episodes e {USER_JOIN} WHERE {where}
            ORDER BY e.pub_ts IS NULL, e.pub_ts {order}, e.id {order} LIMIT :limit OFFSET :offset""",
        {**args, "limit": limit, "offset": offset}).fetchall()
    return rows, total


def counts(db: sqlite3.Connection, user_id: int, podcast_id: int, downloaded: bool = False,
           grabbed: bool = False) -> dict[str, int]:
    """How many of a podcast's episodes are in each of a user's lists, narrowed as `episodes` narrows them.
    Filtered ones count only as filtered. `episodes` is how many the podcast has outside the filtered list,
    and `expired` how many cleaned-up ones are in the main list, neither narrowed."""
    narrowed = " AND ".join(["1", *(["d"] if downloaded else []), *(["g"] if grabbed else [])])
    row = db.execute(
        f"""SELECT COALESCE(SUM(h = 0), 0) AS episodes, COALESCE(SUM(h = 0 AND a = 0 AND {narrowed}), 0) AS active,
                   COALESCE(SUM(h = 0 AND a = 1 AND {narrowed}), 0) AS archived,
                   COALESCE(SUM(h AND {narrowed}), 0) AS filtered, COALESCE(SUM(h = 0 AND x AND a = 0), 0) AS expired
            FROM (SELECT {ARCHIVED} AS a, {WAS_DOWNLOADED} AS d, e.state = 'expired' AS x, {HIDDEN} AS h, {GRABBED} AS g
                  FROM episodes e {USER_JOIN} WHERE e.podcast_id = :podcast_id)""",
        {"user_id": user_id, "podcast_id": podcast_id}).fetchone()
    return dict(row)


def _set_flag(db: sqlite3.Connection, flag: str, user_id: int, podcast_id: int | None, ids: list[int],
              value: bool) -> list[int]:
    """Set a user's `archived` or `kept` flag on episodes (of one podcast, unless None). Returns the ids that
    changed."""
    assert flag in ("archived", "kept")
    ids = list(dict.fromkeys(ids))[:MAX_IDS]
    if not ids:
        return []
    marks, id_args = in_list("id", ids)
    scope = "e.podcast_id = :podcast_id AND " if podcast_id is not None else ""
    with transaction(db):
        changed = [r["id"] for r in db.execute(
            f"""SELECT e.id FROM episodes e {USER_JOIN}
                WHERE {scope}e.id IN ({marks}) AND COALESCE(ue.{flag}, 0) != :value""",
            {"user_id": user_id, "podcast_id": podcast_id, "value": int(value), **id_args})]
        db.executemany(
            f"""INSERT INTO user_episodes (user_id, episode_id, {flag}) VALUES (?, ?, ?)
                ON CONFLICT (user_id, episode_id) DO UPDATE SET {flag} = excluded.{flag}""",
            [(user_id, ep_id, int(value)) for ep_id in changed])
    return changed


def set_archived(db: sqlite3.Connection, user_id: int, podcast_id: int | None, ids: list[int],
                 archived: bool) -> list[int]:
    """Archive or unarchive episodes of one podcast. Returns the ids that changed."""
    return _set_flag(db, "archived", user_id, podcast_id, ids, archived)


def set_kept(db: sqlite3.Connection, user_id: int, podcast_id: int | None, ids: list[int], kept: bool) -> list[int]:
    """Keep episodes, so they are never cleaned up, or stop keeping them. Returns the ids that changed."""
    return _set_flag(db, "kept", user_id, podcast_id, ids, kept)


def archive_expired(db: sqlite3.Connection, user_id: int, podcast_id: int | None = None) -> list[int]:
    """Archive every cleaned-up episode this user still has in their list, of one podcast or all of theirs."""
    if podcast_id is not None:
        scope = "AND e.podcast_id = :podcast_id"
    else:
        scope = "AND p.id IN (SELECT podcast_id FROM user_podcasts WHERE user_id = :user_id)"
    ids = [r["id"] for r in db.execute(
        f"""SELECT e.id FROM episodes e {USER_JOIN} JOIN podcasts p ON p.id = e.podcast_id AND p.followed = 1
            WHERE e.state = 'expired' AND {ARCHIVED} = 0 AND NOT {HIDDEN} {scope}""",
        {"user_id": user_id, "podcast_id": podcast_id})]
    changed = []
    with transaction(db):
        for start in range(0, len(ids), MAX_IDS):
            changed += set_archived(db, user_id, None, ids[start:start + MAX_IDS], True)
    return changed


def archive_older(db: sqlite3.Connection, user_id: int, podcast_id: int, episode_id: int) -> list[int]:
    """Archive an episode and every one of the podcast published before it."""
    row = db.execute("SELECT pub_ts FROM episodes WHERE id = ? AND podcast_id = ?",
                     (episode_id, podcast_id)).fetchone()
    if row is None:
        return []
    if row["pub_ts"] is None:
        return set_archived(db, user_id, podcast_id, [episode_id], True)
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM episodes WHERE podcast_id = ? AND (pub_ts <= ? OR id = ?)",
        (podcast_id, row["pub_ts"], episode_id))]
    changed = []
    with transaction(db):
        for start in range(0, len(ids), MAX_IDS):
            changed += set_archived(db, user_id, podcast_id, ids[start:start + MAX_IDS], True)
    return changed
