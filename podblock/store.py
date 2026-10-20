"""SQLite store for podcasts, episodes, common audio blocks and their fingerprints.

One store holds every feed, so blocks shared across podcasts (network promos,
ads) are found too. Each block has a type. `repeated` blocks, and the bumpers
tagged automatically, are checked against every episode; `intro`, `outro`
and `show` blocks only against episodes of the podcast they came from.
`ignored` blocks, false positives flagged by hand, are never reported; they
stay stored only so the same audio is not taken up as a new block.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from .fingerprint import Fingerprint, FingerprintConfig, frames_to_seconds
from .matcher import MatchParams, find_runs, match_hashes

SCHEMA_VERSION = "3"

REPEATED, INTRO, OUTRO, SHOW = "repeated", "intro", "outro", "show"
BUMPER_IN, BUMPER_OUT, BUMPER = "bumper-in", "bumper-out", "bumper"
IGNORED = "ignored"  # a false positive: kept so it is not found again, otherwise skipped
TYPES = (REPEATED, INTRO, OUTRO, SHOW, BUMPER_IN, BUMPER_OUT, BUMPER, IGNORED)
PODCAST_ONLY = {INTRO, OUTRO, SHOW}  # only checked against their own podcast

SEGMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    source_episode_id INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
    podcast_id INTEGER REFERENCES podcasts(id),  -- podcast it was found in
    source_start REAL NOT NULL,
    source_end REAL NOT NULL,
    label TEXT,
    type TEXT NOT NULL DEFAULT 'repeated',
    type_manual INTEGER NOT NULL DEFAULT 0,  -- set by hand; never retagged automatically
    confirmed INTEGER NOT NULL DEFAULT 0,  -- automatic bumper tag with enough evidence to keep
    hashes BLOB NOT NULL,  -- uint32 per hash, little-endian
    times BLOB NOT NULL,  -- int32 frame of each hash from the block start
    created_at REAL,
    last_seen REAL  -- when a scan last heard it
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS podcasts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    feed_url TEXT UNIQUE,
    outro_bed INTEGER NOT NULL DEFAULT 0  -- episodes end with talk over a music bed
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
{segments}CREATE TABLE IF NOT EXISTS occurrences (
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
CREATE TABLE IF NOT EXISTS reports (
    episode_id INTEGER PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    report TEXT NOT NULL  -- JSON, as `podblock report --json` prints it
);
""".replace("{segments}", SEGMENTS_TABLE.format(name="segments"))


@dataclass
class SegmentMatch:
    segment_id: int
    start: float  # seconds, in the scanned audio
    end: float
    segment_start: float  # where in the stored segment the match begins
    hits: int


class StoreError(Exception):
    pass


class Connection(sqlite3.Connection):
    """A connection that knows how many `transaction` blocks it is inside."""

    depth = 0


@contextmanager
def transaction(db: Connection) -> Iterator[None]:
    """Make the writes in the block one transaction, committed at its end or rolled back if it raises.

    A block inside another is a savepoint of that one: a failure rolls back the inner block's writes, and the
    outermost block commits. So a function that writes wraps its writes in one, and a caller that needs several
    such functions to land together wraps them all in another.

    The outermost block takes the write lock at once (BEGIN IMMEDIATE), so what it reads can't change before it
    writes. Other writers, in this process or another, wait while it is open: keep slow work out of it.
    """
    outer = db.depth == 0
    if outer:
        if db.in_transaction:
            raise RuntimeError("a write was left uncommitted outside a transaction() block")
        db.execute("BEGIN IMMEDIATE")
    else:
        db.execute("SAVEPOINT nested")
    db.depth += 1
    try:
        yield
    except BaseException:
        if outer:
            db.rollback()
        elif db.in_transaction:  # some errors end the whole transaction in SQLite
            db.execute("ROLLBACK TO nested")
            db.execute("RELEASE nested")
        raise
    else:
        if outer:
            db.commit()
        else:
            db.execute("RELEASE nested")
    finally:
        db.depth -= 1


class Store:
    def __init__(self, path: Path, config: FingerprintConfig | None = None):
        """Open or create a store.

        A store keeps one fingerprint setting for life. `config` picks it for a
        new store; for an existing one it must match (or be None).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, factory=Connection)
        self._owned = True
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "episodes" in tables and "settings" not in tables:
            raise StoreError(f"{path} was made by an older pod-block; start a new --data-dir")
        if "segment_hashes" in tables:
            self._upgrade_from_2()
        self.db.executescript(SCHEMA)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(segments)")}
        if "confirmed" not in columns:  # stores made before bumpers were confirmed
            self.db.execute("ALTER TABLE segments ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
        self.config = self._settle_config(config)
        self._index: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None

    @classmethod
    def on(cls, db: Connection, config: FingerprintConfig | None = None) -> Store:
        """A store on a connection opened elsewhere, to a database that `Store(path)` has set up. Its writes join
        that connection's transactions, and closing it leaves the connection open."""
        store = cls.__new__(cls)
        store.db, store._owned = db, False
        store.config = store._settle_config(config)
        store._index = None
        return store

    def _upgrade_from_2(self) -> None:
        """Move block hashes from one row each into packed columns (schema 2 to 3)."""
        db = self.db
        db.execute("PRAGMA foreign_keys = OFF")
        with db:
            columns = {r["name"] for r in db.execute("PRAGMA table_info(podcasts)")}
            if "outro_bed" not in columns:
                db.execute("ALTER TABLE podcasts ADD COLUMN outro_bed INTEGER NOT NULL DEFAULT 0")
            db.execute(SEGMENTS_TABLE.format(name="segments_v3"))
            for seg in db.execute("SELECT * FROM segments").fetchall():
                rows = db.execute("SELECT hash, t FROM segment_hashes WHERE segment_id = ? ORDER BY rowid",
                                  (seg["id"],)).fetchall()
                arr = np.array(rows, dtype=np.int64).reshape(-1, 2)
                db.execute(
                    "INSERT INTO segments_v3 (id, source_episode_id, podcast_id, source_start, source_end, "
                    "label, type, type_manual, hashes, times, created_at, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (seg["id"], seg["source_episode_id"], seg["podcast_id"], seg["source_start"],
                     seg["source_end"], seg["label"], seg["type"], seg["type_manual"],
                     _pack(arr[:, 0], "<u4"), _pack(arr[:, 1], "<i4"), seg["created_at"], seg["created_at"]),
                )
            db.execute("DROP TABLE segment_hashes")
            db.execute("DROP TABLE segments")
            db.execute("ALTER TABLE segments_v3 RENAME TO segments")
            db.execute("UPDATE settings SET value = ? WHERE key = 'schema'", (SCHEMA_VERSION,))
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("VACUUM")

    def _settle_config(self, wanted: FingerprintConfig | None) -> FingerprintConfig:
        rows = dict(self.db.execute("SELECT key, value FROM settings").fetchall())
        if not rows:
            config = wanted or FingerprintConfig()
            with transaction(self.db):
                self.db.executemany("INSERT INTO settings VALUES (?, ?)",
                                    [("schema", SCHEMA_VERSION), ("fingerprint", config.to_json())])
            return config
        if rows.get("schema") != SCHEMA_VERSION:
            raise StoreError("this store was made by another pod-block version; start a new --data-dir")
        stored = FingerprintConfig.from_json(rows["fingerprint"])
        if wanted is not None and wanted != stored:
            raise StoreError(f"this store uses '{stored.name}' fingerprints, not '{wanted.name}'; "
                             "fingerprints from different settings never match, so use a new --data-dir")
        return stored

    def close(self) -> None:
        if self._owned:
            self.db.close()

    # --- podcasts -----------------------------------------------------------

    def podcast_id(self, name: str | None = None, feed_url: str | None = None) -> int:
        """Find or create a podcast by feed URL, or by name when there is no feed."""
        with transaction(self.db):
            if feed_url:
                row = self.db.execute("SELECT id FROM podcasts WHERE feed_url = ?", (feed_url,)).fetchone()
            else:
                row = self.db.execute("SELECT id FROM podcasts WHERE name = ? AND feed_url IS NULL",
                                      (name,)).fetchone()
            if row:
                if name and feed_url:
                    self.db.execute("UPDATE podcasts SET name = ? WHERE id = ?", (name, row["id"]))
                return row["id"]
            cur = self.db.execute("INSERT INTO podcasts (name, feed_url) VALUES (?, ?)",
                                  (name or feed_url, feed_url))
            return cur.lastrowid

    def find_podcast(self, key: str) -> int | None:
        """A podcast id from its id, feed URL or name."""
        row = self.db.execute(
            "SELECT id FROM podcasts WHERE CAST(id AS TEXT) = ? OR feed_url = ? OR name = ?",
            (key, key, key),
        ).fetchone()
        return row["id"] if row else None

    def podcast(self, podcast_id: int | None) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()

    def set_outro_bed(self, podcast_id: int, on: bool) -> None:
        with transaction(self.db):
            self.db.execute("UPDATE podcasts SET outro_bed = ? WHERE id = ?", (int(on), podcast_id))

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
        with transaction(self.db):
            row = self.db.execute("SELECT id FROM episodes WHERE guid = ?", (guid,)).fetchone()
            if row is None:
                cols = ["guid", "added_at", *fields]
                vals = [guid, time.time(), *fields.values()]
                cur = self.db.execute(
                    f"INSERT INTO episodes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    vals,
                )
                return cur.lastrowid
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                self.db.execute(f"UPDATE episodes SET {sets} WHERE id = ?", [*fields.values(), row["id"]])
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
        with transaction(self.db):
            self.db.execute("INSERT OR IGNORE INTO compared_pairs VALUES (?, ?)", (a_id, b_id))

    # --- segments -----------------------------------------------------------

    def add_segment(self, fp: Fingerprint, source_episode_id: int | None,
                    source_start: float, source_end: float, podcast_id: int | None = None) -> int:
        """Store a common segment. `fp` must already be timed relative to the segment.

        Its podcast is the source episode's, or `podcast_id` when there is none.
        """
        if source_episode_id is not None:
            podcast_id = self.episode(source_episode_id)["podcast_id"]
        now = time.time()
        self._index = None  # before writing: a transaction rolled back leaves the blocks it had, read again
        with transaction(self.db):
            cur = self.db.execute(
                "INSERT INTO segments (source_episode_id, podcast_id, source_start, source_end, hashes, times, "
                "created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (source_episode_id, podcast_id, source_start, source_end,
                 _pack(fp.hashes, "<u4"), _pack(fp.times, "<i4"), now, now),
            )
        return cur.lastrowid

    def segment_fingerprint(self, segment_id: int) -> Fingerprint:
        """A block's own hashes, timed from its start."""
        row = self.segment(segment_id)
        return Fingerprint(np.frombuffer(row["hashes"], "<u4").astype(np.uint32),
                           np.frombuffer(row["times"], "<i4").astype(np.int32),
                           row["source_end"] - row["source_start"], self.config)

    def delete_segment(self, segment_id: int) -> None:
        self._index = None
        with transaction(self.db):
            self.db.execute("DELETE FROM segments WHERE id = ?", (segment_id,))

    def set_label(self, segment_id: int, label: str | None) -> None:
        with transaction(self.db):
            self.db.execute("UPDATE segments SET label = ? WHERE id = ?", (label, segment_id))

    def set_type(self, segment_id: int, block_type: str | None) -> None:
        """Set a block's type by hand, or with None hand it back to automatic tagging."""
        if block_type is not None and block_type not in TYPES:
            raise ValueError(f"unknown type {block_type!r}; pick one of {', '.join(TYPES)}")
        with transaction(self.db):
            if block_type is None:
                self.db.execute("UPDATE segments SET type = ?, type_manual = 0, confirmed = 0 WHERE id = ?",
                                (REPEATED, segment_id))
            else:
                self.db.execute("UPDATE segments SET type = ?, type_manual = 1, confirmed = 0 WHERE id = ?",
                                (block_type, segment_id))
                if block_type == IGNORED:  # forget where it played, so breaks and tags drop it
                    self.db.execute("DELETE FROM occurrences WHERE segment_id = ?", (segment_id,))

    def set_auto_types(self, types: dict[int, str], confirm: set[int] = frozenset()) -> None:
        """Retag every block not typed by hand or confirmed: the given type, else `repeated`.

        Blocks in `confirm` keep the type they get here from then on.
        """
        free = "type_manual = 0 AND confirmed = 0"
        with transaction(self.db):
            self.db.execute(f"UPDATE segments SET type = ? WHERE {free}", (REPEATED,))
            self.db.executemany(f"UPDATE segments SET type = ? WHERE id = ? AND {free}",
                                [(t, seg_id) for seg_id, t in types.items()])
            self.db.executemany("UPDATE segments SET confirmed = 1 WHERE id = ? AND type_manual = 0",
                                [(seg_id,) for seg_id in confirm])

    def hand_typed(self) -> set[int]:
        """Blocks whose type was set by hand."""
        return {r[0] for r in self.db.execute("SELECT id FROM segments WHERE type_manual = 1")}

    def types(self) -> dict[int, str]:
        return dict(self.db.execute("SELECT id, type FROM segments").fetchall())

    def segment(self, segment_id: int) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()

    def segments(self) -> list[sqlite3.Row]:
        """Every block, without its hashes."""
        return self.db.execute(
            "SELECT id, source_episode_id, podcast_id, source_start, source_end, label, type, type_manual, "
            "confirmed, created_at, last_seen FROM segments ORDER BY id"
        ).fetchall()

    def segment_summary(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT s.id, s.label, s.type, s.type_manual, s.confirmed, s.podcast_id, s.source_start, s.source_end,
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

    def episodes_with_occurrences(self) -> list[int]:
        """Episodes with known block matches, including ones whose fingerprints are gone."""
        return [r[0] for r in self.db.execute("SELECT DISTINCT episode_id FROM occurrences ORDER BY episode_id")]

    def occurrences_for_episode(self, episode_id: int) -> list[SegmentMatch]:
        rows = self.db.execute(
            "SELECT segment_id, start, end, segment_start, hits FROM occurrences "
            "WHERE episode_id = ? ORDER BY start",
            (episode_id,),
        ).fetchall()
        return [SegmentMatch(*r) for r in rows]

    def add_occurrences(self, rows: list[tuple[int, SegmentMatch]]) -> None:
        """Add (episode id, match) pairs."""
        with transaction(self.db):
            self.db.executemany(
                "INSERT INTO occurrences (segment_id, episode_id, start, end, segment_start, hits) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(m.segment_id, ep, m.start, m.end, m.segment_start, m.hits) for ep, m in rows],
            )

    def replace_occurrences(self, episode_id: int, matches: list[SegmentMatch]) -> None:
        with transaction(self.db):
            self.db.execute("DELETE FROM occurrences WHERE episode_id = ?", (episode_id,))
            self.db.executemany(
                "INSERT INTO occurrences (segment_id, episode_id, start, end, segment_start, hits) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(m.segment_id, episode_id, m.start, m.end, m.segment_start, m.hits) for m in matches],
            )

    # --- matching -----------------------------------------------------------

    def _load_index(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Every block's hashes, block ids and times, kept in memory until blocks change."""
        if self._index is None:
            rows = self.db.execute("SELECT id, hashes, times FROM segments").fetchall()
            hashes = [np.frombuffer(r["hashes"], "<u4") for r in rows]
            times = [np.frombuffer(r["times"], "<i4") for r in rows]
            ids = np.repeat(np.array([r["id"] for r in rows], dtype=np.int64), [len(h) for h in hashes])
            self._index = (np.concatenate([np.zeros(0, "<u4"), *hashes]).astype(np.uint32), ids,
                           np.concatenate([np.zeros(0, "<i4"), *times]).astype(np.int64))
        return self._index

    def visible_segments(self, podcast_id: int | None) -> np.ndarray:
        """Ids of the blocks that apply to an episode of `podcast_id`; never ignored ones."""
        scoped = ", ".join("?" * len(PODCAST_ONLY))
        rows = self.db.execute(
            f"SELECT id FROM segments WHERE type != ? AND (type NOT IN ({scoped}) OR podcast_id IS ?)",
            (IGNORED, *PODCAST_ONLY, podcast_id),
        ).fetchall()
        return np.array([r[0] for r in rows], dtype=np.int64)

    def scan(self, fp: Fingerprint, params: MatchParams, podcast_id: int | None = None,
             everything: bool = False, heard: bool = False) -> list[SegmentMatch]:
        """Find the stored blocks that appear in `fp`.

        Only blocks that apply to `podcast_id`, and not ignored ones, are used,
        unless `everything`.
        With `heard`, the blocks found count as heard now, which keeps them
        from being pruned; pass it for audio being processed, not for
        rescans of episodes already stored.
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
        if heard:
            self.mark_heard({m.segment_id for m in found})
        return sorted(found, key=lambda m: m.start)

    def mark_heard(self, segment_ids) -> None:
        ids = sorted(segment_ids)
        if ids:
            with transaction(self.db):
                self.db.execute(f"UPDATE segments SET last_seen = ? WHERE id IN ({', '.join('?' * len(ids))})",
                                (time.time(), *ids))

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

    # --- reports and clean-up ----------------------------------------------

    def save_report(self, episode_id: int, report: str) -> None:
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO reports VALUES (?, ?, ?)", (episode_id, time.time(), report))

    def saved_report(self, episode_id: int) -> str | None:
        row = self.db.execute("SELECT report FROM reports WHERE episode_id = ?", (episode_id,)).fetchone()
        return row["report"] if row else None

    def delete_episode(self, episode_id: int) -> None:
        """Forget an episode: its occurrences, report and comparisons go too."""
        with transaction(self.db):
            self.db.execute("DELETE FROM compared_pairs WHERE a_id = ? OR b_id = ?", (episode_id, episode_id))
            self.db.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))


def _pack(values: np.ndarray, dtype: str) -> bytes:
    return np.ascontiguousarray(values, dtype=dtype).tobytes()
