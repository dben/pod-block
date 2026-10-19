import os
import sqlite3
import time

import numpy as np

from podblock.fingerprint import Fingerprint, FingerprintConfig
from podblock.pipeline import comparison_set, prune, split_segments
from podblock.store import INTRO, SegmentMatch, Store


def tones(seed, n=300, length=20.0):
    rng = np.random.default_rng(seed)
    return Fingerprint(rng.integers(0, 2**26, n).astype(np.uint32),
                       np.sort(rng.integers(0, int(length / 0.032), n)).astype(np.int32), length)


def test_comparison_set_takes_episodes_closest_in_publish_date(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    show, other = store.podcast_id("Show", "http://show"), store.podcast_id("Other", "http://other")
    ids = {}
    for day in (1, 5, 8, 9, 11, 30, 60):  # a back catalogue; we ask about day 10
        ids[day] = store.upsert_episode(f"s{day}", podcast_id=show, pub_date=f"2026-01-{day:02d}T00:00:00+00:00"
                                        if day <= 31 else "2026-03-01T00:00:00+00:00", fingerprint_path="x")
    target = store.upsert_episode("s10", podcast_id=show, pub_date="2026-01-10T00:00:00+00:00",
                                  fingerprint_path="x")
    near = store.upsert_episode("o12", podcast_id=other, pub_date="2026-01-12T00:00:00+00:00", fingerprint_path="x")
    store.upsert_episode("o13", podcast_id=other, pub_date="2026-01-13T00:00:00+00:00", fingerprint_path="x")
    store.upsert_episode("o-far", podcast_id=other, pub_date="2025-06-01T00:00:00+00:00", fingerprint_path="x")

    picked = comparison_set(store, target, same=3, other=1)

    assert set(picked) == {ids[9], ids[11], ids[8], near}


def test_split_cuts_from_the_blocks_own_hashes(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    whole = tones(1, n=600, length=40.0)
    block = store.add_segment(whole, None, 100.0, 140.0)
    a = store.upsert_episode("a")
    b = store.upsert_episode("b")
    store.replace_occurrences(a, [SegmentMatch(block, 0, 40, 0, 99)])
    store.replace_occurrences(b, [SegmentMatch(block, 500, 520, 0, 99)])  # only the first half

    touched = split_segments(store)

    assert touched == {a, b}
    pieces = [store.segment_fingerprint(r["id"]) for r in store.segments()]
    assert [round(p.duration) for p in pieces] == [20, 20]
    assert sum(len(p.hashes) for p in pieces) == len(whole.hashes)


def test_prune_clears_old_files_and_unheard_blocks(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    old = time.time() - 40 * 86400
    audio, fp, own = tmp_path / "a.mp3", tmp_path / "a.npz", tmp_path / "mine.mp3"
    for f in (audio, fp, own):
        f.write_bytes(b"x")
        os.utime(f, (old, old))
    store.upsert_episode("feed-ep", audio_path=str(audio), fingerprint_path=str(fp), added_at=old)
    store.upsert_episode(f"file:{own}", audio_path=str(own))
    stale, kept, typed = (store.add_segment(tones(i), None, 0, 20) for i in (2, 3, 4))
    store.set_type(typed, INTRO)
    store.db.execute("UPDATE segments SET last_seen = ? WHERE id IN (?, ?)", (old, stale, typed))
    store.db.commit()

    assert prune(store, dry_run=True)["blocks"] == 1 and audio.exists()
    gone = prune(store)

    assert (gone["audio files"], gone["fingerprints"], gone["blocks"]) == (1, 1, 1)
    assert not audio.exists() and not fp.exists() and own.exists()
    assert {r["id"] for r in store.segments()} == {kept, typed}


def test_reports_are_saved_per_episode(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    ep = store.upsert_episode("e")
    store.save_report(ep, '{"sections": []}')
    store.save_report(ep, '{"sections": [1]}')
    assert store.saved_report(ep) == '{"sections": [1]}'


def test_schema_2_stores_are_upgraded(tmp_path):
    path = tmp_path / "db.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE podcasts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, feed_url TEXT UNIQUE);
        CREATE TABLE episodes (id INTEGER PRIMARY KEY, guid TEXT UNIQUE NOT NULL, podcast_id INTEGER,
            title TEXT, pub_date TEXT, audio_url TEXT, audio_path TEXT, fingerprint_path TEXT,
            duration REAL, added_at REAL);
        CREATE TABLE segments (id INTEGER PRIMARY KEY, source_episode_id INTEGER, podcast_id INTEGER,
            source_start REAL NOT NULL, source_end REAL NOT NULL, label TEXT,
            type TEXT NOT NULL DEFAULT 'repeated', type_manual INTEGER NOT NULL DEFAULT 0, created_at REAL);
        CREATE TABLE segment_hashes (segment_id INTEGER NOT NULL, hash INTEGER NOT NULL, t INTEGER NOT NULL);
        CREATE TABLE occurrences (id INTEGER PRIMARY KEY, segment_id INTEGER NOT NULL, episode_id INTEGER NOT NULL,
            start REAL NOT NULL, end REAL NOT NULL, segment_start REAL NOT NULL, hits INTEGER NOT NULL);
    """)
    db.execute("INSERT INTO settings VALUES ('schema', '2'), ('fingerprint', ?)", (FingerprintConfig().to_json(),))
    db.execute("INSERT INTO segments (id, source_start, source_end, label, type, type_manual, created_at) "
               "VALUES (7, 0, 20, 'jingle', 'intro', 1, 1.0)")
    db.executemany("INSERT INTO segment_hashes VALUES (7, ?, ?)", [(4000000000, 3), (12, 1), (99, 2)])
    db.commit()
    db.close()

    store = Store(path)

    fp = store.segment_fingerprint(7)
    assert fp.hashes.tolist() == [4000000000, 12, 99] and fp.times.tolist() == [3, 1, 2]
    seg = store.segment(7)
    assert (seg["label"], seg["type"], seg["type_manual"], seg["last_seen"]) == ("jingle", "intro", 1, 1.0)
    assert "segment_hashes" not in {r[0] for r in store.db.execute("SELECT name FROM sqlite_master")}
