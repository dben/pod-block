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

    assert classify_bumpers(store) == {jingle_in: BUMPER_IN, jingle_out: BUMPER_OUT}


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

    assert [(b.start, b.end, b.opened_by, b.closed_by) for b in breaks] == [
        (100, 244, 1, 3),
        (900, 904, None, 3),
        (2000, 2100, None, None),
    ]
