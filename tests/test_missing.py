import random

from podblock.app.missing import align, cuts, spoken
from podblock.report import BREAK, build_sections
from podblock.store import BUMPER_IN, BUMPER_OUT, SegmentMatch

VOCAB = [f"w{i}" for i in range(400)]


def talk(n, seed):
    rng = random.Random(seed)
    return [rng.choice(VOCAB) for _ in range(n)]


def timed(words, start, per_word=0.4):
    return [(start + i * per_word, start + (i + 1) * per_word - 0.05, w) for i, w in enumerate(words)]


def test_an_ad_put_in_is_missing_from_a_transcript_timed_to_the_recording():
    show_a, show_b, ad = talk(500, 1), talk(500, 2), talk(150, 3)
    # The feed's transcript runs straight on from one half to the other, in the recording's times.
    feed = timed(show_a, 0) + timed(show_b, 200)
    # The download has a 60-second ad between them, which the transcriber hears.
    heard = timed(show_a, 0) + timed(ad, 200) + timed(show_b, 260)

    fit = align(feed, heard, duration=460)

    assert fit.coverage == 1.0
    assert [(round(a), round(b)) for a, b in fit.missing] == [(200, 260)]
    assert cuts(fit) == [{"type": "missing", "start": fit.missing[0][0], "end": fit.missing[0][1]}]


def test_an_ad_put_in_is_missing_from_a_transcript_timed_to_the_download():
    show_a, show_b, ad = talk(500, 1), talk(500, 2), talk(150, 3)
    feed = timed(show_a, 0) + timed(show_b, 260)  # a blank where the ad plays
    heard = timed(show_a, 0) + timed(ad, 200) + timed(show_b, 260)

    assert [(round(a), round(b)) for a, b in align(feed, heard, duration=460).missing] == [(200, 260)]


def test_mishearing_and_pauses_are_not_missing():
    words = talk(1200, 4)
    feed = timed(words, 0)
    misheard = [("x" if i % 9 == 0 else w) for i, w in enumerate(words)]
    heard = timed(misheard[:600], 0) + timed(misheard[600:], 240 + 10)  # a pause the transcript has too
    feed = timed(words[:600], 0) + timed(words[600:], 250)

    fit = align(feed, heard, duration=500)

    assert fit.missing == []
    assert spoken(fit) == []  # with nothing missing, the transcript may have the ads in it


def test_a_transcript_of_something_else_gives_nothing():
    assert align(timed(talk(800, 5), 0), timed(talk(800, 6), 0), duration=320) is None


def test_bumpers_around_the_show_itself_make_no_break():
    kinds = {1: BUMPER_IN, 3: BUMPER_OUT}
    matches = [SegmentMatch(1, 100, 104, 0, 50), SegmentMatch(3, 400, 404, 0, 50)]
    types = dict(kinds)

    assert any(BREAK in s.types for s in build_sections(matches, types, 1000))
    assert not build_sections(matches, types, 1000, spoken=[(90, 380)])


def test_music_after_the_last_word_is_not_missing_but_an_ad_there_is():
    show, ad = talk(600, 7), talk(150, 8)
    feed = timed(show, 0)

    assert align(feed, timed(show, 0), duration=300).missing == []  # 60 s of music after the talk
    assert [(round(a), round(b)) for a, b in align(feed, timed(show, 0) + timed(ad, 245), duration=320).missing] \
        == [(240, 320)]
