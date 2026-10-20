"""How much room the downloads take, each episode's files and the media volume's, and deleting them."""

from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import jobs, retention
from .db import transaction
from .library import MAX_IDS


def size(path: str | None) -> int:
    """A file's size in bytes; 0 if it is gone."""
    try:
        return Path(path).stat().st_size if path else 0
    except OSError:
        return 0


def on_disk(db: sqlite3.Connection, sort: str = "size") -> list[dict]:
    """Every episode with a file under the media folder, with the size of each file, sorted."""
    rows = db.execute(
        """SELECT e.*, p.name AS podcast_name, p.image_url AS podcast_image, p.followed
           FROM episodes e JOIN podcasts p ON p.id = e.podcast_id
           WHERE e.original_path IS NOT NULL OR e.cut_path IS NOT NULL OR e.peaks_path IS NOT NULL""").fetchall()
    out = []
    for r in rows:
        original, cut, peaks = size(r["original_path"]), size(r["cut_path"]), size(r["peaks_path"])
        out.append({"e": r, "original": original, "cut": cut, "total": original + cut + peaks,
                    "when": r["processed_at"] or r["ready_at"] or 0})
    key = {"size": lambda d: (-d["total"], -d["when"]), "recent": lambda d: -d["when"],
           "podcast": lambda d: ((d["e"]["podcast_name"] or "").lower(), -d["when"])}[sort]
    return sorted(out, key=key)


def disk(media_dir: Path):
    """The media volume's total, used and free bytes, or None if it can't be read."""
    try:
        return shutil.disk_usage(media_dir)
    except OSError:
        return None


def space(db: sqlite3.Connection, media_dir: Path) -> dict:
    """How much room the downloads take (`used`), and the media volume's (`disk`), for the home page."""
    return {"used": sum(d["total"] for d in on_disk(db)), "disk": disk(media_dir)}


@dataclass
class Deleted:
    deleted: int = 0  # episodes whose files went
    skipped: int = 0  # being processed, so left alone
    freed: int = 0  # bytes


def delete(db: sqlite3.Connection, ids: list[int]) -> Deleted:
    """Delete the files of the episodes picked. Ready ones show as cleaned up and can be downloaded again;
    ones being processed are left alone."""
    ids = list(dict.fromkeys(ids))[:MAX_IDS]
    if not ids:
        return Deleted()
    rows = db.execute(f"SELECT * FROM episodes WHERE id IN ({', '.join('?' * len(ids))})", ids).fetchall()
    done, now = Deleted(), time.time()
    with transaction(db):
        for ep in rows:
            if ep["state"] in jobs.ACTIVE:
                done.skipped += 1
                continue
            done.freed += sum(size(ep[k]) for k in ("original_path", "cut_path", "peaks_path"))
            retention.clear(db, ep, now)
            done.deleted += 1
    return done
