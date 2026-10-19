"""SQLite store for episodes, common audio segments and their fingerprints."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .fingerprint import Fingerprint, frames_to_seconds
from .matcher import MatchParams, find_runs, match_hashes

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    guid TEXT UNIQUE NOT NULL,
    feed_url TEXT,
    title TEXT,
    pub_date TEXT,
    audio_url TEXT,
    audio_path TEXT,
    fingerprint_path TEXT,
    duration REAL,
    added_at REAL
);
CREATE TABLE IF NOT EXISTS compared_pairs (
    a_id INTEGER NOT NULL,
    b_id INTEGER NOT NULL,
    PRIMARY KEY (a_id, b_id)
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    source_episode_id INTEGER REFERENCES episodes(id),
    source_start REAL NOT NULL,
    source_end REAL NOT NULL,
    label TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS segment_hashes (
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    hash INTEGER NOT NULL,
    t INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS segment_hashes_hash ON segment_hashes(hash);
CREATE INDEX IF NOT EXISTS segment_hashes_segment ON segment_hashes(segment_id);
CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    start REAL NOT NULL,
    end REAL NOT NULL,
    segment_start REAL NOT NULL,
    hits INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS occurrences_episode ON occurrences(episode_id);
CREATE INDEX IF NOT EXISTS occurrences_segment ON occurrences(segment_id);
"""


@dataclass
class SegmentMatch:
    segment_id: int
    start: float  # seconds, in the scanned audio
    end: float
    segment_start: float  # where in the stored segment the match begins
    hits: int


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._index: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None

    def close(self) -> None:
        self.db.close()

    # --- episodes -----------------------------------------------------------

    def upsert_episode(self, guid: str, **fields) -> int:
        row = self.db.execute("SELECT id FROM episodes WHERE guid = ?", (guid,)).fetchone()
        if row is None:
            cols = ["guid", "added_at", *fields]
            vals = [guid, time.time(), *fields.values()]
            cur = self.db.execute(
                f"INSERT INTO episodes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
            self.db.commit()
            return cur.lastrowid
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE episodes SET {sets} WHERE id = ?", [*fields.values(), row["id"]])
            self.db.commit()
        return row["id"]

    def episode(self, episode_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()

    def fingerprinted_episodes(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM episodes WHERE fingerprint_path IS NOT NULL ORDER BY pub_date"
        ).fetchall()

    def was_compared(self, a_id: int, b_id: int) -> bool:
        a_id, b_id = sorted((a_id, b_id))
        return self.db.execute(
            "SELECT 1 FROM compared_pairs WHERE a_id = ? AND b_id = ?", (a_id, b_id)
        ).fetchone() is not None

    def mark_compared(self, a_id: int, b_id: int) -> None:
        a_id, b_id = sorted((a_id, b_id))
        self.db.execute("INSERT OR IGNORE INTO compared_pairs VALUES (?, ?)", (a_id, b_id))
        self.db.commit()

    # --- segments -----------------------------------------------------------

    def add_segment(self, fp: Fingerprint, source_episode_id: int | None,
                    source_start: float, source_end: float) -> int:
        """Store a common segment. `fp` must already be timed relative to the segment."""
        cur = self.db.execute(
            "INSERT INTO segments (source_episode_id, source_start, source_end, created_at) "
            "VALUES (?, ?, ?, ?)",
            (source_episode_id, source_start, source_end, time.time()),
        )
        seg_id = cur.lastrowid
        self.db.executemany(
            "INSERT INTO segment_hashes (segment_id, hash, t) VALUES (?, ?, ?)",
            zip([seg_id] * len(fp.hashes), fp.hashes.tolist(), fp.times.tolist()),
        )
        self.db.commit()
        self._index = None
        return seg_id

    def delete_segment(self, segment_id: int) -> None:
        self.db.execute("DELETE FROM segments WHERE id = ?", (segment_id,))
        self.db.commit()
        self._index = None

    def set_label(self, segment_id: int, label: str | None) -> None:
        self.db.execute("UPDATE segments SET label = ? WHERE id = ?", (label, segment_id))
        self.db.commit()

    def segment(self, segment_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()

    def segments(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM segments ORDER BY id").fetchall()

    def segment_summary(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT s.id, s.label, s.source_start, s.source_end,
                   s.source_end - s.source_start AS duration,
                   e.title AS source_title,
                   COUNT(o.id) AS occurrences,
                   COUNT(DISTINCT o.episode_id) AS episodes
            FROM segments s
            LEFT JOIN episodes e ON e.id = s.source_episode_id
            LEFT JOIN occurrences o ON o.segment_id = s.id
            GROUP BY s.id
            ORDER BY episodes DESC, duration DESC
            """
        ).fetchall()

    def occurrences_for_segment(self, segment_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT o.*, e.title FROM occurrences o JOIN episodes e ON e.id = o.episode_id
            WHERE o.segment_id = ? ORDER BY e.pub_date, o.start
            """,
            (segment_id,),
        ).fetchall()

    def occurrences_for_episode(self, episode_id: int) -> list[SegmentMatch]:
        rows = self.db.execute(
            "SELECT segment_id, start, end, segment_start, hits FROM occurrences "
            "WHERE episode_id = ? ORDER BY start",
            (episode_id,),
        ).fetchall()
        return [SegmentMatch(*r) for r in rows]

    def replace_occurrences(self, episode_id: int, matches: list[SegmentMatch]) -> None:
        self.db.execute("DELETE FROM occurrences WHERE episode_id = ?", (episode_id,))
        self.db.executemany(
            "INSERT INTO occurrences (segment_id, episode_id, start, end, segment_start, hits) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(m.segment_id, episode_id, m.start, m.end, m.segment_start, m.hits) for m in matches],
        )
        self.db.commit()

    # --- matching -----------------------------------------------------------

    def _load_index(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if self._index is None:
            rows = self.db.execute("SELECT hash, segment_id, t FROM segment_hashes").fetchall()
            if rows:
                arr = np.array(rows, dtype=np.int64)
                self._index = (arr[:, 0].astype(np.uint32), arr[:, 1], arr[:, 2])
            else:
                empty = np.zeros(0, np.int64)
                self._index = (empty.astype(np.uint32), empty, empty)
        return self._index

    def scan(self, fp: Fingerprint, params: MatchParams) -> list[SegmentMatch]:
        """Find every stored segment that appears in `fp`."""
        hashes, seg_ids, seg_times = self._load_index()
        iq, iref = match_hashes(fp.hashes, hashes, params.max_hash_occurrences)
        if len(iq) == 0:
            return []
        matched_seg = seg_ids[iref]
        order = np.argsort(matched_seg, kind="stable")
        matched_seg = matched_seg[order]
        tq = fp.times[iq][order]
        tref = seg_times[iref][order]

        bounds = np.nonzero(np.diff(matched_seg))[0] + 1
        starts = np.concatenate(([0], bounds))
        ends = np.concatenate((bounds, [len(matched_seg)]))
        found: list[SegmentMatch] = []
        for s, e in zip(starts, ends):
            for run in find_runs(tq[s:e], tref[s:e], params):
                found.append(SegmentMatch(
                    segment_id=int(matched_seg[s]),
                    start=frames_to_seconds(run.a_start),
                    end=frames_to_seconds(run.a_end),
                    segment_start=frames_to_seconds(run.b_start),
                    hits=run.hits,
                ))
        return sorted(found, key=lambda m: m.start)

    def uncovered(self, fp: Fingerprint, params: MatchParams) -> list[tuple[float, float]]:
        """Parts of `fp` (in seconds) that no stored segment already covers."""
        covered = sorted((m.start, m.end) for m in self.scan(fp, params))
        gaps, cursor = [], 0.0
        for start, end in covered:
            if start > cursor:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if fp.duration > cursor:
            gaps.append((cursor, fp.duration))
        return gaps

