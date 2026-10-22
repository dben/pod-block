"""Who speaks when in an episode, as the transcription service finds it, set beside the repeats found.

An idea to test before anything is cut by it: an ad read by someone who is
not on the show is a voice heard in one short stretch and never again. Each
speaker's talk is summed up, with how much of it falls inside the episode's
repeated sections, and voices heard only briefly and in one stretch are
flagged as lone voices (a few seconds of talk don't count). A lone voice outside every repeat may be an ad the
fingerprints haven't caught yet; one inside a repeat backs up what they found.
A voice heard mostly inside the repeats, wherever they are, is called out too:
on a network show that is often the announcer or a voice the ads share.

The turns are saved with the episode's captions, as JSON:
{"options": {threshold, count, min_on, min_off}, "turns": [[start, end, speaker], ...]}.
`podblock speakers` prints the comparison, and can ask the service again with
other options.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

LONE_SHARE = 0.05  # a lone voice has at most this share of the episode's talk
LONE_SPAN = 180.0  # and all of it within this many seconds
LONE_MIN = 10.0  # but at least this many seconds of it: below that it's a cough, a laugh, or a mistaken turn
MOSTLY_IN_REPEATS = 0.5  # a voice with this share of its talk inside repeats is called out


def save(stem: Path, turns: list, options: dict | None) -> Path:
    path = stem.with_name(stem.name + ".speakers.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps({"options": options, "turns": turns}, separators=(",", ":")))
    tmp.replace(path)
    return path


def saved(ep) -> dict | None:
    """The turns saved with an episode's captions, if any."""
    for t in json.loads(ep["transcripts"] or "[]"):
        if t.get("speakers") and Path(t["speakers"]).exists():
            return json.loads(Path(t["speakers"]).read_text())
    return None


def _overlap(a: float, b: float, spans: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in spans)


def summary(turns: list, sections: list[dict], lone_share: float = LONE_SHARE, lone_span: float = LONE_SPAN,
            lone_min: float = LONE_MIN) -> list[dict]:
    """Each speaker, most talk first: {speaker, talk, share, turns, first, last, in_repeats, lone}. `in_repeats` is
    the share of their talk inside the sections."""
    spans = [(s["start"], s["end"]) for s in sections]
    talk, count, inside = defaultdict(float), defaultdict(int), defaultdict(float)
    first, last = {}, {}
    for a, b, who in turns:
        talk[who] += b - a
        count[who] += 1
        inside[who] += _overlap(a, b, spans)
        first.setdefault(who, a)
        last[who] = b
    total = sum(talk.values()) or 1.0
    out = []
    for who in sorted(talk, key=lambda w: -talk[w]):
        out.append({"speaker": who, "talk": talk[who], "share": talk[who] / total, "turns": count[who],
                    "first": first[who], "last": last[who],
                    "in_repeats": inside[who] / talk[who] if talk[who] else 0.0,
                    "lone": lone_min <= talk[who] and talk[who] / total <= lone_share
                    and last[who] - first[who] <= lone_span})
    return out


def by_section(turns: list, sections: list[dict]) -> list[dict]:
    """Each section, with the share of its talk each speaker has: {start, end, types, speakers: {speaker: share}}."""
    out = []
    for s in sections:
        talk = defaultdict(float)
        for a, b, who in turns:
            talk[who] += _overlap(a, b, [(s["start"], s["end"])])
        total = sum(talk.values())
        out.append({"start": s["start"], "end": s["end"], "types": s.get("types", []),
                    "speakers": {w: t / total for w, t in sorted(talk.items(), key=lambda x: -x[1]) if t > 0}
                    if total else {}})
    return out


def _clock(secs: float) -> str:
    secs = int(round(secs))
    h, rem = divmod(secs, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def report(turns: list, sections: list[dict], options: dict | None = None, **lone) -> str:
    """The comparison, as text for the command line."""
    people = summary(turns, sections, **lone)
    lines = []
    if options:
        lines.append("Options: " + ", ".join(f"{k} {v}" for k, v in options.items()))
    lines += [f"{len(people)} speaker(s), {len(turns)} turns", "",
              "speaker    talk  share  turns     first      last  in repeats"]
    for p in people:
        lines.append(f"S{p['speaker']:<4} {_clock(p['talk']):>8} {p['share']:6.1%} {p['turns']:6d} "
                     f"{_clock(p['first']):>9} {_clock(p['last']):>9} {p['in_repeats']:10.0%}"
                     + ("  lone voice" if p["lone"] else ""))
    lone_ids = {p["speaker"] for p in people if p["lone"]}
    if sections:
        lines += ["", "Repeated sections:"]
        for s in by_section(turns, sections):
            who = ", ".join(f"S{w} {share:.0%}" for w, share in s["speakers"].items()) or "no speech"
            mark = "  <- lone voice" if any(w in lone_ids and share >= 0.5 for w, share in s["speakers"].items()) \
                else ""
            lines.append(f"  {_clock(s['start']):>8} - {_clock(s['end']):>8}  {'/'.join(s['types']) or '-':<10} "
                         f"{who}{mark}")
    mostly = [p for p in people if p["in_repeats"] >= MOSTLY_IN_REPEATS]
    if mostly:
        lines += ["", "Voices mostly inside the repeats (an announcer, or someone reading the ads):"]
        lines += [f"  S{p['speaker']}: {p['in_repeats']:.0%} of {_clock(p['talk'])}" for p in mostly]
    outside = [p for p in people if p["lone"] and p["in_repeats"] < MOSTLY_IN_REPEATS]
    if outside:
        lines += ["", "Lone voices outside the repeats (an ad not yet found, or a guest's aside):"]
        for p in outside:
            stretches = [(a, b) for a, b, w in turns if w == p["speaker"]]
            lines.append(f"  S{p['speaker']}: {_clock(stretches[0][0])} - {_clock(stretches[-1][1])}, "
                         f"{_clock(p['talk'])} of talk in {len(stretches)} turn(s)")
    return "\n".join(lines)
