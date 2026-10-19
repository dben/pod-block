"""The `process` job: download an episode, find its repeats, and cut them out.

Repeats are found by comparing episodes, so a show needs a few fingerprinted
episodes first. When it has fewer than COMPARE_WITH, the job also takes the
episodes published just after the chosen one (then just before it, if the
feed runs out). Shows often come in parts, so those are likely next anyway;
they are fingerprinted, compared as one group with the chosen episode, and
cut and made ready too.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .. import cut
from ..feed import download
from ..fingerprint import fingerprint_file
from ..matcher import MatchParams
from ..pipeline import Paths, episode_report, find_common_blocks
from ..store import Store
from .config import Settings
from .jobs import Cancelled, Context, settle_episode

log = logging.getLogger(__name__)

COMPARE_WITH = 4  # fingerprinted episodes a show should have before its repeats are looked for
FILL_GAPS = 4.0  # seconds of new audio between two repeats that is cut with them, so no scraps are left
AUDIO_SUFFIXES = {".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".oga", ".wav", ".flac"}
MIME_SUFFIXES = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
                 "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/opus": ".opus"}

_detecting = threading.Lock()  # the store's blocks change while repeats are found; one group at a time


class ProcessError(Exception):
    pass


def media_key(guid: str) -> str:
    return hashlib.sha1(guid.encode()).hexdigest()[:16]


def audio_suffix(url: str | None, mime: str | None) -> str:
    suffix = Path((url or "").split("?", 1)[0]).suffix.lower()
    if suffix in AUDIO_SUFFIXES:
        return suffix
    return MIME_SUFFIXES.get((mime or "").split(";")[0].strip().lower(), ".mp3")


def _episode(db: sqlite3.Connection, episode_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        raise ProcessError("the episode is gone")
    return row


def companions(db: sqlite3.Connection, ep: sqlite3.Row, count: int) -> list[sqlite3.Row]:
    """Up to `count` unfingerprinted episodes of the same show: the ones after `ep` first, nearest first,
    then the ones before it."""
    if count <= 0 or ep["pub_ts"] is None:
        return []
    usable = """podcast_id = ? AND id != ? AND fingerprint_path IS NULL AND audio_url IS NOT NULL
                AND in_feed = 1 AND COALESCE(state, '') NOT IN ('queued', 'running')"""
    after = db.execute(f"SELECT * FROM episodes WHERE {usable} AND pub_ts > ? ORDER BY pub_ts LIMIT ?",
                       (ep["podcast_id"], ep["id"], ep["pub_ts"], count)).fetchall()
    before = db.execute(f"SELECT * FROM episodes WHERE {usable} AND pub_ts <= ? ORDER BY pub_ts DESC LIMIT ?",
                        (ep["podcast_id"], ep["id"], ep["pub_ts"], count - len(after))).fetchall()
    return [*after, *before]


def _fetch(ctx: Context, settings: Settings, store: Store, paths: Paths, episode_id: int) -> None:
    """Make sure the episode's audio is downloaded and fingerprinted."""
    ep = _episode(ctx.db, episode_id)
    original = Path(ep["original_path"]) if ep["original_path"] else None
    if original is None or not original.exists():
        if not ep["audio_url"]:
            raise ProcessError("the feed gives no audio for it")
        original = settings.media_dir / "original" / (media_key(ep["guid"]) + audio_suffix(ep["audio_url"], ep["audio_type"]))
        ctx.step("Downloading", episode_id)
        try:
            download(ep["audio_url"], original, progress=lambda done, total: ctx.progress(done / total if total else None))
        except (Cancelled, ProcessError):
            raise
        except Exception as e:
            raise ProcessError(f"couldn't download it ({type(e).__name__}: {e})") from None
        ctx.db.execute("UPDATE episodes SET original_path = ?, ever_downloaded = 1 WHERE id = ?",
                       (str(original), episode_id))
        ctx.db.commit()
    fp_path = Path(ep["fingerprint_path"]) if ep["fingerprint_path"] else None
    if fp_path is None or not fp_path.exists():
        ctx.step("Fingerprinting", episode_id)
        try:
            fp = fingerprint_file(original, store.config)
        except Exception as e:
            raise ProcessError(f"couldn't read the audio ({e})") from None
        fp_path = paths.fingerprint(ep["guid"], store.config)
        fp.save(fp_path)
        store.upsert_episode(ep["guid"], fingerprint_path=str(fp_path), duration=fp.duration)


def _render(ctx: Context, settings: Settings, episode_id: int, report: dict) -> None:
    ep = _episode(ctx.db, episode_id)
    keep = cut.keep_spans([(s["start"], s["end"]) for s in report["sections"]], ep["duration"])
    if not keep:
        raise ProcessError("the whole episode was found to repeat, so nothing would be left")
    ctx.step("Cutting", episode_id)
    try:
        out = cut.render(Path(ep["original_path"]), settings.media_dir / "cut" / media_key(ep["guid"]), keep,
                         progress=ctx.progress)
    except cut.CutError as e:
        raise ProcessError(str(e)) from None
    if ep["cut_path"] and Path(ep["cut_path"]) != out.path:
        Path(ep["cut_path"]).unlink(missing_ok=True)
    ctx.db.execute(
        """UPDATE episodes SET state = 'ready', state_note = NULL, cut_path = ?, cut_type = ?, cut_duration = ?,
                  cut_bytes = ?, processed_at = ?, ever_downloaded = 1 WHERE id = ?""",
        (str(out.path), out.mime, out.duration, out.size, time.time(), episode_id))
    ctx.db.commit()


def run(ctx: Context, settings: Settings) -> None:
    episode_id = ctx.job["episode_id"]
    params = MatchParams()
    store = Store(settings.db_path)
    paths = Paths(settings.data_dir)
    group = [episode_id]
    try:
        _fetch(ctx, settings, store, paths, episode_id)
        ep = _episode(ctx.db, episode_id)
        have = ctx.db.execute("SELECT COUNT(*) FROM episodes WHERE podcast_id = ? AND fingerprint_path IS NOT NULL",
                              (ep["podcast_id"],)).fetchone()[0]
        extra = companions(ctx.db, ep, COMPARE_WITH - have)
        for c in extra:
            ctx.db.execute("UPDATE episodes SET state = 'queued', state_note = ? WHERE id = ?",
                           (f"Coming along with “{ep['title']}”", c["id"]))
        ctx.db.commit()
        group += [c["id"] for c in extra]
        for c in extra:
            _fetch(ctx, settings, store, paths, c["id"])

        ctx.step("Finding repeats", episode_id)
        for other in group[1:]:
            ctx.db.execute("UPDATE episodes SET state = 'running', state_note = 'Finding repeats' WHERE id = ?",
                           (other,))
        ctx.db.commit()
        with _detecting:
            find_common_blocks(store, group, params)
            reports = {ep_id: episode_report(store, ep_id, params, fill_gaps=FILL_GAPS) for ep_id in group}
        for ep_id in group:
            _render(ctx, settings, ep_id, reports[ep_id])
    except BaseException as e:
        failed = None if isinstance(e, Cancelled) else str(e)
        for other in group[1:]:  # companions were never asked for; they go back to rest quietly
            settle_episode(ctx.db, other)
        settle_episode(ctx.db, episode_id, failed)
        raise
    finally:
        store.close()
