"""Intro and outro phrases: words a show says at the edges of its talk, cut along with everything past them.

Each podcast can list phrases that end its intro ("let's get into it") and
phrases that begin its outro ("that's all for this week"). Once an episode
has a timed transcript (its feed's, or captions from the transcription
service), it is searched for them:

- an intro phrase heard in the first third cuts from the start to the
  phrase's last word, at its first hearing;
- an outro phrase heard in the last third cuts from the phrase's first word
  to the end, at its last hearing.

Hearings less than CHAIN seconds apart count as one, so a chant sung over
and over is cut whole, and so is a host saying the line twice. A phrase
said again mid-episode cuts nothing. Matching ignores case and
punctuation ("Let's get into it!" is "lets get into it"), word for word.

A phrase between slashes is a regular expression instead, matched against
the same words: lower case, without punctuation, one space apart. It
matches whole words only, and at most MAX_WORDS of them at once, so
`/youth of (the|a) nation/` matches "the youth of the nation" but not
"youth of the nations".

Captions keep whisper's time for each word (`words.json` beside them), so
cuts land between words; a feed's transcript only times whole cues, so
there each word's time is guessed from where it falls in its cue.

What is found is saved on the episode (`episodes.phrase_cuts`) each time it
is cut, as [{type: "intro" | "outro", start, end, phrase}], and cut with
the repeats. Captions come after the first cut, so finding a phrase in them
queues the episode to be cut again; so does changing the phrases.
"""

from __future__ import annotations

import bisect
import functools
import json
import re
import sqlite3
from pathlib import Path

from . import chapters
from .db import transaction
from .jobs import enqueue
from .states import READY

MAX_PHRASES = 20  # of each kind, per podcast
MAX_CHARS = 100  # in a phrase
MAX_WORDS = 30  # a regular expression's match spans at most this many words
REACH = 1 / 3  # of the episode, from its edge, where a phrase counts
CHAIN = 20.0  # seconds between two hearings of a phrase that still make one run of it, as in a chant
PAD = 0.25  # seconds past a phrase's edge also cut, when the next word isn't nearer: whisper's times run tight
INTRO, OUTRO = "intro", "outro"


class PhraseError(ValueError):
    """Phrases that can't be saved; the message says why, for the page."""


def tokens(text: str) -> list[str]:
    """Words to match: lower case, without punctuation, "let's" as "lets"."""
    return re.findall(r"\w+", re.sub(r"['’ʼ]", "", text.casefold()))


@functools.lru_cache(maxsize=256)
def pattern(phrase: str) -> re.Pattern | None:
    """A phrase between slashes as the regular expression it matches the words with; None for a plain phrase.
    Raises re.error for one that isn't valid."""
    if len(phrase) < 3 or not (phrase.startswith("/") and phrase.endswith("/")):
        return None
    return re.compile(rf"(?<!\S)(?:{phrase[1:-1]})(?!\S)", re.IGNORECASE)


def parse(text: str) -> list[str]:
    """Phrases from a box of them, one a line. Blank lines and repeats go."""
    found: dict[tuple, str] = {}
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        if len(line) > MAX_CHARS:
            raise PhraseError(f"Phrases can be at most {MAX_CHARS} characters.")
        try:
            compiled = pattern(line)
        except re.error as e:
            raise PhraseError(f"“{line}” isn't a valid regular expression: {e}.") from None
        if compiled is not None:
            if compiled.search("") or compiled.search(" "):
                raise PhraseError(f"“{line}” matches nothing at all; it needs a word.")
            found.setdefault(("/", line), line)
            continue
        if not tokens(line):
            raise PhraseError(f"“{line}” has no words to listen for.")
        found.setdefault(tuple(tokens(line)), line)
    if len(found) > MAX_PHRASES:
        raise PhraseError(f"At most {MAX_PHRASES} phrases of each kind.")
    return list(found.values())


def of_podcast(db: sqlite3.Connection, podcast_id: int | None) -> tuple[list[str], list[str]]:
    row = db.execute("SELECT intro_phrases, outro_phrases FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    if row is None:
        return [], []
    return json.loads(row["intro_phrases"] or "[]"), json.loads(row["outro_phrases"] or "[]")


# ---- the words of a transcript, with their times -----------------------------------

def _from_cue(start: float, end: float, text: str) -> list[tuple[float, float, str]]:
    """A cue's words, each given a share of its time by where it falls in the text."""
    text = chapters._unescape(re.sub(r"<[^>]*>", "", text))
    spans = [(m.start(), m.end(), w) for m in re.finditer(r"\S+", text) for w in tokens(m[0])]
    if not spans or end <= start:
        return []
    size = max(1, len(text.rstrip()))
    return [(start + (end - start) * a / size, start + (end - start) * b / size, w) for a, b, w in spans]


def _cue_words(text: str) -> list[tuple[float, float, str]]:
    out = []
    for block in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip()):
        lines = block.split("\n")
        at = next((i for i, line in enumerate(lines) if "-->" in line), None)
        m = chapters.CUE.match(lines[at]) if at is not None else None
        start, end = (chapters._clock(m[1]), chapters._clock(m[2])) if m else (None, None)
        if start is not None and end is not None:
            out += _from_cue(start, end, " ".join(lines[at + 1:]))
    return out


def _json_words(text: str) -> list[tuple[float, float, str]]:
    segments = [s for s in json.loads(text).get("segments", [])
                if isinstance(s, dict) and isinstance(s.get("startTime"), (int, float))]
    out = []
    for i, seg in enumerate(segments):
        nxt = segments[i + 1]["startTime"] if i + 1 < len(segments) else seg["startTime"] + 5
        end = seg["endTime"] if isinstance(seg.get("endTime"), (int, float)) else nxt
        out += _from_cue(seg["startTime"], end, str(seg.get("body") or ""))
    return out


def words(ep: sqlite3.Row) -> list[tuple[float, float, str]]:
    """The episode's words in the uncut audio, in order, as (start, end, word) with `word` as `tokens` gives it:
    whisper's own times when its captions kept them, else guessed from a transcript's cues. Empty without one."""
    saved = chapters.transcripts(ep)
    for t in saved:
        if t.get("words") and Path(t["words"]).exists():
            return [(a, b, w) for a, b, word in json.loads(Path(t["words"]).read_text()) for w in tokens(word)]
    for fmt in ("vtt", "srt", "json"):
        if any(t["format"] == fmt for t in saved):
            text = chapters._read(ep, fmt)
            try:
                return _json_words(text) if fmt == "json" else _cue_words(text)
            except (ValueError, AttributeError):
                return []
    return []


# ---- finding them ----------------------------------------------------------------

def _hits(said: list[str], phrase: list[str]) -> list[int]:
    n = len(phrase)
    return [i for i in range(len(said) - n + 1) if said[i:i + n] == phrase]


def _regex_hits(said: list[str], compiled: re.Pattern) -> list[tuple[int, int]]:
    """Where a regular expression matches the words, as (index of the first word, of the last)."""
    text = " ".join(said)
    starts = [0]
    for w in said[:-1]:
        starts.append(starts[-1] + len(w) + 1)
    found = []
    for m in compiled.finditer(text):
        if m.end() <= m.start():
            continue
        first = bisect.bisect_right(starts, m.start()) - 1
        last = bisect.bisect_right(starts, m.end() - 1) - 1
        if last - first < MAX_WORDS:
            found.append((first, last))
    return found


def _all_hits(said: list[str], phrases: list[str]) -> list[tuple[int, int, str]]:
    """Every hearing of any of the phrases, in order, as (index of its first word, of its last, phrase)."""
    found = []
    for p in phrases:
        try:
            compiled = pattern(p)
        except re.error:
            continue
        if compiled is not None:
            found += [(a, b, p) for a, b in _regex_hits(said, compiled)]
        else:
            found += [(i, i + len(tokens(p)) - 1, p) for i in _hits(said, tokens(p))]
    return sorted(found)


def find(heard: list[tuple[float, float, str]], duration: float, intro: list[str],
         outro: list[str]) -> list[dict]:
    """The cuts the phrases make: at most one intro and one outro, as {type, start, end, phrase}."""
    if not heard or not duration:
        return []
    said = [w for _, _, w in heard]
    found = []
    hits = [h for h in _all_hits(said, intro) if heard[h[1]][1] <= duration * REACH]
    if hits:
        at, phrase = hits[0][1], hits[0][2]
        for first, last, _ in hits[1:]:  # said again straight after: a chant, or a host repeating it
            if heard[first][0] - heard[at][1] > CHAIN:
                break
            at = max(at, last)
        nxt = heard[at + 1][0] if at + 1 < len(heard) else duration
        end = min(max(heard[at][1], min(heard[at][1] + PAD, nxt)), duration)
        found.append({"type": INTRO, "start": 0.0, "end": round(end, 2), "phrase": phrase})
    hits = [h for h in _all_hits(said, outro) if heard[h[0]][0] >= duration * (1 - REACH)]
    if hits:
        at, phrase = hits[-1][0], hits[-1][2]
        for first, last, said_as in reversed(hits[:-1]):
            if heard[at][0] - heard[last][1] > CHAIN:
                break
            at, phrase = min(at, first), said_as
        prev = heard[at - 1][1] if at > 0 else 0.0
        start = max(min(heard[at][0], max(heard[at][0] - PAD, prev)), 0.0)
        if not found or start > found[0]["end"]:
            found.append({"type": OUTRO, "start": round(start, 2), "end": round(duration, 2), "phrase": phrase})
    return found


def for_episode(db: sqlite3.Connection, ep: sqlite3.Row) -> list[dict]:
    """The cuts the podcast's phrases make in this episode, as `find` gives them."""
    intro, outro = of_podcast(db, ep["podcast_id"])
    if not (intro or outro) or not ep["duration"]:
        return []
    return find(words(ep), ep["duration"], intro, outro)


def saved(ep: sqlite3.Row) -> list[dict]:
    return json.loads(ep["phrase_cuts"] or "[]")


# ---- when the phrases change ------------------------------------------------------

def save(db: sqlite3.Connection, podcast_id: int, intro: list[str], outro: list[str], user_id: int | None) -> int:
    """Save a podcast's phrases, and queue its ready episodes to be cut again where they now cut differently.
    Returns how many were queued."""
    with transaction(db):
        db.execute("UPDATE podcasts SET intro_phrases = ?, outro_phrases = ? WHERE id = ?",
                   (json.dumps(intro), json.dumps(outro), podcast_id))
    return requeue(db, podcast_id, user_id, note="Waiting to be cut again: the intro and outro phrases changed")


def requeue(db: sqlite3.Connection, podcast_id: int, user_id: int | None, episode_ids: list[int] | None = None,
            note: str | None = None) -> int:
    """Queue the podcast's ready episodes (or these of them) whose phrase cuts are no longer what they were cut
    with. Returns how many were queued."""
    rows = db.execute("SELECT * FROM episodes WHERE podcast_id = ? AND state = ? AND transcripts IS NOT NULL "
                      "AND transcripts != '[]'", (podcast_id, READY)).fetchall()
    want = set(episode_ids) if episode_ids is not None else None
    stale = [ep["id"] for ep in rows
             if (want is None or ep["id"] in want) and for_episode(db, ep) != saved(ep)]
    return len(enqueue(db, "process", stale, user_id, skip_ready=False, payload=json.dumps({"recut": True}),
                       note=note))
