import numpy as np
import pytest

from podblock.feed import parse_feed
from podblock.fingerprint import Fingerprint, FingerprintConfig
from podblock.pipeline import neighbours
from podblock.report import BREAK, build_sections
from podblock.store import BUMPER_IN, BUMPER_OUT, INTRO, OUTRO, REPEATED, SegmentMatch, Store, StoreError


def tones(seed, n=400):
    rng = np.random.default_rng(seed)
    return Fingerprint(rng.integers(0, 2**26, n).astype(np.uint32),
                       np.sort(rng.integers(0, 600, n)).astype(np.int32), 20.0)


def test_podcast_only_blocks_stay_with_their_podcast(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    a, b = store.podcast_id("Show A", "http://a/feed"), store.podcast_id("Show B", "http://b/feed")
    ep_a = store.upsert_episode("a1", podcast_id=a)
    intro_fp, promo_fp = tones(1), tones(2)
    intro = store.add_segment(intro_fp, ep_a, 0, 20)
    promo = store.add_segment(promo_fp, ep_a, 100, 120)
    store.set_type(intro, INTRO)

    both = Fingerprint(np.concatenate([intro_fp.hashes, promo_fp.hashes]),
                       np.concatenate([intro_fp.times, promo_fp.times + 2000]), 100.0)
    seen_in_a = {m.segment_id for m in store.scan(both, _params(), podcast_id=a)}
    seen_in_b = {m.segment_id for m in store.scan(both, _params(), podcast_id=b)}

    assert seen_in_a == {intro, promo}
    assert seen_in_b == {promo}


def test_auto_tagging_leaves_hand_set_types_alone(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    x, y = store.add_segment(tones(3), None, 0, 5), store.add_segment(tones(4), None, 0, 5)
    store.set_type(x, OUTRO)

    store.set_auto_types({x: BUMPER_IN, y: BUMPER_OUT})
    assert store.types() == {x: OUTRO, y: BUMPER_OUT}

    store.set_auto_types({})
    assert store.types() == {x: OUTRO, y: REPEATED}


def test_store_refuses_other_fingerprint_settings(tmp_path):
    Store(tmp_path / "db.sqlite", FingerprintConfig(high_band=False)).close()
    assert Store(tmp_path / "db.sqlite").config == FingerprintConfig(high_band=False)
    with pytest.raises(StoreError):
        Store(tmp_path / "db.sqlite", FingerprintConfig(high_band=True))


def test_intro_and_outro_stretch_to_the_ends():
    types = {1: INTRO, 2: REPEATED, 3: OUTRO}
    matches = [SegmentMatch(1, 30, 60, 0, 99), SegmentMatch(2, 600, 660, 0, 99),
               SegmentMatch(3, 2000, 2030, 0, 99)]  # patron names follow the outro until 3000

    sections = build_sections(matches, types, duration=3000)

    assert [(s.start, s.end, s.types) for s in sections] == [
        (0, 60, [INTRO]), (600, 660, [REPEATED]), (2000, 3000, [OUTRO])]


def test_fill_gaps_takes_in_short_unique_audio_between_repeats():
    types = {1: REPEATED, 2: REPEATED, 3: REPEATED}
    matches = [SegmentMatch(1, 100, 130, 0, 99), SegmentMatch(2, 150, 180, 0, 99),
               SegmentMatch(3, 300, 330, 0, 99)]

    assert len(build_sections(matches, types, 1000)) == 3
    filled = build_sections(matches, types, 1000, fill_gaps=30)
    assert [(s.start, s.end, s.unique_sec) for s in filled] == [(100, 180, 20), (300, 330, 0)]


def test_bumpers_turn_new_ads_into_one_section():
    types = {1: BUMPER_IN, 2: BUMPER_OUT}
    matches = [SegmentMatch(1, 500, 504, 0, 50), SegmentMatch(2, 700, 704, 0, 50)]

    [section] = build_sections(matches, types, 1000)

    assert (section.start, section.end) == (504, 700)  # the bumpers themselves stay out
    assert section.types == [BREAK]


def test_neighbours_alternate_newer_and_older():
    eps = list("abcdefg")
    assert neighbours(eps, 3, 4) == ["c", "e", "b", "f"]
    assert neighbours(eps, 0, 3) == ["b", "c", "d"]


def test_parse_feed_keeps_the_feed_url_and_title():
    xml = b"""<rss><channel><title>My Show</title>
      <item><title>Ep 1</title><guid>g1</guid><pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate>
        <enclosure url="http://cdn/1.mp3" type="audio/mpeg"/></item>
    </channel></rss>"""
    feed = parse_feed(xml, "http://show/feed")
    assert (feed.url, feed.title) == ("http://show/feed", "My Show")
    assert feed.episodes[0].audio_url == "http://cdn/1.mp3"


def _params():
    from podblock.matcher import MatchParams
    return MatchParams(min_length_sec=5, min_hits_per_bin=3)


def test_music_bed_outro_found_where_the_floor_steadies():
    from podblock.fingerprint import FRAME_SEC
    from podblock.outro import music_bed_outro
    rng = np.random.default_rng(5)
    frames = lambda sec: int(sec / FRAME_SEC)
    # The show: talk whose quiet moments swing between near-silence and a bed.
    show = np.repeat(rng.choice([-70.0, -40.0], size=frames(1800) // 50), 50)
    show = show + rng.normal(0, 3, len(show))
    # The outro: talk over a steady bed, so the floor holds still.
    outro = -38.0 + rng.normal(0, 1, frames(300)) + np.where(rng.random(frames(300)) < 0.5, 15, 0)

    start = music_bed_outro(np.concatenate([show, outro]).astype(np.float16))

    assert start == pytest.approx(len(show) * FRAME_SEC, abs=20)
    assert music_bed_outro(show.astype(np.float16)) is None


def test_outro_bed_runs_to_the_end_of_the_report():
    matches = [SegmentMatch(1, 100, 160, 0, 99)]
    sections = build_sections(matches, {1: REPEATED}, 3000, outro_bed_start=2600)
    assert [(s.start, s.end, s.types) for s in sections] == [(100, 160, [REPEATED]), (2600, 3000, [OUTRO])]


def test_fingerprint_keeps_its_loudness_curve(tmp_path):
    fp = tones(6)
    fp.loudness = np.linspace(-60, -20, 100).astype(np.float16)
    fp.save(tmp_path / "fp.npz")
    assert np.array_equal(Fingerprint.load(tmp_path / "fp.npz").loudness, fp.loudness)


def test_ignored_blocks_are_never_reported_or_found_again(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    ep = store.upsert_episode("e")
    fp = tones(7)
    block = store.add_segment(fp, None, 0, 20)
    store.replace_occurrences(ep, [SegmentMatch(block, 0, 20, 0, 99)])

    store.set_type(block, "ignored")

    assert store.scan(fp, _params()) == []
    assert store.occurrences_for_episode(ep) == []
    # It still covers its audio, so the same audio is not stored again.
    assert [g for g in store.uncovered(fp, _params()) if g[1] - g[0] >= 1.5] == []
