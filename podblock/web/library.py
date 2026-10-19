"""Reading podcasts and episodes for pages, and each user's archived and kept flags."""

from __future__ import annotations

import sqlite3

ARCHIVED = "COALESCE(ue.archived, 0)"
KEPT_ANY = "EXISTS (SELECT 1 FROM user_episodes k WHERE k.episode_id = e.id AND k.kept = 1)"
DOWNLOADED = "e.state IN ('queued', 'running', 'ready', 'failed', 'expired')"
USER_JOIN = "LEFT JOIN user_episodes ue ON ue.episode_id = e.id AND ue.user_id = ?"
MAX_IDS = 5000  # per bulk action


def podcasts(db: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT p.*, COUNT(e.id) AS episode_count, MAX(e.pub_ts) AS latest_ts,
               COALESCE(SUM(e.state = 'ready'), 0) AS ready_count,
               COALESCE(SUM(e.state = 'expired' AND COALESCE(ue.archived, 0) = 0), 0) AS expired_count
        FROM podcasts p LEFT JOIN episodes e ON e.podcast_id = p.id
        LEFT JOIN user_episodes ue ON ue.episode_id = e.id AND ue.user_id = ?
        WHERE p.followed = 1
        GROUP BY p.id ORDER BY p.name COLLATE NOCASE
        """, (user_id,)
    ).fetchall()


def podcast(db: sqlite3.Connection, podcast_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM podcasts WHERE id = ? AND followed = 1", (podcast_id,)).fetchone()


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def episodes(db: sqlite3.Connection, user_id: int, podcast_id: int, archived: bool, q: str = "",
             limit: int = 50, offset: int = 0, downloaded: bool = False) -> tuple[list[sqlite3.Row], int]:
    """One page of a podcast's episodes, newest first, and how many match in all.

    `downloaded` keeps only episodes downloaded at some point: queued, ready, failed or cleaned up."""
    where = f"e.podcast_id = ? AND {ARCHIVED} = ?" + (f" AND {DOWNLOADED}" if downloaded else "")
    args: list = [user_id, podcast_id, int(archived)]
    if q.strip():
        where += " AND e.title LIKE ? ESCAPE '\\'"
        args.append(_like(q.strip()))
    total = db.execute(f"SELECT COUNT(*) FROM episodes e {USER_JOIN} WHERE {where}", args).fetchone()[0]
    rows = db.execute(
        f"""SELECT e.*, {ARCHIVED} AS archived, COALESCE(ue.kept, 0) AS kept, {KEPT_ANY} AS kept_any
            FROM episodes e {USER_JOIN} WHERE {where}
            ORDER BY e.pub_ts IS NULL, e.pub_ts DESC, e.id DESC LIMIT ? OFFSET ?""",
        [*args, limit, offset]).fetchall()
    return rows, total


def counts(db: sqlite3.Connection, user_id: int, podcast_id: int) -> dict[str, int]:
    row = db.execute(
        f"""SELECT COUNT(*) AS everything, COALESCE(SUM({ARCHIVED}), 0) AS archived,
                   COALESCE(SUM({DOWNLOADED} AND {ARCHIVED} = 0), 0) AS downloaded,
                   COALESCE(SUM(e.state = 'expired' AND {ARCHIVED} = 0), 0) AS expired
            FROM episodes e {USER_JOIN} WHERE e.podcast_id = ?""", (user_id, podcast_id)).fetchone()
    return {"active": row["everything"] - row["archived"], "archived": row["archived"],
            "downloaded": row["downloaded"], "expired": row["expired"]}


def _set_flag(db: sqlite3.Connection, flag: str, user_id: int, podcast_id: int | None, ids: list[int],
              value: bool) -> list[int]:
    """Set a user's `archived` or `kept` flag on episodes (of one podcast, unless None). Returns the ids that changed."""
    assert flag in ("archived", "kept")
    ids = list(dict.fromkeys(ids))[:MAX_IDS]
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    scope = "e.podcast_id = ? AND " if podcast_id is not None else ""
    changed = [r["id"] for r in db.execute(
        f"""SELECT e.id FROM episodes e {USER_JOIN}
            WHERE {scope}e.id IN ({marks}) AND COALESCE(ue.{flag}, 0) != ?""",
        (user_id, *([podcast_id] if podcast_id is not None else []), *ids, int(value)))]
    db.executemany(
        f"""INSERT INTO user_episodes (user_id, episode_id, {flag}) VALUES (?, ?, ?)
            ON CONFLICT (user_id, episode_id) DO UPDATE SET {flag} = excluded.{flag}""",
        [(user_id, ep_id, int(value)) for ep_id in changed])
    db.commit()
    return changed


def set_archived(db: sqlite3.Connection, user_id: int, podcast_id: int | None, ids: list[int],
                 archived: bool) -> list[int]:
    """Archive or unarchive episodes of one podcast. Returns the ids that changed."""
    return _set_flag(db, "archived", user_id, podcast_id, ids, archived)


def set_kept(db: sqlite3.Connection, user_id: int, podcast_id: int | None, ids: list[int], kept: bool) -> list[int]:
    """Keep episodes, so they are never cleaned up, or stop keeping them. Returns the ids that changed."""
    return _set_flag(db, "kept", user_id, podcast_id, ids, kept)


def archive_expired(db: sqlite3.Connection, user_id: int, podcast_id: int | None = None) -> list[int]:
    """Archive every cleaned-up episode this user still has in their list, of one podcast or all of them."""
    scope = "AND e.podcast_id = ?" if podcast_id is not None else ""
    ids = [r["id"] for r in db.execute(
        f"""SELECT e.id FROM episodes e {USER_JOIN} JOIN podcasts p ON p.id = e.podcast_id AND p.followed = 1
            WHERE e.state = 'expired' AND {ARCHIVED} = 0 {scope}""",
        (user_id, *([podcast_id] if podcast_id is not None else [])))]
    changed = []
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
    for start in range(0, len(ids), MAX_IDS):
        changed += set_archived(db, user_id, podcast_id, ids[start:start + MAX_IDS], True)
    return changed
