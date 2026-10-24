import numpy as np

from podblock.breaks import BUMPER_IN, BUMPER_OUT, classify_bumpers, find_breaks
from podblock.fingerprint import Fingerprint
from podblock.store import SegmentMatch, Store


def block(store, length):
    empty = Fingerprint(np.zeros(0, np.uint32), np.zeros(0, np.int32), length)
    return store.add_segment(empty, None, 0, length)


def test_short_blocks_at_group_edges_become_bumpers(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    jingle_in, ad, jingle_out, theme = block(store, 4), block(store, 60), block(store, 3), block(store, 5)
    for n in range(2):
        ep = store.upsert_episode(f"ep{n}", fingerprint_path="unused")
        matches = [SegmentMatch(theme, 0, 5, 0, 50)]  # alone, far from any ads
        for t in (600, 1800):
            matches += [
                SegmentMatch(jingle_in, t, t + 4, 0, 50),
                SegmentMatch(ad, t + 6, t + 66, 0, 500),
                SegmentMatch(jingle_out, t + 68, t + 71, 0, 40),
            ]
        store.replace_occurrences(ep, matches)

    tags = classify_bumpers(store)
    assert tags.kinds == {jingle_in: BUMPER_IN, jingle_out: BUMPER_OUT}
    assert tags.confirmed == set()  # 4 edges in 2 episodes is not enough yet


def test_bumpers_seen_often_enough_stay_tagged(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    jingle_in, ad = block(store, 4), block(store, 60)
    episodes = []
    for n in range(3):
        ep = store.upsert_episode(f"ep{n}", fingerprint_path="unused")
        episodes.append(ep)
        store.replace_occurrences(ep, [m for t in (600, 1800) for m in (
            SegmentMatch(jingle_in, t, t + 4, 0, 50), SegmentMatch(ad, t + 6, t + 66, 0, 500))])

    tags = classify_bumpers(store)
    assert tags.confirmed == {jingle_in}
    store.set_auto_types(tags.kinds, tags.confirmed)

    # Even with its evidence gone, a confirmed bumper keeps its tag.
    for ep in episodes:
        store.replace_occurrences(ep, [])
    store.set_auto_types(classify_bumpers(store).kinds)
    assert store.types()[jingle_in] == BUMPER_IN


def test_breaks_from_bumpers_and_from_plain_groups():
    kinds = {1: BUMPER_IN, 3: BUMPER_OUT}
    matches = [
        SegmentMatch(1, 100, 104, 0, 50),
        SegmentMatch(5, 130, 190, 0, 300),  # an ad; others in this break are unknown
        SegmentMatch(3, 240, 244, 0, 50),
        SegmentMatch(3, 900, 904, 0, 50),  # a lone bumper-out
        SegmentMatch(2, 2000, 2060, 0, 500),  # ads back to back, no bumpers
        SegmentMatch(4, 2061, 2100, 0, 400),
    ]

    breaks = find_breaks(matches, kinds)

    # Breaks run between their bumpers, not over them; a lone bumper-out marks
    # where a break ended, with nothing known before it.
    assert [(b.start, b.end, b.opened_by, b.closed_by) for b in breaks] == [
        (104, 240, 1, 3),
        (900, 900, None, 3),
        (2000, 2100, None, None),
    ]


def test_a_second_bumper_in_starts_the_break_again():
    # The show's bumper-in plays before a short notice, then the episode runs
    # on until the real break, which opens with the same sting.
    kinds = {1: BUMPER_IN, 3: BUMPER_OUT}
    matches = [
        SegmentMatch(1, 100, 104, 0, 50),  # before the notice
        SegmentMatch(1, 280, 284, 0, 50),
        SegmentMatch(5, 290, 350, 0, 300),
        SegmentMatch(3, 380, 384, 0, 50),
    ]

    breaks = [(b.start, b.end) for b in find_breaks(matches, kinds) if b.length > 0]

    assert breaks == [(284, 380)]


def test_bumpers_further_apart_than_the_longest_break_do_not_pair():
    kinds = {1: BUMPER_IN, 3: BUMPER_OUT}
    matches = [
        SegmentMatch(1, 100, 104, 0, 50),  # a lone sting before a notice
        SegmentMatch(5, 330, 390, 0, 300),  # the real break: no bumper-in, then a known ad
        SegmentMatch(3, 400, 404, 0, 50),
    ]

    assert [(b.start, b.end) for b in find_breaks(matches, kinds, max_break=600)] == [(104, 400)]
    # The ad and bumper-out still mark the real break.
    assert [(b.start, b.end) for b in find_breaks(matches, kinds, max_break=240) if b.length > 0] == [(330, 400)]


def test_a_closing_sting_right_before_an_opening_one_leads_into_the_break():
    # From a real episode: a bumper-in before a notice, then 7 minutes of the
    # show, then a bumper-out and bumper-in back to back going into the break.
    kinds = {1: BUMPER_IN, 2: BUMPER_IN, 3: BUMPER_OUT}
    matches = [
        SegmentMatch(1, 1617, 1620, 0, 50),
        SegmentMatch(3, 2039, 2040, 0, 50),
        SegmentMatch(2, 2040, 2041, 0, 50),
        SegmentMatch(5, 2073, 2135, 0, 500),
        SegmentMatch(6, 2136, 2194, 0, 500),
        SegmentMatch(3, 2293, 2294, 0, 50),
    ]

    breaks = [(b.start, b.end) for b in find_breaks(matches, kinds, max_break=600) if b.length > 0]

    assert breaks == [(2041, 2293)]


def test_a_break_with_known_ads_still_ends_at_its_bumper_out():
    kinds = {1: BUMPER_IN, 3: BUMPER_OUT}
    matches = [
        SegmentMatch(1, 100, 104, 0, 50),
        SegmentMatch(5, 110, 170, 0, 500),
        SegmentMatch(3, 200, 204, 0, 50),
        SegmentMatch(1, 206, 210, 0, 50),  # the next break, straight after
        SegmentMatch(6, 215, 275, 0, 500),
        SegmentMatch(3, 300, 304, 0, 50),
    ]

    assert [(b.start, b.end) for b in find_breaks(matches, kinds)] == [(104, 200), (210, 300)]
