"""Spot bumpers (short clips that open or close an ad break) and the breaks they mark.

In each episode, occurrences of known blocks that sit close together form a
group, such as a run of ads. A short block that keeps turning up at the front
of such groups is a bumper-in; one that keeps closing them is a bumper-out.
Once bumpers are known, a break runs from a bumper-in to the next bumper-out,
even when the ads in between are new and match nothing in the store.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .store import BUMPER, BUMPER_IN, BUMPER_OUT, SegmentMatch, Store

SHORT_BLOCK_SEC = 10.0  # bumpers usually match for less than this; ads for longer
MAX_BUMPER_SEC = 20.0  # stored blocks longer than this are never bumpers
MIN_PLAIN_BREAK_SEC = 60.0  # a group of known blocks this long is a break even without bumpers
NEW_ADS_GAP_SEC = 90.0  # a half-found break joins known blocks this close: new ads in between
CONFIRM_EDGES = 6  # a bumper seen at a break edge this often...
CONFIRM_EPISODES = 3  # ...in this many episodes keeps its tag from then on
JOIN_GAP_SEC = 8.0  # blocks this close together belong to one group
MAX_BREAK_SEC = 300.0  # a bumper-in and bumper-out further apart do not pair up (podcasts.max_break)
MIN_EDGE_SHARE = 0.5  # share of a block's occurrences that must sit at a group edge
STING_PAIR_SEC = 15.0  # a bumper-out with a bumper-in this soon after it leads into a break

OPENS = {BUMPER_IN, BUMPER}
CLOSES = {BUMPER_OUT, BUMPER}
BUMPER_TYPES = {BUMPER_IN, BUMPER_OUT, BUMPER}


@dataclass
class AdBreak:
    start: float
    end: float
    opened_by: int | None = None  # bumper block ids, if found
    closed_by: int | None = None
    blocks: list[int] = field(default_factory=list)  # every block seen inside

    @property
    def complete(self) -> bool:
        """Both bumpers were found."""
        return self.opened_by is not None and self.closed_by is not None

    @property
    def length(self) -> float:
        return self.end - self.start


def _groups(matches: list[SegmentMatch], gap: float = JOIN_GAP_SEC) -> list[list[SegmentMatch]]:
    groups: list[list[SegmentMatch]] = []
    for m in sorted(matches, key=lambda m: m.start):
        if groups and m.start - max(g.end for g in groups[-1]) <= gap:
            groups[-1].append(m)
        else:
            groups.append([m])
    return groups


@dataclass
class BumperTags:
    kinds: dict[int, str]  # block id -> bumper type
    confirmed: set[int]  # blocks with enough evidence to keep their tag for good


def classify_bumpers(store: Store) -> BumperTags:
    """Tag blocks whose matches are short and usually open or close a group of longer ones.

    Lengths here are match lengths, not block lengths: a long stored block can
    be music the show only ever plays in snippets. A tag is confirmed once the
    block has sat at a group edge CONFIRM_EDGES times in CONFIRM_EPISODES
    episodes; bumpers rarely change, so confirmed tags are kept from then on.
    """
    lengths: dict[int, list[float]] = {}
    opening, closing = Counter(), Counter()
    edge_episodes: dict[int, set[int]] = {}

    for ep_id in store.episodes_with_occurrences():
        matches = store.occurrences_for_episode(ep_id)
        for m in matches:
            lengths.setdefault(m.segment_id, []).append(m.end - m.start)
        for group in _groups(matches):
            long_at = [i for i, m in enumerate(group) if m.end - m.start >= SHORT_BLOCK_SEC]
            if not long_at:
                continue
            for m in group[:long_at[0]]:
                opening[m.segment_id] += 1
                edge_episodes.setdefault(m.segment_id, set()).add(ep_id)
            for m in group[long_at[-1] + 1:]:
                closing[m.segment_id] += 1
                edge_episodes.setdefault(m.segment_id, set()).add(ep_id)

    block_length = {r["id"]: r["duration"] for r in store.segment_summary()}
    tags = BumperTags({}, set())
    for seg_id, seen in lengths.items():
        if block_length[seg_id] > MAX_BUMPER_SEC or sorted(seen)[len(seen) // 2] >= SHORT_BLOCK_SEC:
            continue
        at_edge = opening[seg_id] + closing[seg_id]
        if at_edge < 2 or at_edge < MIN_EDGE_SHARE * len(seen):
            continue
        if opening[seg_id] >= 0.8 * at_edge:
            tags.kinds[seg_id] = BUMPER_IN
        elif closing[seg_id] >= 0.8 * at_edge:
            tags.kinds[seg_id] = BUMPER_OUT
        else:
            tags.kinds[seg_id] = BUMPER
        if at_edge >= CONFIRM_EDGES and len(edge_episodes[seg_id]) >= CONFIRM_EPISODES:
            tags.confirmed.add(seg_id)
    return tags


def find_breaks(matches: list[SegmentMatch], kinds: dict[int, str],
                max_break: float = MAX_BREAK_SEC) -> list[AdBreak]:
    """Ad breaks in one piece of audio, from its block matches and the blocks' types.

    A break spans only what plays between its bumpers: hosts often talk over
    a bumper, and that talk belongs to the show. A lone bumper-out with
    nothing known before it gives a break of no length, which still marks
    where one ended.
    """
    matches = sorted(matches, key=lambda m: m.start)
    breaks: list[AdBreak] = []
    open_break: AdBreak | None = None
    group_end = 0.0  # end of the known blocks running on from the opener
    ads_seen = False  # a long known block played since the break opened
    closers_until = float("-inf")  # end of the last run of closing bumpers

    def give_up(b: AdBreak) -> None:
        # Its bumper-out never came. A bumper-in still marks where a break
        # starts, and the break runs on through the blocks right after it;
        # a two-sided bumper on its own says nothing.
        if kinds.get(b.opened_by) == BUMPER_IN:
            b.end = max(b.start, group_end)
            breaks.append(b)

    def open_at(m: SegmentMatch) -> AdBreak:
        nonlocal group_end, ads_seen
        group_end, ads_seen = m.end, False
        return AdBreak(m.end, m.end, opened_by=m.segment_id, blocks=[m.segment_id])

    for i, m in enumerate(matches):
        kind = kinds.get(m.segment_id)
        if kind in CLOSES and m.start - closers_until <= JOIN_GAP_SEC:
            closers_until = max(closers_until, m.end)  # bumper-outs often come in a row
            continue
        if open_break and m.start - open_break.start > max_break:
            give_up(open_break)
            open_break = None
        if open_break:
            gap = m.start - open_break.start
            if kind in OPENS and not ads_seen and m.start - group_end <= JOIN_GAP_SEC:
                # Another opening sting right after the first: the break starts after both.
                open_break.start = group_end = max(open_break.start, m.end)
                open_break.blocks.append(m.segment_id)
                continue
            if kind == BUMPER_IN:
                # A break never holds a second bumper-in, so the first was a
                # false start (the same sting before a notice, say) or its
                # bumper-out was missed. Either way the break starts here.
                give_up(open_break)
                open_break = open_at(m)
                continue
            # A break holds at least one ad, so a closer right on the opener's
            # heels is part of the same sting. Shows that play one sting on
            # both sides of a break leave the direction open; there, only
            # known ads in between show that a pair encloses a break.
            two_sided = kinds.get(open_break.opened_by) == BUMPER and kind == BUMPER
            if kind == BUMPER_OUT and not ads_seen and _opener_follows(matches, i, kinds):
                # Some shows play a closing sting and an opening one back to back
                # going into a break. With no known ads since the break opened,
                # the pair starts a break rather than ending this one: the
                # bumper-in that follows takes over.
                continue
            if kind in CLOSES and two_sided and not ads_seen and gap >= SHORT_BLOCK_SEC:
                open_break = open_at(m)
            elif kind in CLOSES and gap >= SHORT_BLOCK_SEC:
                open_break.end = max(open_break.start, m.start)
                open_break.closed_by = m.segment_id
                open_break.blocks.append(m.segment_id)
                breaks.append(open_break)
                open_break = None
                closers_until = m.end
            elif kind not in BUMPER_TYPES and m.start - group_end <= JOIN_GAP_SEC:
                group_end = max(group_end, m.end)
                open_break.blocks.append(m.segment_id)
            ads_seen = ads_seen or (kind not in BUMPER_TYPES and m.end - m.start >= SHORT_BLOCK_SEC)
            continue
        if kind in OPENS:
            open_break = open_at(m)
        elif kind == BUMPER_OUT:
            # A break ends here, but its start was not found (new ads, no bumper-in).
            breaks.append(AdBreak(m.start, m.start, closed_by=m.segment_id, blocks=[m.segment_id]))
            closers_until = m.end
    if open_break:
        give_up(open_break)

    # Shows without bumpers still play their ads back to back. Such a group
    # also fills in the missing side of a half-found break it overlaps or
    # nearly meets (the gap being new ads). Bumpers at a group's edges are
    # left out of it, as above.
    def reaches(b: AdBreak, start: float, end: float) -> bool:
        if b.start < end and start < b.end:
            return True
        if b.opened_by is not None and b.closed_by is None:
            return 0 <= start - b.end <= NEW_ADS_GAP_SEC
        if b.closed_by is not None and b.opened_by is None:
            return 0 <= b.start - end <= NEW_ADS_GAP_SEC
        return False

    for group in _groups(matches):
        content = [m for m in group if kinds.get(m.segment_id) not in BUMPER_TYPES]
        if not any(m.end - m.start >= SHORT_BLOCK_SEC for m in content):
            continue  # no ads in it, just stings and snippets
        start, end = min(m.start for m in content), max(m.end for m in content)
        overlapping = [b for b in breaks if reaches(b, start, end)]
        if any(b.complete for b in overlapping):
            continue
        if overlapping:
            for b in overlapping:
                if b.opened_by is None:
                    b.start = min(b.start, start)
                if b.closed_by is None:
                    b.end = max(b.end, end)
        elif _covered(content) >= MIN_PLAIN_BREAK_SEC:
            breaks.append(AdBreak(start, end, blocks=[m.segment_id for m in content]))

    return _merge_overlapping(breaks)


def _opener_follows(matches: list[SegmentMatch], i: int, kinds: dict[int, str]) -> bool:
    """Whether a bumper-in starts within STING_PAIR_SEC of where matches[i] ends."""
    end = matches[i].end
    for n in matches[i + 1:]:
        if n.start - end > STING_PAIR_SEC:
            return False
        if kinds.get(n.segment_id) == BUMPER_IN:
            return True
    return False


def _merge_overlapping(breaks: list[AdBreak]) -> list[AdBreak]:
    merged: list[AdBreak] = []
    for b in sorted(breaks, key=lambda b: b.start):
        last = merged[-1] if merged else None
        if last and b.start < last.end:
            last.end = max(last.end, b.end)
            last.opened_by = last.opened_by or b.opened_by
            last.closed_by = b.closed_by or last.closed_by
            last.blocks += b.blocks
        else:
            merged.append(b)
    return merged


def _covered(matches: list[SegmentMatch]) -> float:
    """Seconds covered by at least one match."""
    total, cursor = 0.0, float("-inf")
    for m in sorted(matches, key=lambda m: m.start):
        if m.end > cursor:
            total += m.end - max(m.start, cursor)
            cursor = m.end
    return total
