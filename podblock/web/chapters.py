"""Chapters and transcripts that fit the cut audio.

Cutting moves everything after a cut earlier, so the source's chapter and
transcript times no longer fit. When an episode is first cut, its chapters
are read (the feed's `podcast:chapters` JSON, else its inline `psc:chapters`,
else the ones in the audio file) and its timed transcripts (`podcast:transcript`
as WebVTT, SRT or JSON) are saved under the media folder. Times are then
moved onto the cut audio:

- a time in a kept stretch moves back by everything cut before it;
- a time inside a cut moves to the join the cut left;
- a chapter or cue left with nothing (it was all inside a cut, as sponsor
  chapters often are) goes.

Each join also gets a chapter, so a listener can see where a cut was made and
what it was: "1:32 ad break cut", or inside one of the source's chapters,
"Interview (after 1:32 ad break cut)". An episode without chapters of its own
gets a "Start" chapter before its first cut.

Chapters are written into the cut file and served as JSON beside it;
transcripts are shifted as they are served.
"""

from __future__ import annotations

import bisect
import itertools
import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from .. import cut
from ..feed import PODCAST, PSC, USER_AGENT

log = logging.getLogger(__name__)

TIMEOUT = 20
MAX_BYTES = 8 << 20  # chapters and transcripts are small; anything this big is something else
MIN_SEC = 0.5  # chapters shorter than this after cutting go, and joins this close to a chapter merge with it
FORMATS = {"text/vtt": "vtt", "application/x-subrip": "srt", "application/srt": "srt", "text/srt": "srt",
           "application/json": "json"}  # transcript types whose times we can shift
MIME = {"vtt": "text/vtt", "srt": "application/x-subrip", "json": "application/json"}
LABELS = [("break", "ad break"), ("intro", "intro"), ("outro", "outro"), ("manual", "hand-marked")]  # else "repeat"


def get(url: str) -> bytes:
    """Fetch a chapters or transcript file."""
    with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        body = bytearray()
        for chunk in resp.iter_content(chunk_size=1 << 16):
            body += chunk
            if len(body) > MAX_BYTES:
                raise ValueError(f"over {MAX_BYTES >> 20} MB")
        return bytes(body)


def _web(url: str | None) -> str | None:
    url = (url or "").strip()
    return url if url.split(":", 1)[0].lower() in {"http", "https"} else None


def _clock(text: str | None) -> float | None:
    """Seconds from `1:02:03.5`, `02:03,5` or `123.5`."""
    if not text or not re.fullmatch(r"\d+(:\d{1,2}){0,2}([.,]\d+)?", text.strip()):
        return None
    secs = 0.0
    for part in text.strip().replace(",", ".").split(":"):
        secs = secs * 60 + float(part)
    return secs


class Shift:
    """Moves times in the source audio onto the cut audio, given the stretches kept, in order."""

    def __init__(self, keep: list[tuple[float, float]]):
        self.keep = [(float(a), float(b)) for a, b in keep]
        self.starts = [a for a, _ in self.keep]
        self.offsets = list(itertools.accumulate((b - a for a, b in self.keep), initial=0.0))

    @property
    def length(self) -> float:
        return self.offsets[-1]

    def __call__(self, t: float) -> float:
        i = bisect.bisect_right(self.starts, t) - 1
        if i < 0:
            return 0.0
        a, b = self.keep[i]
        return self.offsets[i] + t - a if t < b else self.offsets[i + 1]

    def joins(self) -> list[tuple[float, float, float]]:
        """Each cut that has kept audio after it: where it is in the cut audio, and what it took out."""
        found, prev = [], 0.0
        for (a, b), at in zip(self.keep, self.offsets):
            if a - prev > 0.01:
                found.append((at, prev, a))
            prev = b
        return found


# ---- reading the source's ------------------------------------------------------

def _json_chapters(body: bytes) -> list[dict]:
    """Podcasting 2.0 JSON chapters, as {start, end?, title, img?, url?, toc?}."""
    doc = json.loads(body)
    found = []
    for c in doc.get("chapters", []) if isinstance(doc, dict) else []:
        if not isinstance(c, dict) or not isinstance(c.get("startTime"), (int, float)):
            continue
        ch = {"start": float(c["startTime"]), "title": str(c.get("title") or "").strip()}
        if isinstance(c.get("endTime"), (int, float)):
            ch["end"] = float(c["endTime"])
        for key in ("img", "url"):
            if isinstance(c.get(key), str) and _web(c[key]):
                ch[key] = c[key].strip()
        if c.get("toc") is False:
            ch["toc"] = False
        found.append(ch)
    return sorted(found, key=lambda c: c["start"])


def _psc_chapters(item: ET.Element) -> list[dict]:
    """Podlove simple chapters, inline in the <item>."""
    found = []
    for c in item.iter(f"{PSC}chapter"):
        start = _clock(c.get("start"))
        if start is None:
            continue
        ch = {"start": start, "title": (c.get("title") or "").strip()}
        if url := _web(c.get("href")):
            ch["url"] = url
        if img := _web(c.get("image")):
            ch["img"] = img
        found.append(ch)
    return sorted(found, key=lambda c: c["start"])


def _transcript_ok(fmt: str, body: bytes) -> bool:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    if fmt == "vtt":
        return text.lstrip().startswith("WEBVTT")
    if fmt == "srt":
        return "-->" in text
    try:
        return isinstance(json.loads(text).get("segments"), list)
    except (ValueError, AttributeError):
        return False


def gather(db: sqlite3.Connection, ep: sqlite3.Row, stem: Path) -> None:
    """Find an episode's chapters and save its timed transcripts (as `stem` plus a suffix), the first time it is
    cut. A chapters or transcript file that can't be had is left out."""
    if ep["source_chapters"] is not None:
        return
    item = None
    try:
        item = ET.fromstring(ep["item_xml"]) if ep["item_xml"] else None
    except ET.ParseError:
        pass
    chapters, transcripts = [], []
    for el in item.findall(f"{PODCAST}chapters") if item is not None else []:
        if url := _web(el.get("url")):
            try:
                chapters = _json_chapters(get(url))
            except Exception as e:
                log.info("no chapters for episode %d from %s: %s", ep["id"], url, e)
        if chapters:
            break
    if not chapters and item is not None:
        chapters = _psc_chapters(item)
    if not chapters and ep["original_path"] and Path(ep["original_path"]).exists():
        try:
            chapters = cut.chapters(Path(ep["original_path"]))
        except cut.CutError:
            pass
    for el in item.findall(f"{PODCAST}transcript") if item is not None else []:
        fmt = FORMATS.get((el.get("type") or "").split(";")[0].strip().lower())
        url = _web(el.get("url"))
        if not fmt or not url or any(t["format"] == fmt for t in transcripts):
            continue
        try:
            body = get(url)
        except Exception as e:
            log.info("no transcript for episode %d from %s: %s", ep["id"], url, e)
            continue
        if not _transcript_ok(fmt, body):
            continue
        path = stem.with_suffix(f".{fmt}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(body)
        tmp.replace(path)
        transcripts.append({"format": fmt, "path": str(path),
                            **{k: el.get(k) for k in ("language", "rel") if el.get(k)}})
    db.execute("UPDATE episodes SET source_chapters = ?, transcripts = ? WHERE id = ?",
               (json.dumps(chapters), json.dumps(transcripts), ep["id"]))
    db.commit()


def forget(ep: sqlite3.Row) -> None:
    """Delete an episode's saved transcripts, as its download goes."""
    for t in json.loads(ep["transcripts"] or "[]"):
        Path(t["path"]).unlink(missing_ok=True)


# ---- chapters for the cut ------------------------------------------------------

def cuts(report: dict | None, edits: list) -> list[tuple[float, float, set[str]]]:
    """What was cut where: each section of the report with its types, and each stretch cut by hand."""
    found = [(s["start"], s["end"], set(s["types"])) for s in (report or {}).get("sections", [])]
    return found + [(r["start"], r["end"], {"manual"}) for r in edits]


def _length(secs: float) -> str:
    secs = int(round(secs))
    if secs < 60:
        return f"{secs} sec"
    h, rem = divmod(secs, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def build(keep: list[tuple[float, float]], removed: list[tuple[float, float, set[str]]],
          source: list[dict]) -> list[dict]:
    """The chapters of the cut audio: the source's, moved to fit, and one at each join. Each is {start, title},
    with `img`, `url` and `toc` where the source had them."""
    shift = Shift(keep)
    source = sorted(source, key=lambda c: c["start"])
    out = []
    for i, c in enumerate(source):
        end = source[i + 1]["start"] if i + 1 < len(source) else c.get("end", float("inf"))
        start = shift(c["start"])
        if shift(end) - start >= MIN_SEC:
            out.append({**{k: v for k, v in c.items() if k != "end"}, "start": start})
    for at, gone_from, gone_to in shift.joins():
        if at >= shift.length - MIN_SEC:
            continue
        kinds = set().union(*(t for a, b, t in removed if a < gone_to - 0.01 and b > gone_from + 0.01))
        what = f"{_length(gone_to - gone_from)} {next((name for k, name in LABELS if k in kinds), 'repeat')} cut"
        playing = next((c for c in reversed(source) if c["start"] <= gone_to + 0.01), None)
        title = f"{playing['title']} (after {what})" if playing and playing["title"] else what
        same = next((c for c in out if abs(c["start"] - at) < MIN_SEC), None)
        if same is not None:
            same["title"] = title
        else:
            out.append({"start": at, "title": title,
                        **{k: playing[k] for k in ("img", "url") if playing and k in playing}})
    out.sort(key=lambda c: c["start"])
    if out and not source and out[0]["start"] >= MIN_SEC:
        out.insert(0, {"start": 0.0, "title": "Start"})
    for c in out:
        c["start"] = round(c["start"], 3)
    return out


def keep_of(ep: sqlite3.Row) -> list[tuple[float, float]]:
    return [tuple(span) for span in json.loads(ep["cut_spec"] or "[]")] or [(0.0, ep["cut_duration"] or 0.0)]


def for_episode(db: sqlite3.Connection, ep: sqlite3.Row) -> list[dict]:
    """The chapters of an episode's cut file: as written into it, or, for files cut before chapters were,
    worked out from its report."""
    if ep["cut_chapters"] is not None:
        return json.loads(ep["cut_chapters"])
    saved = db.execute("SELECT report FROM reports WHERE episode_id = ?", (ep["id"],)).fetchone()
    edits = db.execute("SELECT start, end FROM edits WHERE episode_id = ?", (ep["id"],)).fetchall()
    return build(keep_of(ep), cuts(json.loads(saved["report"]) if saved else None, edits),
                 json.loads(ep["source_chapters"] or "[]"))


def chapters_json(chapters: list[dict]) -> bytes:
    """Chapters as Podcasting 2.0 JSON chapters."""
    out = []
    for c in chapters:
        ch = {"startTime": c["start"], "title": c["title"]}
        ch.update({k: c[k] for k in ("img", "url", "toc") if k in c})
        out.append(ch)
    return json.dumps({"version": "1.2.0", "chapters": out}, ensure_ascii=False).encode()


def has_chapters(ep: sqlite3.Row) -> bool:
    """Whether an episode's cut has chapters, without working them out: it has some of its own, or a join."""
    if ep["cut_chapters"] is not None:
        return ep["cut_chapters"] != "[]"
    return bool(json.loads(ep["source_chapters"] or "[]")) or bool(Shift(keep_of(ep)).joins())


# ---- transcripts ----------------------------------------------------------------

CUE = re.compile(r"^\s*(\S+)\s*-->\s*(\S+)(.*)$")
INLINE = re.compile(r"<((?:\d+:)?\d{1,2}:\d{2}\.\d{1,3})>")  # WebVTT's timestamps inside a cue


def _stamp(secs: float, srt: bool) -> str:
    ms = int(round(secs * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{',' if srt else '.'}{ms:03d}"


def _shift_cues(text: str, shift: Shift, srt: bool) -> str:
    blocks = re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip())
    out, number = [], 0
    for block in blocks:
        lines = block.split("\n")
        at = next((i for i, line in enumerate(lines) if "-->" in line), None)
        m = CUE.match(lines[at]) if at is not None else None
        if at is None or not m:  # the header, notes, styles
            out.append(block)
            continue
        start, end = _clock(m[1]), _clock(m[2])
        if start is None or end is None:
            continue
        start, end = shift(start), shift(end)
        if end - start < 0.01:
            continue
        lines[at] = f"{_stamp(start, srt)} --> {_stamp(end, srt)}{m[3]}"
        if srt:
            number += 1
            lines = [str(number), *lines[at:]]
        else:
            lines = [INLINE.sub(lambda t: f"<{_stamp(shift(_clock(t[1])), False)}>", line) for line in lines]
        out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"


def _shift_json(text: str, shift: Shift) -> str:
    doc = json.loads(text)
    segments = []
    for seg in doc.get("segments", []):
        if not isinstance(seg, dict) or not isinstance(seg.get("startTime"), (int, float)):
            continue
        start = shift(seg["startTime"])
        end = shift(seg["endTime"]) if isinstance(seg.get("endTime"), (int, float)) else None
        if end is not None and end - start < 0.01:
            continue
        segments.append({**seg, "startTime": round(start, 3), **({"endTime": round(end, 3)} if end is not None else {})})
    return json.dumps({**doc, "segments": segments}, ensure_ascii=False)


def transcript(ep: sqlite3.Row, fmt: str) -> str | None:
    """An episode's transcript in one format, with its times moved to fit the cut audio; None if it has none."""
    saved = next((t for t in json.loads(ep["transcripts"] or "[]") if t["format"] == fmt), None)
    if saved is None or not Path(saved["path"]).exists():
        return None
    text = Path(saved["path"]).read_bytes().decode("utf-8-sig", errors="replace")
    shift = Shift(keep_of(ep))
    return _shift_json(text, shift) if fmt == "json" else _shift_cues(text, shift, srt=fmt == "srt")


def transcripts(ep: sqlite3.Row) -> list[dict]:
    """An episode's saved transcripts: format, and the source's language and rel."""
    return [t for t in json.loads(ep["transcripts"] or "[]") if Path(t["path"]).exists()]
