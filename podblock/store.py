"""SQLite store for podcasts, episodes, common audio blocks and their fingerprints.

One store holds every feed, so blocks shared across podcasts (network promos,
ads) are found too. Each block has a type. `repeated` blocks, and the bumpers
tagged automatically, are checked against every episode; `intro`, `outro`
and `show` blocks only against episodes of the podcast they came from.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .fingerprint import Fingerprint, FingerprintConfig, frames_to_seconds
from .matcher import MatchParams, find_runs, match_hashes

SCHEMA_VERSION = "2"

REPEATED, INTRO, OUTRO, SHOW = "repeated", "intro", "outro", "show"
BUMPER_IN, BUMPER_OUT, BUMPER = "bumper-in", "bumper-out", "bumper"
TYPES = (REPEATED, INTRO, OUTRO, SHOW, BUMPER_IN, BUMPER_OUT, BUMPER)
PODCAST_ONLY = {INTRO, OUTRO, SHOW}  # only checked against their own podcast

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS podcasts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    feed_url TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    guid TEXT UNIQUE NOT NULL,
    podcast_id INTEGER REFERENCES podcasts(id),
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
    podcast_id INTEGER REFERENCES podcasts(id),  -- podcast it was found in
    source_start REAL NOT NULL,
    source_end REAL NOT NULL,
    label TEXT,
    type TEXT NOT NULL DEFAULT 'repeated',
    type_manual INTEGER NOT NULL DEFAULT 0,  -- set by hand; never retagged automatically
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


class StoreError(Exception):
    pass


class Store:
    def __init__(self, path: Path, config: FingerprintConfig | None = None):
        """Open or create a store.

        A store keeps one fingerprint setting for life. `config` picks it for a
        new store; for an existing one it must match (or be None).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "episodes" in tables and "settings" not in tables:
            raise StoreError(f"{path} was made by an older pod-block; start a new --data-dir")
        self.db.executescript(SCHEMA)
        self.config = self._settle_config(config)
        self._index: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None

    def _settle_config(self, wanted: FingerprintConfig | None) -> FingerprintConfig:
        rows = dict(self.db.execute("SELECT key, value FROM settings").fetchall())
        if not rows:
            config = wanted or FingerprintConfig()
            self.db.executemany("INSERT INTO settings VALUES (?, ?)",
                                [("schema", SCHEMA_VERSION), ("fingerprint", config.to_json())])
            self.db.commit()
            return config
        if rows.get("schema") != SCHEMA_VERSION:
            raise StoreError("this store was made by another pod-block version; start a new --data-dir")
        stored = FingerprintConfig.from_json(rows["fingerprint"])
        if wanted is not None and wanted != stored:
            raise StoreError(f"this store uses '{stored.name}' fingerprints, not '{wanted.name}'; "
                             "fingerprints from different settings never match, so use a new --data-dir")
        return stored

    def close(self) -> None:
        self.db.close()

    # --- podcasts -----------------------------------------------------------

    def podcast_id(self, name: str | None = None, feed_url: str | None = None) -> int:
        """Find or create a podcast by feed URL, or by name when there is no feed."""
        if feed_url:
            row = self.db.execute("SELECT id FROM podcasts WHERE feed_url = ?", (feed_url,)).fetchone()
        else:
            row = self.db.execute("SELECT id FROM podcasts WHERE name = ? AND feed_url IS NULL",
                                  (name,)).fetchone()
        if row:
            if name and feed_url:
                self.db.execute("UPDATE podcasts SET name = ? WHERE id = ?", (name, row["id"]))
                self.db.commit()
            return row["id"]
        cur = self.db.execute("INSERT INTO podcasts (name, feed_url) VALUES (?, ?)",
                              (name or feed_url, feed_url))
        self.db.commit()
        return cur.lastrowid

    def find_podcast(self, key: str) -> int | None:
        """A podcast id from its id, feed URL or name."""
        row = self.db.execute(
            "SELECT id FROM podcasts WHERE CAST(id AS TEXT) = ? OR feed_url = ? OR name = ?",
            (key, key, key),
        ).fetchone()
        return row["id"] if row else None

    def podcasts(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT p.*, COUNT(DISTINCT e.id) AS episodes,
                   (SELECT COUNT(*) FROM segments s WHERE s.podcast_id = p.id) AS blocks
            FROM podcasts p LEFT JOIN episodes e ON e.podcast_id = p.id
            GROUP BY p.id ORDER BY p.id
            """
        ).fetchall()

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

    def episode_by_guid(self, guid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM episodes WHERE guid = ?", (guid,)).fetchone()

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
        podcast = None
        if source_episode_id is not None:
            podcast = self.episode(source_episode_id)["podcast_id"]
        cur = self.db.execute(
            "INSERT INTO segments (source_episode_id, podcast_id, source_start, source_end, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_episode_id, podcast, source_start, source_end, time.time()),
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

    def set_type(self, segment_id: int, block_type: str | None) -> None:
        """Set a block's type by hand, or with None hand it back to automatic tagging."""
        if block_type is None:
            self.db.execute("UPDATE segments SET type = ?, type_manual = 0 WHERE id = ?",
                            (REPEATED, segment_id))
        elif block_type in TYPES:
            self.db.execute("UPDATE segments SET type = ?, type_manual = 1 WHERE id = ?",
                            (block_type, segment_id))
        else:
            raise ValueError(f"unknown type {block_type!r}; pick one of {', '.join(TYPES)}")
        self.db.commit()

    def set_auto_types(self, types: dict[int, str]) -> None:
        """Retag every block not typed by hand: the given type, else `repeated`."""
        self.db.execute("UPDATE segments SET type = ? WHERE type_manual = 0", (REPEATED,))
        self.db.executemany("UPDATE segments SET type = ? WHERE id = ? AND type_manual = 0",
                            [(t, seg_id) for seg_id, t in types.items()])
        self.db.commit()

    def types(self) -> dict[int, str]:
        return dict(self.db.execute("SELECT id, type FROM segments").fetchall())

    def segment(self, segment_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()

    def segments(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM segments ORDER BY id").fetchall()

    def segment_summary(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT s.id, s.label, s.type, s.type_manual, s.podcast_id, s.source_start, s.source_end,
                   s.source_end - s.source_start AS duration,
                   e.title AS source_title, p.name AS podcast,
                   COUNT(o.id) AS occurrences,
                   COUNT(DISTINCT o.episode_id) AS episodes
            FROM segments s
            LEFT JOIN episodes e ON e.id = s.source_episode_id
            LEFT JOIN podcasts p ON p.id = s.podcast_id
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

    def visible_segments(self, podcast_id: int | None) -> np.ndarray:
        """Ids of the blocks that apply to an episode of `podcast_id`."""
        scoped = ", ".join("?" * len(PODCAST_ONLY))
        rows = self.db.execute(
            f"SELECT id FROM segments WHERE type NOT IN ({scoped}) OR podcast_id IS ?",
            (*PODCAST_ONLY, podcast_id),
        ).fetchall()
        return np.array([r[0] for r in rows], dtype=np.int64)

    def scan(self, fp: Fingerprint, params: MatchParams, podcast_id: int | None = None,
             everything: bool = False) -> list[SegmentMatch]:
        """Find the stored blocks that appear in `fp`.

        Only blocks that apply to `podcast_id` are used, unless `everything`.
        """
        hashes, seg_ids, seg_times = self._load_index()
        if not everything:
            keep = np.isin(seg_ids, self.visible_segments(podcast_id))
            hashes, seg_ids, seg_times = hashes[keep], seg_ids[keep], seg_times[keep]
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
        """Parts of `fp` (in seconds) that no stored block, of any podcast, already covers."""
        covered = sorted((m.start, m.end) for m in self.scan(fp, params, everything=True))
        gaps, cursor = [], 0.0
        for start, end in covered:
            if start > cursor:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if fp.duration > cursor:
            gaps.append((cursor, fp.duration))
        return gaps

