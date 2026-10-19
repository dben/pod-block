"""Turn the block matches in one episode into a list of repeated sections.

Every match of a stored block is a part, except bumpers: hosts often talk over
them. Ad breaks found from bumpers are parts too, covering what plays between
the bumpers (new ads included), and so is an outro read over a music bed (see
outro.py). The first intro in the first
half of an episode is stretched back to the start, and the last outro in the
second half forward to the end, so whatever plays before the intro or after
the outro (patron thank-yous, trailers) is included. Parts that touch, or sit
no more than `fill_gaps` seconds apart, merge into one section.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .breaks import BUMPER_TYPES, find_breaks
from .store import INTRO, OUTRO, REPEATED, TYPES, SegmentMatch

BREAK = "break"
JOIN_SEC = 2.0  # parts this close always merge
ORDER = [INTRO, OUTRO, BREAK, *TYPES]  # how a section's types are listed


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
                   fill_gaps: float = 0.0, outro_bed_start: float | None = None) -> list[Section]:
    """`outro_bed_start` is where a talked-over music-bed outro begins, if one was found."""
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
    parts += [Part(b.start, b.end, BREAK) for b in find_breaks(matches, types)
              if (b.opened_by is not None or b.closed_by is not None) and b.length > 0]

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
    return sections
