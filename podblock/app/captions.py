"""The `captions` job: a transcript of an episode, made by the transcription service (whisper/).

When `PODBLOCK_WHISPER_URL` is set, each episode that is downloaded and cut,
and has no timed transcript from its feed, gets a `captions` job. The job
sends the download (the uncut audio) to the service, which streams back what
it hears, segment by segment, with a time for each word. Those are split into
captions of at most two short lines, and saved as WebVTT beside the feed's
transcripts, as one of them (`episodes.transcripts`, with `rel` "captions").
From there it is served like a feed's own: its times are moved to fit the cut
as it is served (`chapters.py`), so a re-cut needs no new transcript.

The job works on no one episode (`jobs.enqueue_task`, the episode in its
payload), so the episode stays ready, and in the feeds, while it runs, and an
edit can be saved meanwhile. Captions jobs run in a thread of their own
(worker.py), so a long transcription doesn't hold up downloads.

Each word's time is saved beside the captions too, for the show's intro and
outro phrases (`phrases.py`); hearing one queues the episode to be cut again.

When the service has speakers turned on, it also says who speaks when; those
turns are saved beside the captions (`speakers.py`) for `podblock speakers`
to compare with the repeats found. They don't go into the captions yet.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import textwrap
from pathlib import Path

import requests

from . import chapters, phrases, speakers
from .config import Settings
from .db import transaction
from .jobs import Context, enqueue_task
from .states import ACTIVE, sql

log = logging.getLogger(__name__)

KIND = "captions"
TIMEOUT = (10, 900)  # connect; and the longest wait for the next line, which a large model on a slow CPU needs
LINE = 42  # characters in a caption line; two lines at most
MAX_SEC = 7.0  # the longest a caption stays up
PAUSE = 1.5  # seconds of silence that end a caption
SENTENCE_END = re.compile(r"[.?!…。？！]['\")\]]*$")
CLAUSE_END = re.compile(r"[,;:，、；：]['\")\]]*$")
MIN_SENTENCE = 24  # characters a caption has before a sentence's end closes it; a clause's needs a line (LINE)


class CaptionError(Exception):
    pass


def wanted(ep: sqlite3.Row, settings: Settings) -> bool:
    """Whether an episode should get captions: the service is set, it has a download, and no timed transcript."""
    return bool(settings.whisper_url and ep["original_path"] and Path(ep["original_path"]).exists()
                and not json.loads(ep["transcripts"] or "[]"))


def queue(db: sqlite3.Connection, settings: Settings, episode_ids: list[int], user_id: int | None) -> list[int]:
    """Queue captions for the episodes that want them and don't have a captions job already. Returns those."""
    queued = []
    for ep_id in dict.fromkeys(episode_ids):
        ep = db.execute("SELECT * FROM episodes WHERE id = ?", (ep_id,)).fetchone()
        if ep is None or not wanted(ep, settings):
            continue
        if db.execute(f"SELECT 1 FROM jobs WHERE kind = ? AND state IN ({sql(ACTIVE)}) "
                      "AND json_extract(payload, '$.episode') = ?", (KIND, ep_id)).fetchone():
            continue
        enqueue_task(db, KIND, user_id, json.dumps({"episode": ep_id}))
        queued.append(ep_id)
    return queued


# ---- splitting what was heard into captions -------------------------------------

def _words(segment: dict) -> list[tuple[float, float, str]]:
    """A segment's words, with their times; a segment without them is one long word."""
    words = [(float(a), float(b), str(w)) for a, b, w in segment.get("words") or [] if str(w).strip()]
    if words:
        return words
    text = str(segment.get("text") or "")
    return [(float(segment["start"]), float(segment["end"]), text)] if text.strip() else []


def _fits(text: str) -> bool:
    return len(textwrap.wrap(text, LINE)) <= 2


def _wrap(text: str) -> str:
    """Split a caption over two lines when it won't fit on one: at the space nearest the middle that leaves both
    short enough."""
    if len(text) <= LINE:
        return text
    middle = len(text) // 2
    spaces = [i for i, ch in enumerate(text) if ch == " " and i <= LINE and len(text) - i - 1 <= LINE]
    if not spaces:
        return "\n".join(textwrap.wrap(text, LINE))
    at = min(spaces, key=lambda i: abs(i - middle))
    return f"{text[:at]}\n{text[at + 1:]}"


def cues(segments: list[dict]) -> list[tuple[float, float, str]]:
    """Captions from whisper's segments: each is (start, end, text). Whisper's segments often end mid-sentence,
    so captions run across them. A caption ends at a pause, at a sentence's end once it has a few words, or at a
    clause's once it fills a line, and before it would outgrow two lines or MAX_SEC."""
    out: list[tuple[float, float, str]] = []
    start = end = None
    text = ""
    for a, b, word in (w for seg in segments for w in _words(seg)):
        joined = (text + word).strip()
        if start is not None and (not _fits(joined) or b - start > MAX_SEC or a - end > PAUSE):
            out.append((start, end, text.strip()))
            start, text, joined = None, "", word.strip()
        if start is None:
            start = a
        text, end = text + word, max(b, start)
        if ((SENTENCE_END.search(word.strip()) and len(joined) >= MIN_SENTENCE)
                or (CLAUSE_END.search(word.strip()) and len(joined) >= LINE)):
            out.append((start, end, text.strip()))
            start, text = None, ""
    if start is not None and text.strip():
        out.append((start, end, text.strip()))
    fixed = []
    for i, (a, b, text) in enumerate(out):
        nxt = out[i + 1][0] if i + 1 < len(out) else None
        b = min(b, nxt) if nxt is not None and nxt > a else b  # captions don't overlap
        if b - a >= 0.05:
            fixed.append((a, b, _wrap(text)))
    return fixed


def _stamp(secs: float) -> str:
    ms = int(round(secs * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def webvtt(captions: list[tuple[float, float, str]]) -> str:
    text = ["WEBVTT", "NOTE Made by pod-block with faster-whisper."]
    for a, b, words in captions:
        words = words.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text.append(f"{_stamp(a)} --> {_stamp(b)}\n{words}")
    return "\n\n".join(text) + "\n"


# ---- the job ---------------------------------------------------------------------

def _language(db: sqlite3.Connection, podcast_id: int | None) -> str | None:
    """The show's language, as whisper names it: "en" of "en-us"."""
    row = db.execute("SELECT language FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    code = (row["language"] or "").strip().lower().split("-")[0] if row else ""
    return code if re.fullmatch(r"[a-z]{2,3}", code) else None


def ask(ctx, url: str, what: str, audio: Path, params: dict | None = None) -> dict:
    """Send the audio to the service's `what` ("transcribe" or "speakers") and read back what it found:
    {language, duration, segments, speakers, options}, the last two when it says who speaks when. `ctx` is told
    each step and how far through (a job's Context, or anything with `step` and `progress`)."""
    found: dict = {"language": None, "duration": None, "segments": [], "speakers": None, "options": None}
    done = False
    try:
        with audio.open("rb") as body, requests.post(
                f"{url}/{what}", data=body, params=params, headers={"Content-Type": "application/octet-stream"},
                stream=True, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            ctx.step("Transcribing" if what == "transcribe" else "Finding who speaks when")
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = json.loads(raw)
                if line.get("waiting"):
                    ctx.progress(None)  # also notices a cancel
                elif "error" in line:
                    raise CaptionError(f"the transcriber failed: {line['error']}")
                elif line.get("done"):
                    done = True
                elif "start" in line:
                    found["segments"].append(line)
                    duration = found["duration"]
                    ctx.progress(min(1.0, line["end"] / duration) if duration else None)
                elif line.get("step") == "speakers" and what == "transcribe":  # a second step, after the words
                    ctx.step("Finding who speaks when")
                elif "progress" in line:
                    ctx.progress(line["progress"])
                elif "speakers" in line:
                    found["speakers"], found["options"] = line["speakers"], line.get("options")
                elif "duration" in line:
                    found["language"], found["duration"] = line.get("language"), line["duration"]
    except requests.RequestException as e:
        raise CaptionError(f"couldn't reach the transcriber at {url} ({type(e).__name__}: {e})") from None
    if not done:
        raise CaptionError("the transcriber stopped part way through")
    return found


def run(ctx: Context, settings: Settings) -> None:
    episode_id = json.loads(ctx.job["payload"] or "{}").get("episode")
    if not settings.whisper_url:
        raise CaptionError("no transcriber is set (PODBLOCK_WHISPER_URL)")
    ep = ctx.db.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if ep is None:
        raise CaptionError("the episode is gone")
    if json.loads(ep["transcripts"] or "[]"):
        return  # it has one already: from its feed, or an earlier captions job
    if not ep["original_path"] or not Path(ep["original_path"]).exists():
        raise CaptionError("its download is gone; download it again for captions")
    ctx.step("Sending the audio to the transcriber")
    language = _language(ctx.db, ep["podcast_id"])
    heard = ask(ctx, settings.whisper_url, "transcribe", Path(ep["original_path"]),
                {"language": language} if language else None)
    stem = settings.media.transcripts(ep)
    path = stem.with_name(stem.name + ".captions.vtt")
    _write(path, webvtt(cues(heard["segments"])))
    timed = stem.with_name(stem.name + ".words.json")  # each word's time, for finding intro and outro phrases
    _write(timed, json.dumps([w for seg in heard["segments"] for w in _words(seg)], separators=(",", ":")))
    saved = {"format": "vtt", "path": str(path), "rel": "captions", "made": "whisper", "words": str(timed),
             **({"language": heard["language"]} if heard["language"] else {})}
    if heard["speakers"] is not None:
        saved["speakers"] = str(speakers.save(stem, heard["speakers"], heard["options"]))
    with transaction(ctx.db):
        now = ctx.db.execute("SELECT original_path, transcripts FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if now is None or now["original_path"] != ep["original_path"] or json.loads(now["transcripts"] or "[]"):
            chapters.forget({"transcripts": json.dumps([saved])})  # cleaned up meanwhile, or the feed's were read
            return
        ctx.db.execute("UPDATE episodes SET transcripts = ? WHERE id = ?", (json.dumps([saved]), episode_id))
    log.info("captions for episode %d: %d segments, %s%s", episode_id, len(heard["segments"]),
             heard["language"] or "language unknown",
             f", {len({t[2] for t in heard['speakers']})} speakers" if heard["speakers"] else "")
    phrases.requeue(ctx.db, ep["podcast_id"], ctx.job["requested_by"], [episode_id],
                    note="Waiting to be cut again: an intro or outro phrase was heard")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
