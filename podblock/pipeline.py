"""End-to-end steps: pull a feed, fingerprint episodes, find and store common blocks."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .breaks import SHORT_BLOCK_SEC, classify_bumpers
from .feed import Episode, Feed, download, fetch_feed
from .fingerprint import (Fingerprint, FingerprintConfig, fingerprint_file, frames_to_seconds,
                          loudness_file)
from .matcher import MatchParams, common_runs
from .outro import music_bed_outro
from .report import MIN_SECTION_SEC, build_sections
from .store import BUMPER, BUMPER_IN, BUMPER_OUT, SegmentMatch, Store

log = logging.getLogger(__name__)

SHORT_MIN_OCCURRENCES = 3  # blocks under SHORT_BLOCK_SEC must turn up this often
COMPARE_SAME = 4  # same-show episodes, closest in publish date, to compare each episode with
COMPARE_OTHER = 1  # episodes from each other show to compare with, for network ads...
OTHER_WINDOW_DAYS = 14.0  # ...if published within this many days
BUMPER_TYPES = {BUMPER_IN, BUMPER_OUT, BUMPER}


@dataclass
class Paths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "podblock.sqlite"

    def audio(self, guid: str, url: str) -> Path:
        suffix = Path(url.split("?", 1)[0]).suffix or ".mp3"
        return self.root / "audio" / f"{_key(guid)}{suffix}"

    def fingerprint(self, guid: str, config: FingerprintConfig) -> Path:
        return self.root / "fingerprints" / f"{_key(guid)}.{config.name}.npz"


def _key(guid: str) -> str:
    return hashlib.sha1(guid.encode()).hexdigest()[:16]


def ingest_episodes(store: Store, paths: Paths, feed: Feed, episodes: list[Episode],
                    keep_audio: bool = True) -> list[int]:
    """Download and fingerprint feed episodes, skipping ones already done. Returns ids."""
    podcast = store.podcast_id(feed.title, feed.url)
    ids = []
    for ep in episodes:
        fp_path = paths.fingerprint(ep.guid, store.config)
        audio_path = paths.audio(ep.guid, ep.audio_url)
        ep_id = store.upsert_episode(ep.guid, podcast_id=podcast, title=ep.title,
                                     pub_date=ep.pub_date, audio_url=ep.audio_url)
        if not fp_path.exists():
            log.info("downloading %s", ep.title)
            download(ep.audio_url, audio_path)
            log.info("fingerprinting %s", ep.title)
            fp = fingerprint_file(audio_path, store.config)
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


def ingest_feed(store: Store, paths: Paths, feed_url: str, count: int,
                keep_audio: bool = True) -> list[int]:
    """Download and fingerprint the newest `count` episodes. Returns episode ids."""
    feed = fetch_feed(feed_url)
    return ingest_episodes(store, paths, feed, feed.episodes[:count], keep_audio)


def neighbours(episodes: list, index: int, count: int) -> list:
    """Up to `count` episodes nearest to `episodes[index]`, alternating newer and older."""
    picked = []
    for step in range(1, len(episodes)):
        for i in (index - step, index + step):
            if 0 <= i < len(episodes) and len(picked) < count:
                picked.append(episodes[i])
    return picked


def refingerprint(store: Store, paths: Paths, episode_id: int) -> None:
    """Fingerprint a stored feed episode again after its fingerprint was pruned."""
    ep = store.episode(episode_id)
    if not ep["audio_url"]:
        raise RuntimeError(f"episode {episode_id} has no audio URL to fetch again")
    audio_path = paths.audio(ep["guid"], ep["audio_url"])
    download(ep["audio_url"], audio_path)
    fp = fingerprint_file(audio_path, store.config)
    fp_path = paths.fingerprint(ep["guid"], store.config)
    fp.save(fp_path)
    store.upsert_episode(ep["guid"], fingerprint_path=str(fp_path), audio_path=str(audio_path),
                         duration=fp.duration)


def ingest_file(store: Store, paths: Paths, audio_path: Path, podcast_id: int | None = None) -> int:
    """Fingerprint a local file and register it as an episode."""
    audio_path = audio_path.resolve()
    guid = f"file:{audio_path}"
    fp_path = paths.fingerprint(guid, store.config)
    fp = fingerprint_file(audio_path, store.config)
    fp.save(fp_path)
    fields = {"podcast_id": podcast_id} if podcast_id is not None else {}
    return store.upsert_episode(guid, title=audio_path.name, audio_path=str(audio_path),
                                fingerprint_path=str(fp_path), duration=fp.duration, **fields)


def load_fingerprint(store: Store, episode_id: int) -> Fingerprint:
    return Fingerprint.load(Path(store.episode(episode_id)["fingerprint_path"]))


def episode_loudness(store: Store, episode_id: int) -> np.ndarray | None:
    """The episode's loudness curve: from its fingerprint, else from its audio."""
    fp = load_fingerprint(store, episode_id)
    if fp.loudness is not None:
        return fp.loudness
    audio = store.episode(episode_id)["audio_path"]
    if audio and Path(audio).exists():
        return loudness_file(audio, store.config.sample_rate)
    return None


def episode_report(store: Store, episode_id: int, params: MatchParams, fill_gaps: float = 0.0,
                   outro_bed: bool | None = None, min_section: float = MIN_SECTION_SEC) -> dict:
    """List a stored episode's repeated sections and save that as its report.

    `outro_bed` looks for a music-bed outro (True) or not (False); None
    leaves it to the podcast's setting.
    """
    ep = store.episode(episode_id)
    matches = store.scan(load_fingerprint(store, episode_id), params, podcast_id=ep["podcast_id"], heard=True)
    pod = store.podcast(ep["podcast_id"])
    wanted = outro_bed if outro_bed is not None else bool(pod and pod["outro_bed"])
    loudness = episode_loudness(store, episode_id) if wanted else None
    bed = music_bed_outro(loudness) if loudness is not None else None
    sections = build_sections(matches, store.types(), ep["duration"], fill_gaps=fill_gaps, outro_bed_start=bed,
                              min_section=min_section, pinned=store.hand_typed())
    data = {"episode_id": episode_id, "title": ep["title"], "duration": round(ep["duration"], 2),
            "repeated_sec": round(sum(s.length for s in sections), 2),
            "sections": [s.to_dict() for s in sections]}
    store.save_report(episode_id, json.dumps(data))
    return data


def rescan(store: Store, episode_id: int, params: MatchParams, fp: Fingerprint | None = None,
           heard: bool = False) -> None:
    """Refresh where stored blocks occur in one episode."""
    fp = fp or load_fingerprint(store, episode_id)
    store.replace_occurrences(
        episode_id, store.scan(fp, params, podcast_id=store.episode(episode_id)["podcast_id"], heard=heard))


def _when(row) -> float:
    """An episode's publish time as a timestamp, else when it was added."""
    if row["pub_date"]:
        try:
            return datetime.fromisoformat(row["pub_date"]).timestamp()
        except ValueError:
            pass
    return row["added_at"] or 0.0


def comparison_set(store: Store, episode_id: int, same: int = COMPARE_SAME, other: int = COMPARE_OTHER,
                   among: set[int] | None = None) -> list[int]:
    """The stored episodes to compare one episode with.

    From its own podcast, the `same` episodes closest to it in publish date:
    listeners hop around a back catalogue, and ads that ran at the time an
    episode was published sit in its neighbours, not in this week's episodes.
    From every other podcast, the `other` closest within OTHER_WINDOW_DAYS, to
    catch network ads. `among` limits the choice to those episodes.
    """
    ep = store.episode(episode_id)
    when = _when(ep)
    by_podcast: dict[int | None, list] = {}
    for r in store.fingerprinted_episodes():
        if r["id"] != episode_id and (among is None or r["id"] in among):
            by_podcast.setdefault(r["podcast_id"], []).append(r)
    picked = []
    for podcast, eps in by_podcast.items():
        eps.sort(key=lambda r: abs(_when(r) - when))
        if podcast is not None and podcast == ep["podcast_id"]:
            picked += eps[:same]
        else:
            picked += [r for r in eps[:other] if abs(_when(r) - when) <= OTHER_WINDOW_DAYS * 86400]
    return [r["id"] for r in picked]


def find_common_blocks(store: Store, episode_ids: list[int], params: MatchParams,
                       same: int = COMPARE_SAME, other: int = COMPARE_OTHER,
                       only_among_given: bool = False) -> int:
    """Compare each episode with its comparison set and store new common blocks.

    Returns how many blocks were added. With `only_among_given`, the given
    episodes are only compared with each other.
    """
    fps: dict[int, Fingerprint] = {}

    def load(ep_id: int) -> Fingerprint:
        if ep_id not in fps:
            fps[ep_id] = load_fingerprint(store, ep_id)
        return fps[ep_id]

    targets = set(episode_ids)
    pairs = set()
    for ep_id in episode_ids:
        for other_id in comparison_set(store, ep_id, same, other, targets if only_among_given else None):
            pairs.add(tuple(sorted((ep_id, other_id))))
    involved = targets | {e for pair in pairs for e in pair}

    candidates: list[tuple[int, float, float, int]] = []  # (episode, start, end, hits)
    for a_id, b_id in sorted(pairs):
        if store.was_compared(a_id, b_id):
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

    # New blocks may also play in the episodes compared against.
    for ep_id in sorted(involved if added else targets):
        rescan(store, ep_id, params, load(ep_id), heard=ep_id in targets)
    split_segments(store, params.min_length_sec)
    drop_nested_blocks(store)
    prune_short_blocks(store)
    retag_bumpers(store)
    return added


def retag_bumpers(store: Store) -> None:
    tags = classify_bumpers(store)
    store.set_auto_types(tags.kinds, tags.confirmed)


def drop_nested_blocks(store: Store, share: float = 0.8) -> int:
    """Drop blocks that nearly always play inside a longer block.

    Short runs found in one pair of episodes can be slivers of a longer block
    found in another pair; they add nothing but clutter. Blocks are judged
    shortest first, so dropping one never changes the verdict on another.
    """
    summary = store.segment_summary()
    lengths = {r["id"]: r["duration"] for r in summary}
    by_hand = {r["id"] for r in summary if r["type_manual"]}
    inside, total = Counter(), Counter()
    for ep_id in store.episodes_with_occurrences():
        matches = sorted(store.occurrences_for_episode(ep_id), key=lambda m: m.start)
        for i, m in enumerate(matches):
            total[m.segment_id] += 1
            need = 0.5 * (m.end - m.start)
            for o in matches:
                if o.start >= m.end:
                    break
                if (lengths.get(o.segment_id, 0) > lengths.get(m.segment_id, 0)
                        and min(m.end, o.end) - max(m.start, o.start) >= need):
                    inside[m.segment_id] += 1
                    break
    dropped = [seg_id for seg_id in lengths
               if seg_id not in by_hand and total[seg_id] and inside[seg_id] >= share * total[seg_id]]
    for seg_id in dropped:
        store.delete_segment(seg_id)
    return len(dropped)


def prune_short_blocks(store: Store, min_occurrences: int = SHORT_MIN_OCCURRENCES) -> int:
    """Drop short blocks seen too rarely to trust; a pair of 2 s clips can match by chance."""
    weak = [r["id"] for r in store.segment_summary()
            if r["duration"] < SHORT_BLOCK_SEC and r["occurrences"] < min_occurrences
            and not r["type_manual"]]
    for seg_id in weak:
        store.delete_segment(seg_id)
    return len(weak)


def split_segments(store: Store, min_length: float = MatchParams.min_length_sec) -> int:
    """Cut blocks at points where their occurrences start or stop.

    A block first found as one long run (say a 5 minute ad break) often turns up
    elsewhere only in parts (one ad from that break). Those part boundaries mark
    where the separate pieces of audio begin and end. The pieces are cut from
    the block's own hashes, and each known occurrence is carried over to the
    pieces it covers, so neither source episodes nor the episodes it played in
    need their fingerprints any more. Overlaps under `min_length` are dropped.
    Returns how many blocks were split.
    """
    split = 0
    for seg in store.segments():
        length = seg["source_end"] - seg["source_start"]
        occurrences = store.occurrences_for_segment(seg["id"])
        edges = []
        for start, end in _joined_spans(occurrences):
            edges += [start, end]
        cuts = _cut_points(edges, length, SHORT_BLOCK_SEC)
        if not cuts:
            continue
        whole = store.segment_fingerprint(seg["id"])
        bounds = [0.0, *cuts, length]
        carried = []
        for lo, hi in zip(bounds, bounds[1:]):
            new_id = store.add_segment(whole.slice(lo, hi), seg["source_episode_id"],
                                       seg["source_start"] + lo, seg["source_start"] + hi,
                                       podcast_id=seg["podcast_id"])
            if seg["label"]:
                store.set_label(new_id, seg["label"])
            if seg["type_manual"]:
                store.set_type(new_id, seg["type"])
            for o in occurrences:
                # The part of the block this occurrence played, in block time.
                o_lo, o_hi = o["segment_start"], o["segment_start"] + o["end"] - o["start"]
                a, b = max(lo, o_lo), min(hi, o_hi)
                if b - a >= min_length:
                    start = o["start"] + a - o_lo
                    share = (b - a) / max(o_hi - o_lo, 1e-9)
                    carried.append((o["episode_id"], SegmentMatch(new_id, start, start + b - a, a - lo,
                                                                  max(1, round(o["hits"] * share)))))
        store.delete_segment(seg["id"])
        store.add_occurrences(carried)
        log.info("split block %d into %d", seg["id"], len(bounds) - 1)
        split += 1
    return split


def _joined_spans(occurrences, gap: float = 15.0, tolerance: float = 0.2) -> list[tuple[float, float]]:
    """Spans of the block (in block time) that each occurrence covers.

    A quiet patch can break one occurrence into two matches at the same
    offset; those are joined so the patch does not look like an edge.
    """
    spans: list[tuple[int, float, float, float]] = []  # (episode, offset, start, end)
    for o in sorted(occurrences, key=lambda o: (o["episode_id"], o["start"])):
        offset = o["start"] - o["segment_start"]
        start, end = o["segment_start"], o["segment_start"] + o["end"] - o["start"]
        if spans:
            ep, off, s0, s1 = spans[-1]
            if ep == o["episode_id"] and abs(off - offset) <= tolerance and start - s1 <= gap:
                spans[-1] = (ep, off, s0, max(s1, end))
                continue
        spans.append((o["episode_id"], offset, start, end))
    return [(s0, s1) for _, _, s0, s1 in spans]


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




def prune(store: Store, audio_days: float = 0.0, fingerprint_days: float = 30.0,
          block_days: float = 30.0, bumper_days: float = 365.0, dry_run: bool = False) -> Counter:
    """Clear out what is no longer needed. Returns counts of what went (or would go).

    - Downloaded audio older than `audio_days`; only `export` needs it.
      Local files given to `analyze` are never touched.
    - Fingerprints older than `fingerprint_days`; those episodes can no longer
      be compared or rescanned, and are fetched again if asked for.
    - Blocks not heard in `block_days`, unless their type was set by hand.
      Bumpers last `bumper_days`: a show's bumpers rarely change, and they
      mark breaks even when every ad in them is new.
    - Block matches in episodes whose fingerprints are gone, once older than
      `block_days`. Episode rows and their saved reports stay; both are tiny.
    """
    now, day = time.time(), 86400.0
    gone = Counter()

    def older(path: str | None, days: float) -> bool:
        return bool(path) and Path(path).exists() and now - Path(path).stat().st_mtime > days * day

    for ep in store.db.execute("SELECT * FROM episodes").fetchall():
        if not ep["guid"].startswith("file:") and older(ep["audio_path"], audio_days):
            gone["audio files"] += 1
            if not dry_run:
                Path(ep["audio_path"]).unlink()
                store.upsert_episode(ep["guid"], audio_path=None)
        if older(ep["fingerprint_path"], fingerprint_days):
            gone["fingerprints"] += 1
            if not dry_run:
                Path(ep["fingerprint_path"]).unlink()
                store.upsert_episode(ep["guid"], fingerprint_path=None)

    def lifetime(row) -> float:
        return (bumper_days if row["type"] in BUMPER_TYPES else block_days) * day

    stale = [r["id"] for r in store.segments()
             if not r["type_manual"] and (r["last_seen"] or 0) < now - lifetime(r)]
    gone["blocks"] += len(stale)
    old_matches = store.db.execute(
        "SELECT COUNT(*) FROM occurrences WHERE episode_id IN "
        "(SELECT id FROM episodes WHERE fingerprint_path IS NULL AND added_at < ?)",
        (now - block_days * day,)).fetchone()[0]
    gone["old block matches"] += old_matches
    if not dry_run:
        for seg_id in stale:
            store.delete_segment(seg_id)
        store.db.execute("DELETE FROM occurrences WHERE episode_id IN "
                         "(SELECT id FROM episodes WHERE fingerprint_path IS NULL AND added_at < ?)",
                         (now - block_days * day,))
        store.db.commit()
        if stale:
            retag_bumpers(store)
    return gone
