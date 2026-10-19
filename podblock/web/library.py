"""Reading podcasts and episodes for pages, and each user's archived flags."""

from __future__ import annotations

import sqlite3

ARCHIVED = "COALESCE(ue.archived, 0)"
USER_JOIN = "LEFT JOIN user_episodes ue ON ue.episode_id = e.id AND ue.user_id = ?"
MAX_IDS = 5000  # per bulk action


def podcasts(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT p.*, COUNT(e.id) AS episode_count, MAX(e.pub_ts) AS latest_ts
        FROM podcasts p LEFT JOIN episodes e ON e.podcast_id = p.id
        WHERE p.followed = 1
        GROUP BY p.id ORDER BY p.name COLLATE NOCASE
        """
    ).fetchall()


def podcast(db: sqlite3.Connection, podcast_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM podcasts WHERE id = ? AND followed = 1", (podcast_id,)).fetchone()


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def episodes(db: sqlite3.Connection, user_id: int, podcast_id: int, archived: bool, q: str = "",
             limit: int = 50, offset: int = 0) -> tuple[list[sqlite3.Row], int]:
    """One page of a podcast's episodes, newest first, and how many match in all."""
    where = f"e.podcast_id = ? AND {ARCHIVED} = ?"
    args: list = [user_id, podcast_id, int(archived)]
    if q.strip():
        where += " AND e.title LIKE ? ESCAPE '\\'"
        args.append(_like(q.strip()))
    total = db.execute(f"SELECT COUNT(*) FROM episodes e {USER_JOIN} WHERE {where}", args).fetchone()[0]
    rows = db.execute(
        f"""SELECT e.*, {ARCHIVED} AS archived, COALESCE(ue.kept, 0) AS kept
            FROM episodes e {USER_JOIN} WHERE {where}
            ORDER BY e.pub_ts IS NULL, e.pub_ts DESC, e.id DESC LIMIT ? OFFSET ?""",
        [*args, limit, offset]).fetchall()
    return rows, total


def counts(db: sqlite3.Connection, user_id: int, podcast_id: int) -> dict[str, int]:
    row = db.execute(
        f"""SELECT COUNT(*) AS everything, COALESCE(SUM({ARCHIVED}), 0) AS archived
            FROM episodes e {USER_JOIN} WHERE e.podcast_id = ?""", (user_id, podcast_id)).fetchone()
    return {"active": row["everything"] - row["archived"], "archived": row["archived"]}


def set_archived(db: sqlite3.Connection, user_id: int, podcast_id: int, ids: list[int],
                 archived: bool) -> list[int]:
    """Archive or unarchive episodes of one podcast. Returns the ids that changed."""
    ids = list(dict.fromkeys(ids))[:MAX_IDS]
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    changed = [r["id"] for r in db.execute(
        f"""SELECT e.id FROM episodes e {USER_JOIN}
            WHERE e.podcast_id = ? AND e.id IN ({marks}) AND {ARCHIVED} != ?""",
        (user_id, podcast_id, *ids, int(archived)))]
    db.executemany(
        """INSERT INTO user_episodes (user_id, episode_id, archived) VALUES (?, ?, ?)
           ON CONFLICT (user_id, episode_id) DO UPDATE SET archived = excluded.archived""",
        [(user_id, ep_id, int(archived)) for ep_id in changed])
    db.commit()
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
