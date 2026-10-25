"""The `process` job: download an episode, find its repeats, and cut them out.

Repeats are found by comparing episodes, so a show needs a few fingerprinted
episodes first. When it has fewer than COMPARE_WITH, the job also takes the
episodes published just after the chosen one (then just before it, if the
feed runs out). Shows often come in parts, so those are likely next anyway;
they are fingerprinted, compared as one group with the chosen episode, and
cut and made ready too.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .. import cut
from ..breaks import MAX_BREAK_SEC
from ..feed import download
from ..fingerprint import fingerprint_file
from ..matcher import MatchParams
from ..pipeline import Paths, episode_report, find_common_blocks, load_fingerprint, rescan
from ..store import BUMPER, BUMPER_IN, BUMPER_OUT, IGNORED, INTRO, KEPT, OUTRO, PODCAST_TYPES, SHOW, Store
from ..timing import Timer
from . import captions, chapters, missing, phrases
from .config import Settings
from .db import transaction
from .jobs import Cancelled, Context, enqueue, settle_episode, take_along
from .locks import detecting
from .states import ACTIVE, sql

log = logging.getLogger(__name__)

COMPARE_WITH = 4  # fingerprinted episodes a show should have before its repeats are looked for
FILL_GAPS = 4.0  # default seconds of new audio between two repeats cut with them (podcasts.fill_gaps)

class ProcessError(Exception):
    pass


@contextmanager
def _detecting(ctx: Context) -> Iterator[None]:
    """Hold `detecting` for the block, timing the wait for another job to let go of it."""
    with ctx.timer("repeats.wait"):
        detecting.acquire()
    try:
        yield
    finally:
        detecting.release()


class Busy(ProcessError):
    """The episode is queued or being processed, so it can't be changed until that is done."""


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
    usable = f"""podcast_id = ? AND id != ? AND fingerprint_path IS NULL AND audio_url IS NOT NULL
                AND in_feed = 1 AND skip_processing = 0 AND COALESCE(state, '') NOT IN ({sql(ACTIVE)})
                AND held_by IS NULL"""
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
        original = settings.media.original(ep)
        ctx.step("Downloading", episode_id)
        try:
            with ctx.timer("download", episode_id) as step:
                download(ep["audio_url"], original,
                         progress=lambda done, total: ctx.progress(done / total if total else None))
                step["count"] = original.stat().st_size
        except (Cancelled, ProcessError):
            raise
        except Exception as e:
            raise ProcessError(f"couldn't download it ({type(e).__name__}: {e})") from None
        with transaction(ctx.db):
            ctx.db.execute("UPDATE episodes SET original_path = ?, ever_downloaded = 1 WHERE id = ?",
                           (str(original), episode_id))
        captions.match_download(ctx.db, episode_id, original)
    fp_path = Path(ep["fingerprint_path"]) if ep["fingerprint_path"] else None
    if fp_path is None or not fp_path.exists():
        ctx.step("Fingerprinting", episode_id)
        try:
            with ctx.timer("fingerprint", episode_id) as step:
                fp = fingerprint_file(original, store.config)
                step["audio"] = fp.duration
        except Exception as e:
            raise ProcessError(f"couldn't read the audio ({e})") from None
        fp_path = paths.fingerprint(ep["guid"], store.config)
        fp.save(fp_path)
        store.upsert_episode(ep["guid"], fingerprint_path=str(fp_path), duration=fp.duration)


def _cutting(db: sqlite3.Connection, settings: Settings, episode_id: int) -> dict:
    """An episode's settings for episode_report: its podcast's, and where its transcript says the show plays."""
    ep = _episode(db, episode_id)
    row = db.execute("SELECT fill_gaps, max_break FROM podcasts WHERE id = ?", (ep["podcast_id"],)).fetchone()
    spoken = missing.spoken(missing.fit(db, settings, ep))
    if row is None:
        return {"fill_gaps": FILL_GAPS, "max_break": MAX_BREAK_SEC, "spoken": spoken}
    return {"fill_gaps": row["fill_gaps"], "max_break": row["max_break"], "spoken": spoken}


def cuts(db: sqlite3.Connection, episode_id: int, report: dict,
         phrase_cuts: list[dict] = ()) -> list[tuple[float, float]]:
    """Everything to cut from an episode: the report's sections, the intro and outro its phrases mark (and what
    its transcript is missing), and the ones cut by hand."""
    found = [(s["start"], s["end"]) for s in report["sections"]]
    found += [(p["start"], p["end"]) for p in phrase_cuts]
    found += [(r["start"], r["end"]) for r in db.execute("SELECT start, end FROM edits WHERE episode_id = ?",
                                                         (episode_id,))]
    return found


def _render(ctx: Context, settings: Settings, episode_id: int, report: dict) -> None:
    ep = _episode(ctx.db, episode_id)
    if ep["source_chapters"] is None:  # with the feed's transcripts, for its phrases
        ctx.step("Reading chapters", episode_id)
        with ctx.timer("chapters", episode_id):
            chapters.gather(ctx.db, ep, settings.media.transcripts(ep))
        ep = _episode(ctx.db, episode_id)
    said = phrases.for_episode(ctx.db, ep)
    unsaid = missing.cuts(missing.fit(ctx.db, settings, ep), missing.kept(ep))
    keep = cut.keep_spans(cuts(ctx.db, episode_id, report, said + unsaid), ep["duration"])
    if not keep:
        raise ProcessError("the whole episode was found to repeat, so nothing would be left")
    spec = json.dumps([[round(a, 2), round(b, 2)] for a, b in keep])
    edits = ctx.db.execute("SELECT start, end FROM edits WHERE episode_id = ?", (episode_id,)).fetchall()
    as_breaks = [{**m, "type": "break"} for m in unsaid]  # chapters call them ad breaks
    marks = chapters.build(json.loads(spec), chapters.cuts(report, edits, said + as_breaks),
                           json.loads(ep["source_chapters"]))
    if (spec == ep["cut_spec"] and json.dumps(marks) == ep["cut_chapters"] and ep["cut_path"]
            and Path(ep["cut_path"]).exists()):
        with transaction(ctx.db):  # nothing to cut differently
            ctx.db.execute("UPDATE episodes SET state = 'ready', state_note = NULL, phrase_cuts = ?, missing_cuts = ?, "
                           "ready_at = COALESCE(ready_at, ?) WHERE id = ?",
                           (json.dumps(said), json.dumps(unsaid), time.time(), episode_id))
        return
    ctx.step("Cutting", episode_id)
    try:
        with ctx.timer("cut", episode_id, audio=ep["duration"]):
            out = cut.render(Path(ep["original_path"]), settings.media.cut(ep), keep,
                             progress=ctx.progress, chapters=marks)
    except cut.CutError as e:
        raise ProcessError(str(e)) from None
    if ep["cut_path"] and Path(ep["cut_path"]) != out.path:
        Path(ep["cut_path"]).unlink(missing_ok=True)
    with transaction(ctx.db):
        ctx.db.execute(
            """UPDATE episodes SET state = 'ready', state_note = NULL, cut_path = ?, cut_type = ?, cut_duration = ?,
                      cut_bytes = ?, cut_spec = ?, cut_chapters = ?, phrase_cuts = ?, missing_cuts = ?,
                      processed_at = ?, ready_at = COALESCE(ready_at, ?), expired_at = NULL, ever_downloaded = 1
               WHERE id = ?""",
            (str(out.path), out.mime, out.duration, out.size, spec, json.dumps(marks), json.dumps(said),
             json.dumps(unsaid), time.time(), time.time(), episode_id))


def ensure_peaks(db: sqlite3.Connection, settings: Settings, episode_id: int, timer: Timer | None = None
                 ) -> Path | None:
    """The episode's waveform file, made from its download if there isn't one yet (timed by `timer`, if given)."""
    ep = _episode(db, episode_id)
    if ep["peaks_path"] and Path(ep["peaks_path"]).exists():
        return Path(ep["peaks_path"])
    if not ep["original_path"] or not Path(ep["original_path"]).exists():
        return None
    per_second = 10
    with (timer or Timer())("waveform", episode_id, audio=ep["duration"]):
        values = cut.peaks(Path(ep["original_path"]), per_second)
    path = settings.media.peaks(ep)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps({"per_second": per_second, "peaks": values}, separators=(",", ":")))
    tmp.replace(path)
    with transaction(db):
        db.execute("UPDATE episodes SET peaks_path = ? WHERE id = ?", (str(path), episode_id))
    return path


def run(ctx: Context, settings: Settings) -> None:
    episode_id = ctx.job["episode_id"]
    params = MatchParams()
    store = Store.on(ctx.db)
    paths = Paths(settings.data_dir)
    group = [episode_id]
    try:
        _fetch(ctx, settings, store, paths, episode_id)
        ep = _episode(ctx.db, episode_id)
        learn = not ep["skip_processing"]  # a filter keeps it out: cut it with known repeats, learn nothing from it
        have = ctx.db.execute("SELECT COUNT(*) FROM episodes WHERE podcast_id = ? AND fingerprint_path IS NOT NULL "
                              "AND skip_processing = 0", (ep["podcast_id"],)).fetchone()[0]
        extra = companions(ctx.db, ep, COMPARE_WITH - have) if learn else []
        group += take_along(ctx.db, ctx.job["id"], [c["id"] for c in extra], f"Coming along with “{ep['title']}”")
        for other in group[1:]:
            _fetch(ctx, settings, store, paths, other)

        ctx.step("Finding repeats", episode_id)
        with transaction(ctx.db):
            for other in group[1:]:
                ctx.db.execute("UPDATE episodes SET state = 'running', state_note = 'Finding repeats' WHERE id = ?",
                               (other,))
        with _detecting(ctx), ctx.timer("repeats", count=len(group)) as step:
            blocks = ctx.db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
            if learn:
                skip = {r["id"] for r in ctx.db.execute("SELECT id FROM episodes WHERE skip_processing = 1")}
                added = find_common_blocks(store, group, params, skip=skip, timer=ctx.timer)
            else:
                with ctx.timer("repeats.rescan", count=1):
                    rescan(store, episode_id, params, heard=True)
                added = 0
            with ctx.timer("repeats.reports", count=len(group)):
                reports = {ep_id: episode_report(store, ep_id, params, **_cutting(ctx.db, settings, ep_id))
                           for ep_id in group}
            step["audio"] = sum(_episode(ctx.db, ep_id)["duration"] or 0 for ep_id in group) or None
            step["detail"] = {"blocks": blocks, "added": added, "learn": learn}
        for ep_id in group:
            _render(ctx, settings, ep_id, reports[ep_id])
        for ep_id in group:
            ctx.step("Drawing the waveform", ep_id)
            ensure_peaks(ctx.db, settings, ep_id, ctx.timer)
            with transaction(ctx.db):
                ctx.db.execute("UPDATE episodes SET state = 'ready', state_note = NULL WHERE id = ?", (ep_id,))
        captions.queue(ctx.db, settings, group, ctx.job["requested_by"])
        recut = json.loads(ctx.job["payload"] or "{}").get("recut")
        if added and not recut:  # new repeats may play in the show's other ready episodes; cut those again
            others = [r["id"] for r in ctx.db.execute(  # (a re-cut never sets off more re-cuts)
                f"SELECT id FROM episodes WHERE podcast_id = ? AND state = 'ready' AND id NOT IN "
                f"({', '.join('?' * len(group))})", (ep["podcast_id"], *group))]
            enqueue(ctx.db, "process", others, ctx.job["requested_by"], skip_ready=False,
                    payload=json.dumps({"recut": True}), note="Waiting to be cut again: new repeats were found")
    except BaseException as e:
        failed = None if isinstance(e, Cancelled) else str(e)
        for other in group[1:]:  # companions were never asked for; they go back to rest quietly
            settle_episode(ctx.db, other)
        settle_episode(ctx.db, episode_id, failed)
        raise


# ---- the `unlearn` job: forget what filtered episodes taught ------------------

def run_unlearn(ctx: Context, settings: Settings) -> None:
    """Forget the repeats learned from episodes a processing filter now keeps out.

    A block automatically found goes when, leaving those episodes out, it plays
    in fewer than two episodes: it was only ever a repeat because a compilation
    replayed it. Blocks typed by hand stay. Ready episodes that played a
    forgotten block are cut again, and the filtered episodes lose their record
    of comparisons, so they are compared afresh if the filter goes.
    """
    wanted = json.loads(ctx.job["payload"] or "{}").get("episodes", [])
    store = Store.on(ctx.db)
    touched: set[int] = set()
    ctx.step("Forgetting repeats found in filtered episodes")
    with _detecting(ctx), ctx.timer("repeats.unlearn", count=0) as step, transaction(ctx.db):
        skip = {r["id"] for r in ctx.db.execute("SELECT id FROM episodes WHERE skip_processing = 1")}
        eps = [e for e in wanted if e in skip]  # a filter taken away since then leaves the rest be
        if not eps:
            return
        marks = ", ".join("?" * len(eps))
        blocks = [r["id"] for r in ctx.db.execute(
            f"""SELECT DISTINCT s.id FROM segments s LEFT JOIN occurrences o ON o.segment_id = s.id
                WHERE s.type_manual = 0 AND (o.episode_id IN ({marks}) OR s.source_episode_id IN ({marks}))""",
            (*eps, *eps))]
        for block in blocks:
            plays = {r[0] for r in ctx.db.execute(
                "SELECT DISTINCT episode_id FROM occurrences WHERE segment_id = ?", (block,))}
            if len(plays - skip) < 2:
                touched |= plays
                store.delete_segment(block)
                step["count"] += 1
        ctx.db.execute(f"DELETE FROM compared_pairs WHERE a_id IN ({marks}) OR b_id IN ({marks})", (*eps, *eps))
    ready = [r["id"] for r in ctx.db.execute(
        f"SELECT id FROM episodes WHERE state = 'ready' AND id IN ({', '.join('?' * len(touched))})",
        tuple(touched))] if touched else []
    enqueue(ctx.db, "process", ready, ctx.job["requested_by"], skip_ready=False,
            payload=json.dumps({"recut": True}), note="Waiting to be cut again: a filter changed what repeats")


# ---- the `edit` job: changes saved from the timeline -------------------------

# What a found section can be set to: as found, a type of this podcast's own (PODCAST_TYPES), or a false
# positive for every podcast. A section marked by hand is a local block, an intro or an outro.
EDIT_TYPES = {"auto", *PODCAST_TYPES, IGNORED}
BLOCK_TYPES = {SHOW, INTRO, OUTRO}
BUMPERS = {BUMPER, BUMPER_IN, BUMPER_OUT}
MIN_BLOCK_SEC = MatchParams.min_length_sec


def _edge_block(plays, blocks: list[int], section: dict, last: bool) -> int:
    """The block that ends a section (for an intro) or starts it (for an outro).

    Only that block takes the type: the report runs an intro back to the start and an outro on to
    the end anyway, and the section's other blocks are often ads that play elsewhere too.
    """
    lo, hi = float(section.get("start", 0)) - 1, float(section.get("end", 1e12)) + 1
    inside = [r for r in plays if r["segment_id"] in blocks and r["start"] >= lo and r["end"] <= hi]
    if not inside:
        return blocks[-1] if last else blocks[0]
    pick = max(inside, key=lambda r: r["end"]) if last else min(inside, key=lambda r: r["start"])
    return pick["segment_id"]


def _reported_blocks(report: dict | None, *keys: str) -> set[int]:
    return {b for key in keys for span in (report or {}).get(key, []) for b in span.get("blocks", [])}


def check_edits(db: sqlite3.Connection, episode_id: int, changes: dict) -> dict:
    """Keep the timeline changes that make sense for this episode: blocks that play in it, types a section can
    take, stretches long enough to cut, its own hand-made cuts and blocks. `changes` is shaped as the timeline
    sends it:

        {"types": [{"type", "blocks": [ids], "bumpers": [ids], "start", "end"}],
         "add": [{"start", "end", "as_block", "type"}], "remove": [edit ids], "delete": [block ids],
         "missing": [{"start", "end", "keep"}]}

    (the web app checks that shape). Raises Busy while the episode is being processed, and ProcessError when
    no change is left."""
    ep = _episode(db, episode_id)
    if ep["state"] in ACTIVE:
        raise Busy("it's being processed; save again when that's done")
    plays = db.execute("SELECT segment_id, start, end FROM occurrences WHERE episode_id = ?", (episode_id,)).fetchall()
    row = db.execute("SELECT report FROM reports WHERE episode_id = ?", (episode_id,)).fetchone()
    # False positives aren't kept as plays; the report says where they are.
    here = {r["segment_id"] for r in plays} | _reported_blocks(json.loads(row["report"]) if row else None,
                                                                 "not_repeats")
    types = []
    for t in changes.get("types", []):
        value = t.get("type")
        blocks = [b for b in t.get("blocks", []) if b in here]
        bumpers = [b for b in t.get("bumpers", []) if b in here]
        if value in (INTRO, OUTRO) and blocks:
            blocks = [_edge_block(plays, blocks, t, last=value == INTRO)]
        if value in EDIT_TYPES and (blocks or bumpers):
            types.append({"type": value, "blocks": blocks, "bumpers": bumpers if value in (IGNORED, KEPT) else []})
    added = []
    for a in changes.get("add", []):
        start, end = max(0.0, a["start"]), min(ep["duration"] or 0.0, a["end"])
        block_type = a.get("type", "repeated")
        if end - start < MIN_BLOCK_SEC or (a.get("as_block") and block_type not in BLOCK_TYPES):
            continue
        added.append({"start": round(start, 2), "end": round(end, 2), "as_block": bool(a.get("as_block")),
                      "type": block_type})
    removed = changes.get("remove", [])
    removed = [r["id"] for r in db.execute(
        f"SELECT id FROM edits WHERE episode_id = ? AND id IN ({', '.join('?' * len(removed))})",
        (episode_id, *removed))] if removed else []
    deleted = [b for b in changes.get("delete", []) if b in here]
    deleted = [r["id"] for r in db.execute(
        f"SELECT id FROM segments WHERE by_hand = 1 AND id IN ({', '.join('?' * len(deleted))})",
        tuple(deleted))] if deleted else []
    kept = [{"start": round(max(0.0, m["start"]), 2), "end": round(min(ep["duration"] or 0.0, m["end"]), 2),
             "keep": bool(m["keep"])} for m in changes.get("missing", [])
            if min(ep["duration"] or 0.0, m["end"]) > max(0.0, m["start"])]
    if not (types or added or removed or deleted or kept):
        raise ProcessError("nothing to change")
    return {"types": types, "add": added, "remove": removed, "delete": deleted, "missing": kept}


def queue_edits(db: sqlite3.Connection, episode_id: int, user_id: int | None, changes: dict) -> None:
    """Queue an `edit` job to apply timeline changes (as `check_edits` returns them) and cut the episode again."""
    enqueue(db, "edit", [episode_id], user_id, skip_ready=False, payload=json.dumps(changes),
            note="Saving your changes")


def _playing(db: sqlite3.Connection, blocks: set[int], podcast_id: int | None = None) -> set[int]:
    """The episodes these blocks play in: as plays, or listed as false positives in their reports; only those
    of `podcast_id`, if given."""
    if not blocks:
        return set()
    marks = ", ".join("?" * len(blocks))
    scope, args = ("AND e.podcast_id = ?", (podcast_id,)) if podcast_id is not None else ("", ())
    found = {r[0] for r in db.execute(
        f"SELECT DISTINCT o.episode_id FROM occurrences o JOIN episodes e ON e.id = o.episode_id "
        f"WHERE o.segment_id IN ({marks}) {scope}", (*blocks, *args))}
    for r in db.execute(f"SELECT r.episode_id, r.report FROM reports r JOIN episodes e ON e.id = r.episode_id "
                        f"WHERE r.report LIKE '%\"not_repeats\": [{{%' {scope}", args):
        if _reported_blocks(json.loads(r["report"]), "not_repeats") & blocks:
            found.add(r["episode_id"])
    return found


def run_edit(ctx: Context, settings: Settings) -> None:
    """Apply saved timeline changes, cut the episode again, and queue the other episodes they touch: those of
    this podcast for its own types, every podcast's for changes to a block itself."""
    episode_id = ctx.job["episode_id"]
    changes = json.loads(ctx.job["payload"] or "{}")
    params = MatchParams()
    store = Store.on(ctx.db)
    try:
        ctx.step("Saving your changes", episode_id)
        ep = _episode(ctx.db, episode_id)
        podcast = ep["podcast_id"]
        if not ep["fingerprint_path"] or not Path(ep["fingerprint_path"]).exists():
            raise ProcessError("its fingerprint is gone; process it again first")
        local, everywhere = set(changes.get("delete", [])), set()
        for t in changes["types"]:
            for block in t["blocks"] + t["bumpers"]:
                seg = store.segment(block)
                # A block's own hand type (a false positive, or one from the command line) is for every podcast.
                if t["type"] == IGNORED or (t["type"] == "auto" and seg["type_manual"] and not seg["by_hand"]):
                    everywhere.add(block)
                else:
                    local.add(block)
        touched = _playing(ctx.db, everywhere) | _playing(ctx.db, local, podcast)
        with _detecting(ctx), ctx.timer("repeats", count=1):
            fp = load_fingerprint(store, episode_id) if any(a["as_block"] for a in changes["add"]) else None
            with ctx.timer("repeats.edit"), transaction(ctx.db):  # the changes land together, or not at all
                for t in changes["types"]:
                    for block in t["blocks"] + t["bumpers"]:
                        if t["type"] == IGNORED:
                            store.set_type(block, IGNORED)
                        elif t["type"] == "auto":
                            store.set_podcast_type(podcast, block, None)
                            if block in everywhere:
                                store.set_type(block, None)
                        else:
                            store.set_podcast_type(podcast, block, t["type"])
                for block in changes.get("delete", []):
                    store.delete_segment(block)
                for a in changes["add"]:
                    if a["as_block"]:
                        block = store.add_segment(fp.slice(a["start"], a["end"]), episode_id, a["start"], a["end"],
                                                  by_hand=True)
                        store.set_type(block, a["type"])  # of this podcast only, and cut however short
                    else:
                        ctx.db.execute("INSERT INTO edits (episode_id, start, end, created_by, created_at) "
                                       "VALUES (?, ?, ?, ?, ?)",
                                       (episode_id, a["start"], a["end"], ctx.job["requested_by"], time.time()))
                for edit_id in changes["remove"]:
                    ctx.db.execute("DELETE FROM edits WHERE id = ? AND episode_id = ?", (edit_id, episode_id))
                if changes.get("missing"):
                    spans = missing.kept(_episode(ctx.db, episode_id))
                    for m in changes["missing"]:
                        spans = missing.keep(spans, m["start"], m["end"], m["keep"])
                    ctx.db.execute("UPDATE episodes SET missing_kept = ? WHERE id = ?",
                                   (json.dumps([list(k) for k in spans]), episode_id))
            with ctx.timer("repeats.rescan", count=1):
                rescan(store, episode_id, params, heard=True)
            with ctx.timer("repeats.reports", count=1):
                report = episode_report(store, episode_id, params, **_cutting(ctx.db, settings, episode_id))
        _render(ctx, settings, episode_id, report)
        others = touched - {episode_id}
        if any(a["as_block"] for a in changes["add"]):  # a new block may well play in the show's other episodes
            others |= {r["id"] for r in ctx.db.execute(
                "SELECT id FROM episodes WHERE podcast_id = ? AND state = 'ready' AND id != ?",
                (ep["podcast_id"], episode_id))}
        ready = [r["id"] for r in ctx.db.execute(
            f"SELECT id FROM episodes WHERE state = 'ready' AND id IN ({', '.join('?' * len(others))})",
            tuple(others))] if others else []
        enqueue(ctx.db, "process", ready, ctx.job["requested_by"], skip_ready=False,
                note="Waiting to be cut again after a change")
    except BaseException as e:
        settle_episode(ctx.db, episode_id, None if isinstance(e, Cancelled) else str(e))
        raise


def timeline(db: sqlite3.Connection, episode_id: int, report: dict | None) -> dict:
    """What the episode page's timeline draws: each cut section, what it is, and the blocks and bumpers in it;
    the sections kept in this podcast; the intro and outro its phrases cut; and the false positives that play in
    it, for a list of their own. `custom` marks a section that is what it is because of this podcast's own types
    or blocks (an Outro block that also opens an episode is cut there as a plain repeat, so that one isn't);
    `own` lists its blocks marked by hand."""
    ep = _episode(db, episode_id)
    podcast = ep["podcast_id"]
    store = Store.on(db)
    types = store.types(podcast)
    mine = store.podcast_types(podcast) if podcast is not None else {}
    own = {r[0] for r in db.execute("SELECT id FROM segments WHERE by_hand = 1")}
    bumpers = [r for r in db.execute(
        "SELECT o.segment_id, o.start, o.end FROM occurrences o JOIN segments s ON s.id = o.segment_id "
        f"WHERE o.episode_id = ? AND s.type IN ({', '.join('?' * len(BUMPERS))}) ORDER BY o.start",
        (episode_id, *BUMPERS))]
    sections = []
    duration = (report or {}).get("duration") or 0
    for i, s in enumerate((report or {}).get("sections", [])):
        blocks = [b for b in s["blocks"] if b in types]
        has = {types[b] for b in blocks}
        # An intro block can also play inside a break; the section is the intro only where it starts the episode.
        if INTRO in has and s["start"] <= 1.0:
            set_to = INTRO
        elif OUTRO in has and s["end"] >= duration - 1.0:
            set_to = OUTRO
        else:
            set_to = SHOW if SHOW in has else "auto"
        near = [{"block": r["segment_id"], "start": r["start"], "end": r["end"]} for r in bumpers
                if r["start"] <= s["end"] + 5 and r["end"] >= s["start"] - 5]
        is_break = "break" in s["types"]
        custom = (set_to in {mine.get(b) for b in blocks} or bool(own & set(blocks))
                  or (is_break and any(b["block"] in mine for b in near)))
        sections.append({"id": f"r{i}", "start": s["start"], "end": s["end"], "value": set_to,
                         "is_break": is_break, "blocks": blocks, "bumpers": near if is_break else [],
                         "custom": custom, "own": sorted(own & set(blocks))})
    for i, k in enumerate((report or {}).get("kept", [])):
        blocks = [b for b in k["blocks"] if b in types]
        sections.append({"id": f"k{i}", "start": k["start"], "end": k["end"], "value": KEPT, "is_break": False,
                         "blocks": blocks, "bumpers": [], "custom": True, "own": sorted(own & set(blocks))})
    not_repeats = [{"id": f"x{i}", "start": x["start"], "end": x["end"], "blocks": x["blocks"]}
                   for i, x in enumerate((report or {}).get("not_repeats", []))]
    manual = [{"id": f"m{r['id']}", "edit": r["id"], "start": r["start"], "end": r["end"]}
              for r in db.execute("SELECT * FROM edits WHERE episode_id = ? ORDER BY start", (episode_id,))]
    said = [{"id": f"p{i}", **p} for i, p in enumerate(phrases.saved(ep))]
    unsaid = [{"id": f"u{i}", **m} for i, m in enumerate(missing.listing(ep))]
    return {"sections": sections, "manual": manual, "not_repeats": not_repeats, "phrases": said, "missing": unsaid}

