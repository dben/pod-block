"""How much room the downloads take: each episode's files, and the media volume."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path


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
