"""End-to-end steps: pull a feed, fingerprint episodes, find and store common blocks."""

from __future__ import annotations

import hashlib
import itertools
import logging
from dataclasses import dataclass
from pathlib import Path

from .feed import download, fetch_feed
from .fingerprint import Fingerprint, fingerprint_file, frames_to_seconds
from .matcher import MatchParams, common_runs
from .store import SegmentMatch, Store

log = logging.getLogger(__name__)


@dataclass
class Paths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "podblock.sqlite"

    def audio(self, guid: str, url: str) -> Path:
        suffix = Path(url.split("?", 1)[0]).suffix or ".mp3"
        return self.root / "audio" / f"{_key(guid)}{suffix}"

    def fingerprint(self, guid: str) -> Path:
        return self.root / "fingerprints" / f"{_key(guid)}.npz"


def _key(guid: str) -> str:
    return hashlib.sha1(guid.encode()).hexdigest()[:16]


def ingest_feed(store: Store, paths: Paths, feed_url: str, count: int,
                keep_audio: bool = True) -> list[int]:
    """Download and fingerprint the newest `count` episodes. Returns episode ids."""
    episodes = fetch_feed(feed_url)[:count]
    ids = []
    for ep in episodes:
        fp_path = paths.fingerprint(ep.guid)
        audio_path = paths.audio(ep.guid, ep.audio_url)
        ep_id = store.upsert_episode(ep.guid, feed_url=feed_url, title=ep.title,
                                     pub_date=ep.pub_date, audio_url=ep.audio_url)
        if not fp_path.exists():
            log.info("downloading %s", ep.title)
            download(ep.audio_url, audio_path)
            log.info("fingerprinting %s", ep.title)
            fp = fingerprint_file(audio_path)
            fp.save(fp_path)
            log.info("  %.0f min, %d hashes", fp.duration / 60, len(fp.hashes))
            store.upsert_episode(ep.guid, fingerprint_path=str(fp_path), duration=fp.duration)
        elif not store.episode(ep_id)["fingerprint_path"]:
            fp = Fingerprint.load(fp_path)
            store.upsert_episode(ep.guid, fingerprint_path=str(fp_path), duration=fp.duration)
        if keep_audio and audio_path.exists():
            store.upsert_episode(ep.guid, audio_path=str(audio_path))
        elif not keep_audio:
            audio_path.unlink(missing_ok=True)
            store.upsert_episode(ep.guid, audio_path=None)
        ids.append(ep_id)
    return ids


def ingest_file(store: Store, paths: Paths, audio_path: Path) -> int:
    """Fingerprint a local file and register it as an episode."""
    audio_path = audio_path.resolve()
    guid = f"file:{audio_path}"
    fp_path = paths.fingerprint(guid)
    fp = fingerprint_file(audio_path)
    fp.save(fp_path)
    return store.upsert_episode(guid, title=audio_path.name, audio_path=str(audio_path),
                                fingerprint_path=str(fp_path), duration=fp.duration)


def find_common_blocks(store: Store, episode_ids: list[int], params: MatchParams,
                       compare_with_history: bool = True) -> int:
    """Compare episodes pairwise and store new common blocks. Returns how many were added.

    With `compare_with_history`, the given episodes are also compared with every
    other episode already fingerprinted in the store.
    """
    fps: dict[int, Fingerprint] = {}

    def load(ep_id: int) -> Fingerprint:
        if ep_id not in fps:
            fps[ep_id] = Fingerprint.load(Path(store.episode(ep_id)["fingerprint_path"]))
        return fps[ep_id]

    pool = list(episode_ids)
    if compare_with_history:
        pool += [r["id"] for r in store.fingerprinted_episodes() if r["id"] not in episode_ids]
    targets = set(episode_ids)

    candidates: list[tuple[int, float, float, int]] = []  # (episode, start, end, hits)
    for a_id, b_id in itertools.combinations(pool, 2):
        if not ({a_id, b_id} & targets) or store.was_compared(a_id, b_id):
            continue
        fa, fb = load(a_id), load(b_id)
        runs = common_runs(fa.hashes, fa.times, fb.hashes, fb.times, params)
        log.info("episodes %d vs %d: %d shared blocks", a_id, b_id, len(runs))
        for r in runs:
            candidates.append((a_id, frames_to_seconds(r.a_start), frames_to_seconds(r.a_end), r.hits))
        store.mark_compared(a_id, b_id)

    # Longest first, so a long shared block is stored whole and the shorter
    # pieces of it found in other pairs are recognised as already known.
    added = 0
    for ep_id, start, end, _ in sorted(candidates, key=lambda c: c[1] - c[2]):
        piece = load(ep_id).slice(start, end)
        for gap_start, gap_end in store.uncovered(piece, params):
            if gap_end - gap_start < params.min_length_sec:
                continue
            store.add_segment(piece.slice(gap_start, gap_end), ep_id,
                              start + gap_start, start + gap_end)
            added += 1

    # New segments may also appear in older episodes, so rescan all of them.
    rescan = pool if added else episode_ids
    for ep_id in rescan:
        store.replace_occurrences(ep_id, store.scan(load(ep_id), params))
    if split_segments(store, params, load):
        for ep_id in [r["id"] for r in store.fingerprinted_episodes()]:
            store.replace_occurrences(ep_id, store.scan(load(ep_id), params))
    return added


def split_segments(store: Store, params: MatchParams, load) -> int:
    """Cut blocks at points where their occurrences start or stop.

    A block first found as one long run (say a 5 minute ad break) often turns up
    elsewhere only in parts (one ad from that break). Those part boundaries mark
    where the separate pieces of audio begin and end. Returns how many blocks
    were split.
    """
    split = 0
    for seg in store.segments():
        if seg["source_episode_id"] is None:
            continue
        length = seg["source_end"] - seg["source_start"]
        edges = []
        for o in store.occurrences_for_segment(seg["id"]):
            edges += [o["segment_start"], o["segment_start"] + o["end"] - o["start"]]
        cuts = _cut_points(edges, length, params.min_length_sec)
        if not cuts:
            continue
        source = load(seg["source_episode_id"])
        bounds = [0.0, *cuts, length]
        for lo, hi in zip(bounds, bounds[1:]):
            start, end = seg["source_start"] + lo, seg["source_start"] + hi
            new_id = store.add_segment(source.slice(start, end), seg["source_episode_id"], start, end)
            if seg["label"]:
                store.set_label(new_id, seg["label"])
        store.delete_segment(seg["id"])
        log.info("split block %d into %d", seg["id"], len(bounds) - 1)
        split += 1
    return split


def _cut_points(edges: list[float], length: float, min_length: float,
                tolerance: float = 3.0) -> list[float]:
    """Merge nearby edges and keep those that leave pieces of at least `min_length`."""
    inner = sorted(e for e in edges if min_length <= e <= length - min_length)
    groups: list[list[float]] = []
    for e in inner:
        if groups and e - groups[-1][-1] <= tolerance:
            groups[-1].append(e)
        else:
            groups.append([e])
    cuts: list[float] = []
    for g in groups:
        c = sum(g) / len(g)
        if c - (cuts[-1] if cuts else 0.0) >= min_length and length - c >= min_length:
            cuts.append(c)
    return cuts


def scan_audio(store: Store, audio_path: Path, params: MatchParams) -> tuple[Fingerprint, list[SegmentMatch]]:
    fp = fingerprint_file(audio_path)
    return fp, store.scan(fp, params)
