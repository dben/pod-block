"""Missing from the transcript: audio the feed's transcript doesn't have, which is ads put in as it is downloaded.

Shows with ads put in on the fly (dynamic ads) usually make their feed's
transcript from the episode as recorded, before any ads go in. Where the
download has speech the transcript doesn't, the speech is an ad. When this
check is on for a show (`podcasts.transcript_check`; on by itself when the
transcriber is set and the show's feed has transcripts), each download with
a feed transcript is transcribed as well (captions.py), and the two are
lined up word for word (`align`):

- a stretch the transcriber heard, with next to nothing for it in the feed's
  transcript, is cut as missing (`Fit.missing`);
- so is a stretch the download runs on for much longer than the transcript
  does between the same two words: the feed's times follow the recording,
  so they fall behind by the length of each ad, spoken or not;
- what lines up is the show itself (`Fit.spoken`). An ad break found from
  bumpers that is mostly that is no break (report.py), as when a show plays
  its break sting before a short notice. Only a transcript known to leave
  ads out says so: one with no missing stretch may well have the ads in it.

A transcript that hardly lines up at all (another edit of the episode, or
another language) gives nothing.

Missing stretches are saved on the episode (`episodes.missing_cuts`) each
time it is cut, as [{type: "missing", start, end}], and cut with the
repeats; in chapters they read as ad breaks.
"""

from __future__ import annotations

import bisect
import difflib
import json
import sqlite3
from dataclasses import dataclass, field

from . import chapters, phrases
from .config import Settings
from .db import transaction
from .jobs import enqueue
from .states import READY

MISSING = "missing"
GRAM = 4  # words in a row that pin the two transcripts together, where they occur once in each
MIN_FIT = 0.5  # share of the feed's words that must line up for the transcript to be of this audio
MIN_SEC = 20.0  # the shortest missing stretch
MIN_WORDS = 15  # words heard in a stretch before it can count as missing by its words alone
STRAY = 0.15  # share of a stretch's heard words the feed's transcript may have there and still miss it
SPOKEN_JOIN = 10.0  # lined-up words this close make one stretch of the show

Word = tuple[float, float, str]


@dataclass
class Fit:
    coverage: float  # share of the feed's words that lined up
    missing: list[tuple[float, float]] = field(default_factory=list)
    spoken: list[tuple[float, float]] = field(default_factory=list)  # in the download's times


def _anchors(feed: list[str], heard: list[str]) -> list[tuple[int, int]]:
    """(feed index, heard index) of runs of GRAM words found once in each, in order in both."""
    def once(words: list[str]) -> dict[tuple, int]:
        seen: dict[tuple, int] = {}
        for i in range(len(words) - GRAM + 1):
            gram = tuple(words[i:i + GRAM])
            seen[gram] = -1 if gram in seen else i
        return {g: i for g, i in seen.items() if i >= 0}

    in_feed = once(feed)
    pairs = sorted((i, in_feed[g]) for g, i in once(heard).items() if g in in_feed)  # (heard, feed)
    # The longest run of pairs in order in the feed too; the rest are words said twice in different places.
    tails: list[int] = []
    tail_at: list[int] = []
    back = [-1] * len(pairs)
    for n, (_, f) in enumerate(pairs):
        k = bisect.bisect_left(tails, f)
        back[n] = tail_at[k - 1] if k else -1
        if k == len(tails):
            tails.append(f)
            tail_at.append(n)
        else:
            tails[k], tail_at[k] = f, n
    chain, n = [], tail_at[-1] if tail_at else -1
    while n >= 0:
        chain.append(pairs[n])
        n = back[n]
    return [(f, h) for h, f in reversed(chain)]


def _matched(feed: list[str], heard: list[str]) -> list[tuple[int, int]]:
    """Every (feed index, heard index) of words that line up: the anchors' words, and between anchors what a
    word-by-word comparison of the two finds."""
    out: list[tuple[int, int]] = []
    prev_f = prev_h = 0
    for f, h in [*_anchors(feed, heard), (len(feed), len(heard))]:
        if f >= prev_f and h >= prev_h:
            between = difflib.SequenceMatcher(None, feed[prev_f:f], heard[prev_h:h], autojunk=False)
            for a, b, size in between.get_matching_blocks():
                out += [(prev_f + a + k, prev_h + b + k) for k in range(size)]
        if f < len(feed):
            out += [(f + k, h + k) for k in range(GRAM) if f + k >= prev_f and h + k >= prev_h]
            prev_f, prev_h = f + GRAM, h + GRAM
    return sorted(set(out), key=lambda p: p[1])


def align(feed: list[Word], heard: list[Word], duration: float) -> Fit | None:
    """Line the feed's transcript (`feed`, in its own times) up with what the transcriber heard in the download
    (`heard`, `duration` long). None when they hardly line up."""
    if not feed or not heard:
        return None
    pairs = _matched([w for *_, w in feed], [w for *_, w in heard])
    if len({f for f, _ in pairs}) < MIN_FIT * len(feed):
        return None
    fit = Fit(coverage=round(len({f for f, _ in pairs}) / len(feed), 3))
    # Each stretch between two lined-up words, with the episode's edges as ends.
    ends = [(-1, -1), *pairs, (len(feed), len(heard))]
    for (f1, h1), (f2, h2) in zip(ends, ends[1:], strict=False):
        start = heard[h1][1] if h1 >= 0 else 0.0
        end = heard[h2][0] if h2 < len(heard) else duration
        if end - start < MIN_SEC:
            continue
        heard_words, feed_words = h2 - h1 - 1, f2 - f1 - 1
        # Past its last word, a transcript can't say how long the audio runs on (music, say): words only there.
        feed_start = feed[f1][1] if f1 >= 0 else 0.0
        by_time = f2 < len(feed) and (end - start) - max(0.0, feed[f2][0] - feed_start) >= MIN_SEC
        by_words = heard_words >= MIN_WORDS and feed_words <= STRAY * heard_words
        if by_time or by_words:
            fit.missing.append((round(start, 2), round(end, 2)))
    for _, h in pairs:
        a, b = heard[h][0], heard[h][1]
        if fit.spoken and a - fit.spoken[-1][1] <= SPOKEN_JOIN:
            fit.spoken[-1] = (fit.spoken[-1][0], b)
        else:
            fit.spoken.append((a, b))
    return fit


# ---- for an episode -------------------------------------------------------------------

def enabled(db: sqlite3.Connection, settings: Settings, podcast_id: int | None) -> bool:
    """Whether the show's downloads are checked: its setting, or when unset, whether the transcriber is set and
    the show's feed has transcripts."""
    row = db.execute("SELECT transcript_check FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    if row is None:
        return False
    if row["transcript_check"] is not None:
        return bool(row["transcript_check"]) and bool(settings.whisper_url)
    return bool(settings.whisper_url) and has_feed_transcripts(db, podcast_id)


def has_feed_transcripts(db: sqlite3.Connection, podcast_id: int | None) -> bool:
    """Whether any of the show's episodes came with a transcript from its feed."""
    return db.execute("SELECT 1 FROM episodes e, json_each(e.transcripts) t WHERE e.podcast_id = ? "
                      "AND json_extract(t.value, '$.made') IS NULL LIMIT 1", (podcast_id,)).fetchone() is not None


def feed_transcript(ep: sqlite3.Row) -> dict | None:
    """The episode's transcript from its feed, if it has one."""
    return next((t for t in chapters.transcripts(ep) if not t.get("made")), None)


def needs_words(ep: sqlite3.Row) -> bool:
    """Whether the episode has a feed transcript and nothing heard to check it against yet."""
    t = feed_transcript(ep)
    return t is not None and not t.get("words")


def fit(db: sqlite3.Connection, settings: Settings, ep: sqlite3.Row) -> Fit | None:
    """How the episode's feed transcript lines up with what was heard in its download; None when the check is
    off for the show, or there is nothing to compare."""
    t = feed_transcript(ep)
    if t is None or not t.get("words") or not ep["duration"] or not enabled(db, settings, ep["podcast_id"]):
        return None
    return align(phrases.feed_words(ep), phrases.words(ep), ep["duration"])


def cuts(found: Fit | None) -> list[dict]:
    """The stretches to cut, as saved on the episode."""
    return [{"type": MISSING, "start": a, "end": b} for a, b in (found.missing if found else [])]


def spoken(found: Fit | None) -> list[tuple[float, float]]:
    """Where the show itself plays, for telling false breaks; empty unless the transcript leaves ads out."""
    return found.spoken if found and found.missing else []


def saved(ep: sqlite3.Row) -> list[dict]:
    return json.loads(ep["missing_cuts"] or "[]")


def downloaded(db: sqlite3.Connection, podcast_id: int) -> list[int]:
    """The show's episodes with a download, to check once the check is turned on."""
    return [r["id"] for r in db.execute("SELECT id FROM episodes WHERE podcast_id = ? AND original_path IS NOT NULL",
                                        (podcast_id,))]


def requeue(db: sqlite3.Connection, podcast_id: int, user_id: int | None, note: str) -> int:
    """Queue the show's ready episodes that have been checked to be cut again. Returns how many were queued."""
    rows = db.execute("SELECT * FROM episodes WHERE podcast_id = ? AND state = ?", (podcast_id, READY)).fetchall()
    ids = [ep["id"] for ep in rows if (feed_transcript(ep) or {}).get("words")]
    return len(enqueue(db, "process", ids, user_id, skip_ready=False, payload=json.dumps({"recut": True}),
                       note=note))


def save_setting(db: sqlite3.Connection, podcast_id: int, on: bool) -> bool:
    """Turn the check on or off for a show. Returns whether that changed it."""
    with transaction(db):
        row = db.execute("SELECT transcript_check FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
        db.execute("UPDATE podcasts SET transcript_check = ? WHERE id = ?", (int(on), podcast_id))
    return row is not None and row["transcript_check"] != int(on)
