"""Turn the block matches in one episode into a list of repeated sections.

Every match of a stored block is a part, except bumpers: hosts often talk over
them. Ad breaks found from bumpers are parts too, covering what plays between
the bumpers (new ads included), and so is an outro read over a music bed (see
outro.py). The first intro in the first
half of an episode is stretched back to the start, and the last outro in the
second half forward to the end, so whatever plays before the intro or after
the outro (patron thank-yous, trailers) is included. Parts that touch, or sit
no more than `fill_gaps` seconds apart, merge into one section.

Sections shorter than `min_section` are dropped: a few seconds that happen to
match another episode (a laugh, a catchphrase, a sting) are rarely worth
cutting. Ad breaks, intros, outros, show blocks and blocks typed by hand
(`pinned`) are kept whatever their length. Short blocks stay in the store
all the same: bumpers are short, and they are how breaks are found.

Blocks kept (`kept`, for one podcast) or flagged as false positives
(`ignored`) are never cut and mark no breaks; `skipped` lists where they play.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .breaks import BUMPER_TYPES, MAX_BREAK_SEC, find_breaks
from .store import IGNORED, INTRO, KEPT, OUTRO, REPEATED, SHOW, TYPES, SegmentMatch

BREAK = "break"
JOIN_SEC = 2.0  # parts this close always merge
SPOKEN_SHARE = 0.5  # a break found from bumpers that is this much the show itself is no break
MIN_SECTION_SEC = 15.0  # shorter sections are left in, unless they are breaks or typed by hand
ORDER = [INTRO, OUTRO, BREAK, *TYPES]  # how a section's types are listed
SKIPPED = (KEPT, IGNORED)  # never cut


@dataclass
class Part:
    start: float
    end: float
    type: str  # the block's type, or "break"
    block: int | None = None  # None for a break


@dataclass
class Section:
    start: float
    end: float
    parts: list[Part] = field(default_factory=list)
    unique_sec: float = 0.0  # audio taken in by fill_gaps

    @property
    def length(self) -> float:
        return self.end - self.start

    @property
    def types(self) -> list[str]:
        present = {p.type for p in self.parts}
        return [t for t in dict.fromkeys(ORDER) if t in present]

    @property
    def blocks(self) -> list[int]:
        return sorted({p.block for p in self.parts if p.block is not None})

    def to_dict(self) -> dict:
        return {"start": round(self.start, 2), "end": round(self.end, 2),
                "length": round(self.length, 2), "types": self.types, "blocks": self.blocks,
                "unique_sec": round(self.unique_sec, 2),
                "parts": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in asdict(p).items()}
                          for p in self.parts]}


def build_sections(matches: list[SegmentMatch], types: dict[int, str], duration: float,
                   fill_gaps: float = 0.0, outro_bed_start: float | None = None,
                   min_section: float = MIN_SECTION_SEC, pinned: set[int] = frozenset(),
                   max_break: float = MAX_BREAK_SEC, spoken: list[tuple[float, float]] = ()) -> list[Section]:
    """`outro_bed_start` is where a talked-over music-bed outro begins, if one was found.
    `pinned` blocks (typed by hand) make a section stay however short it is. A bumper-in and bumper-out more
    than `max_break` seconds apart do not make a break, and neither do bumpers around what is mostly `spoken`:
    the show itself, as its transcript has it (app/missing.py)."""
    matches = [m for m in matches if types.get(m.segment_id) not in SKIPPED]
    # Bumpers only mark breaks; hosts often talk over them, so they are left out.
    parts = [Part(m.start, m.end, types.get(m.segment_id, REPEATED), m.segment_id) for m in matches
             if types.get(m.segment_id) not in BUMPER_TYPES]
    if outro_bed_start is not None:
        parts.append(Part(outro_bed_start, duration, OUTRO))

    intros = [p for p in parts if p.type == INTRO and p.start < duration / 2]
    if intros:
        min(intros, key=lambda p: p.start).start = 0.0
    outros = [p for p in parts if p.type == OUTRO and p.end > duration / 2]
    if outros:
        max(outros, key=lambda p: p.end).end = duration

    # Breaks without bumpers are only their known blocks, which are parts already.
    parts += [Part(b.start, b.end, BREAK) for b in find_breaks(matches, types, max_break)
              if (b.opened_by is not None or b.closed_by is not None) and b.length > 0
              and _overlap(b.start, b.end, spoken) < SPOKEN_SHARE * b.length]

    sections: list[Section] = []
    reach = max(JOIN_SEC, fill_gaps)
    for p in sorted(parts, key=lambda p: p.start):
        last = sections[-1] if sections else None
        if last and p.start - last.end <= reach:
            gap = p.start - last.end
            if gap > JOIN_SEC:
                last.unique_sec += gap
            last.end = max(last.end, p.end)
            last.parts.append(p)
        else:
            sections.append(Section(p.start, p.end, [p]))
    keeps = {INTRO, OUTRO, SHOW, BREAK}
    return [s for s in sections if s.length >= min_section
            or any(p.type in keeps or p.block in pinned for p in s.parts)]


def _overlap(start: float, end: float, spans) -> float:
    """Seconds of start-end that the spans cover."""
    return sum(max(0.0, min(end, b) - max(start, a)) for a, b in spans)


def skipped(matches: list[SegmentMatch], types: dict[int, str], kind: str,
            sections: list[Section] = ()) -> list[dict]:
    """Where blocks of a SKIPPED `kind` play, as spans (touching matches joined) with their blocks; leaving out
    spans that `sections` cut anyway."""
    spans: list[dict] = []
    for m in sorted((m for m in matches if types.get(m.segment_id) == kind), key=lambda m: m.start):
        if spans and m.start - spans[-1]["end"] <= JOIN_SEC:
            spans[-1]["end"] = max(spans[-1]["end"], m.end)
            spans[-1]["blocks"] = sorted({*spans[-1]["blocks"], m.segment_id})
        else:
            spans.append({"start": m.start, "end": m.end, "blocks": [m.segment_id]})
    return [{"start": round(sp["start"], 2), "end": round(sp["end"], 2), "blocks": sp["blocks"]} for sp in spans
            if not any(s.start <= sp["start"] and sp["end"] <= s.end for s in sections)]
