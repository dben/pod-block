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
(worker.py), so a long transcription doesn't hold up downloads;
`PODBLOCK_CAPTION_WORKERS` runs several at once, for a service with more than
one replica. With more than one, a job that lands on a busy replica hangs up
and asks again, for the Service to send it to another (`ask`'s `patience`).
A transcription cut off part way (a replica restarting, say) is asked for once
more before the job fails.

Each word's time is saved beside the captions too, for the show's intro and
outro phrases (`phrases.py`); hearing one queues the episode to be cut again.

An episode can also be transcribed again on purpose (`queue(force=True)`, the
downloads page's Transcribe again): the new captions then replace whatever it
had, its feed's transcripts included.

When the service has speakers turned on, it also says who speaks when; those
turns are saved beside the captions (`speakers.py`) for `podblock speakers`
to compare with the repeats found. They don't go into the captions yet.
"""

from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import textwrap
import time
from pathlib import Path

import requests
import urllib3

from ..timing import Timer
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
BUSY_PATIENCE = 600.0  # seconds to keep asking for a free replica before waiting for a busy one
BUSY_WAIT = (2.0, 30.0)  # seconds before asking again: the first wait, doubling up to the second
RETRY_AFTER = 10.0  # seconds before asking once more when a transcription is cut off


class CaptionError(Exception):
    pass


class CutOff(CaptionError):
    """The service couldn't be reached, or stopped answering part way: worth asking once more."""


class _Busy(Exception):
    """The replica asked is working on another episode."""


def wanted(ep: sqlite3.Row, settings: Settings, force: bool = False) -> bool:
    """Whether an episode should get captions: the service is set, it has a download, and no timed transcript
    (or, with `force`, whatever it has)."""
    return bool(settings.whisper_url and ep["original_path"] and Path(ep["original_path"]).exists()
                and (force or not json.loads(ep["transcripts"] or "[]")))


def queue(db: sqlite3.Connection, settings: Settings, episode_ids: list[int], user_id: int | None,
          force: bool = False) -> list[int]:
    """Queue captions for the episodes that want them and don't have a captions job already. Returns those.
    With `force`, the captions replace any transcript the episode has: its feed's, or earlier captions."""
    queued = []
    for ep_id in dict.fromkeys(episode_ids):
        ep = db.execute("SELECT * FROM episodes WHERE id = ?", (ep_id,)).fetchone()
        if ep is None or not wanted(ep, settings, force):
            continue
        if db.execute(f"SELECT 1 FROM jobs WHERE kind = ? AND state IN ({sql(ACTIVE)}) "
                      "AND json_extract(payload, '$.episode') = ?", (KIND, ep_id)).fetchone():
            continue
        enqueue_task(db, KIND, user_id, json.dumps({"episode": ep_id, **({"force": True} if force else {})}))
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


def ask(ctx, url: str, what: str, audio: Path, params: dict | None = None, patience: float = 0.0,
        timer: Timer | None = None) -> dict:
    """Send the audio to the service's `what` ("transcribe" or "speakers") and read back what it found:
    {language, duration, segments, speakers, options}, the last two when it says who speaks when. `ctx` is told
    each step and how far through (a job's Context, or anything with `step` and `progress`).

    For `patience` seconds, a busy replica is hung up on and the audio sent again a little later, which the
    Service may give to another; after that it waits its turn. Cut off part way, it asks once more.

    `timer` is told, once it is done, how long sending the audio took (the last time), how long it waited for a
    transcriber (every try before, and waiting its turn), and how long each of the service's steps took."""
    give_up = start = time.monotonic()
    give_up += patience
    wait, retried, tries = BUSY_WAIT[0], False, 0
    while True:
        tries += 1
        took: dict[str, float] = {}
        try:
            found = _ask(ctx, url, what, audio, params, leave_if_busy=time.monotonic() < give_up, took=took)
        except _Busy:
            ctx.step("Waiting for a free transcriber")
            _pause(ctx, wait * random.uniform(1, 1.5))  # spread out, so jobs turned away together don't collide again
            wait = min(wait * 2, BUSY_WAIT[1])
        except CutOff as e:
            if retried:
                raise
            retried = True
            log.warning("%s; asking again", e)
            ctx.step("Asking the transcriber again")
            _pause(ctx, RETRY_AFTER)
        else:
            if timer is not None:
                _report(timer, took, found["duration"], audio, tries, time.monotonic() - start)
            return found


def _report(timer: Timer, took: dict[str, float], duration: float | None, audio: Path, tries: int,
            total: float) -> None:
    """What the last try's steps took, and the rest of `total` as waiting for a transcriber."""
    timer.add("captions.upload", took.get("upload", 0.0), count=audio.stat().st_size)
    waited = total - sum(took.values()) + took.get("wait", 0.0)
    if tries > 1 or took.get("wait"):
        timer.add("captions.wait", max(0.0, waited), count=tries)
    for step in ("transcribe", "speakers"):
        if step in took:
            timer.add(f"captions.{step}", took[step], audio=duration)


def _pause(ctx, seconds: float) -> None:
    """Wait, noticing a cancel meanwhile."""
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        time.sleep(min(left, 1.0))
        ctx.progress(None)


def _lines(resp: requests.Response):
    """The answer's lines as they arrive. `iter_lines` waits for 512 bytes, which a short line such as "waiting"
    can sit behind for minutes; urllib3 2's `read1` hands over whatever has come."""
    read1 = getattr(resp.raw, "read1", None)
    if read1 is None:  # pragma: no cover (urllib3 1)
        yield from resp.iter_lines()
        return
    rest = b""
    while chunk := read1(1 << 16):
        *lines, rest = (rest + chunk).split(b"\n")
        yield from lines
    if rest:
        yield rest  # cut short: every whole line ends in a newline


def _ask(ctx, url: str, what: str, audio: Path, params: dict | None, leave_if_busy: bool,
         took: dict[str, float]) -> dict:
    """One try. `took` is given the seconds each part took: upload, wait (its turn), transcribe, speakers."""
    found: dict = {"language": None, "duration": None, "segments": [], "speakers": None, "options": None}
    done = False
    phase, since = "upload", time.monotonic()

    def then(next_phase: str) -> None:
        nonlocal phase, since
        now = time.monotonic()
        took[phase] = took.get(phase, 0.0) + now - since
        phase, since = next_phase, now

    try:
        with audio.open("rb") as body, requests.post(
                f"{url}/{what}", data=body, params=params, headers={"Content-Type": "application/octet-stream"},
                stream=True, timeout=TIMEOUT) as resp:
            working = "transcribe" if what == "transcribe" else "speakers"
            then(working)  # the answer starts once the service has the whole upload
            resp.raise_for_status()
            ctx.step("Transcribing" if what == "transcribe" else "Finding who speaks when")
            for raw in _lines(resp):
                if not raw:
                    continue
                try:
                    line = json.loads(raw)
                except ValueError:  # the last line, cut short
                    raise CutOff("the transcriber stopped part way through a line") from None
                if line.get("waiting"):
                    if leave_if_busy:
                        raise _Busy()
                    phase = "wait" if phase == working and not found["duration"] else phase  # since the upload
                    ctx.progress(None)  # also notices a cancel
                    continue
                if phase == "wait":  # its turn came: said so, or (an older service) seen by its first line
                    then(working)
                if line.get("working"):
                    continue
                if "error" in line:
                    raise CaptionError(f"the transcriber failed: {line['error']}")
                elif line.get("done"):
                    then("done")
                    done = True
                elif "start" in line:
                    found["segments"].append(line)
                    duration = found["duration"]
                    ctx.progress(min(1.0, line["end"] / duration) if duration else None)
                elif line.get("step") == "speakers" and what == "transcribe":  # a second step, after the words
                    then("speakers")
                    ctx.step("Finding who speaks when")
                elif "progress" in line:
                    ctx.progress(line["progress"])
                elif "speakers" in line:
                    found["speakers"], found["options"] = line["speakers"], line.get("options")
                elif "duration" in line:
                    found["language"], found["duration"] = line.get("language"), line["duration"]
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 503:  # loading, or stopping
            raise CutOff(f"the transcriber at {url} isn't ready ({e})") from None
        raise CaptionError(f"the transcriber at {url} refused it ({e})") from None
    except (requests.RequestException, urllib3.exceptions.HTTPError) as e:
        raise CutOff(f"couldn't reach the transcriber at {url} ({type(e).__name__}: {e})") from None
    if not done:
        raise CutOff("the transcriber stopped part way through")
    return found


def run(ctx: Context, settings: Settings) -> None:
    payload = json.loads(ctx.job["payload"] or "{}")
    episode_id, force = payload.get("episode"), bool(payload.get("force"))
    if not settings.whisper_url:
        raise CaptionError("no transcriber is set (PODBLOCK_WHISPER_URL)")
    ep = ctx.db.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if ep is None:
        raise CaptionError("the episode is gone")
    if json.loads(ep["transcripts"] or "[]") and not force:
        return  # it has one already: from its feed, or an earlier captions job
    if not ep["original_path"] or not Path(ep["original_path"]).exists():
        raise CaptionError("its download is gone; download it again for captions")
    ctx.step("Sending the audio to the transcriber")
    language = _language(ctx.db, ep["podcast_id"])
    heard = ask(ctx, settings.whisper_url, "transcribe", Path(ep["original_path"]),
                {"language": language} if language else None,
                patience=BUSY_PATIENCE if settings.caption_workers > 1 else 0.0, timer=ctx.timer)
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
        before = json.loads(ep["transcripts"] or "[]")
        if (now is None or now["original_path"] != ep["original_path"]
                or json.loads(now["transcripts"] or "[]") != before):  # cleaned up meanwhile, or the feed's were read
            _forget([saved], keep=json.loads(now["transcripts"] or "[]") if now else [])
            return
        ctx.db.execute("UPDATE episodes SET transcripts = ? WHERE id = ?", (json.dumps([saved]), episode_id))
    _forget(before, keep=[saved])  # what these replace
    log.info("captions for episode %d: %d segments, %s%s", episode_id, len(heard["segments"]),
             heard["language"] or "language unknown",
             f", {len({t[2] for t in heard['speakers']})} speakers" if heard["speakers"] else "")
    phrases.requeue(ctx.db, ep["podcast_id"], ctx.job["requested_by"], [episode_id],
                    note="Waiting to be cut again: an intro or outro phrase was heard")


def _forget(transcripts: list[dict], keep: list[dict]) -> None:
    """Delete these transcripts' files, but not the ones `keep` names: new captions are written over old ones."""
    kept = {t.get(k) for t in keep for k in ("path", "speakers", "words")}
    gone = [{k: (t.get(k) if t.get(k) not in kept else None) for k in ("path", "speakers", "words")}
            for t in transcripts]
    chapters.forget({"transcripts": json.dumps(gone)})


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
