import json

import pytest

from podblock.web import chapters, grabs
from podblock.web.chapters import Shift, build

KEEP = [(0, 10), (20, 30), (40, 50)]  # cuts at 10-20 and 30-40, and after 50


def test_times_move_onto_the_cut_audio():
    shift = Shift(KEEP)
    assert [shift(t) for t in (0, 5, 10, 15, 20, 25, 35, 45, 50, 70)] == [0, 5, 10, 10, 10, 15, 20, 25, 30, 30]
    assert shift.length == 30
    assert shift.joins() == [(10, 10, 20), (20, 30, 40)]  # nothing plays after the last cut, so it has no join
    late = Shift([(5, 10), (12, 20)])
    assert late(2) == 0 and late(11) == 5 and late.joins() == [(0, 0, 5), (5, 10, 12)]


def test_each_join_gets_a_chapter_saying_what_was_cut():
    cut = [(10, 20, {"break"}), (30, 40, {"repeated"})]
    assert build(KEEP, cut, []) == [
        {"start": 0.0, "title": "Start"},
        {"start": 10.0, "title": "10 sec ad break cut"},
        {"start": 20.0, "title": "10 sec repeat cut"},
    ]
    intro = build([(95, 200)], [(0, 95, {"intro", "repeated"})], [])
    assert intro == [{"start": 0.0, "title": "1:35 intro cut"}]  # no "Start" when a cut starts the episode
    by_hand = build([(0, 10), (4000, 4100)], [(10, 4000, {"manual"})], [])
    assert by_hand[1]["title"] == "1:06:30 hand-marked cut"
    assert build([(0, 50)], [], []) == []  # nothing cut, no chapters of its own: none


def test_the_sources_chapters_move_and_name_the_joins():
    source = [{"start": 0, "title": "Hello", "img": "https://example.com/a.jpg"},
              {"start": 12, "title": "Sponsor", "url": "https://example.com/buy"},  # all inside the first cut
              {"start": 20, "title": "Interview", "img": "https://example.com/b.jpg"},
              {"start": 35, "title": "Part two", "toc": False}]  # starts inside the second cut
    out = build(KEEP, [(10, 20, {"break"}), (30, 40, {"break"})], source)
    assert out == [
        {"start": 0.0, "title": "Hello", "img": "https://example.com/a.jpg"},
        {"start": 10.0, "title": "Interview (after 10 sec ad break cut)", "img": "https://example.com/b.jpg"},
        {"start": 20.0, "title": "Part two (after 10 sec ad break cut)", "toc": False},
    ]
    doc = json.loads(chapters.chapters_json(out))
    assert doc["version"] and doc["chapters"][2] == {"startTime": 20.0, "title": "Part two (after 10 sec ad break cut)",
                                                     "toc": False}
    # A chapter that plays on across a cut is named again after it.
    assert build(KEEP, [(10, 20, {"break"}), (30, 40, {"intro"})], source[:1]) == [
        {"start": 0.0, "title": "Hello", "img": "https://example.com/a.jpg"},
        {"start": 10.0, "title": "Hello (after 10 sec ad break cut)", "img": "https://example.com/a.jpg"},
        {"start": 20.0, "title": "Hello (after 10 sec intro cut)", "img": "https://example.com/a.jpg"},
    ]


def test_reading_chapters_from_the_feed():
    body = json.dumps({"version": "1.2.0", "chapters": [
        {"startTime": 61.5, "title": " Two ", "url": "javascript:alert(1)"},
        {"startTime": 0, "title": "One", "img": "https://example.com/1.jpg", "endTime": 61.5},
        {"title": "no start"}]}).encode()
    assert chapters._json_chapters(body) == [
        {"start": 0.0, "title": "One", "end": 61.5, "img": "https://example.com/1.jpg"},
        {"start": 61.5, "title": "Two"}]
    import xml.etree.ElementTree as ET
    item = ET.fromstring("""<item xmlns:psc="http://podlove.org/simple-chapters"><psc:chapters version="1.2">
        <psc:chapter start="00:00:00.000" title="Intro"/><psc:chapter start="1:02:03.5" title="Late" href="https://e.com"/>
        <psc:chapter start="soon" title="Bad"/></psc:chapters></item>""")
    assert chapters._psc_chapters(item) == [{"start": 0.0, "title": "Intro"},
                                            {"start": 3723.5, "title": "Late", "url": "https://e.com"}]


VTT = """WEBVTT

NOTE made by hand

1
00:00:02.000 --> 00:00:04.500 align:start
<v Host>Hello <00:00:03.000>there

00:12.000 --> 00:15.000
An ad, all inside a cut

00:00:18.000 --> 00:00:22.000
Across a join

00:00:45.000 --> 00:00:46.000
Near the end
"""


def test_transcripts_move_with_the_cut():
    shift = Shift(KEEP)
    assert chapters._shift_cues(VTT, shift, srt=False) == """WEBVTT

NOTE made by hand

1
00:00:02.000 --> 00:00:04.500 align:start
<v Host>Hello <00:00:03.000>there

00:00:10.000 --> 00:00:12.000
Across a join

00:00:25.000 --> 00:00:26.000
Near the end
"""
    srt = "1\r\n00:00:12,000 --> 00:00:15,000\r\nGone\r\n\r\n2\r\n00:00:41,250 --> 00:00:43,000\r\nKept\r\n"
    assert chapters._shift_cues(srt, shift, srt=True) == "1\n00:00:21,250 --> 00:00:23,000\nKept\n"
    doc = {"version": "1.0.0", "segments": [{"startTime": 12, "endTime": 14, "body": "ad"},
                                            {"startTime": 41.5, "endTime": 42, "body": "kept", "speaker": "A"}]}
    assert json.loads(chapters._shift_json(json.dumps(doc), shift)) == {
        "version": "1.0.0", "segments": [{"startTime": 21.5, "endTime": 22, "body": "kept", "speaker": "A"}]}


@pytest.fixture
def db(tmp_path):
    from podblock.web.db import connect, prepare
    prepare(tmp_path / "podblock.sqlite")
    conn = connect(tmp_path / "podblock.sqlite")
    conn.execute("INSERT INTO users (id, username, is_admin, created_at) VALUES (1, 'admin', 1, 0)")
    conn.execute("INSERT INTO episodes (id, guid) VALUES (1, 'ep-1')")
    conn.commit()
    yield conn
    conn.close()


def test_hits_close_together_are_one_grab(db):
    t = 1_000_000.0
    assert grabs.record(db, 1, 1, "feed", now=t)  # a stream starts
    assert not grabs.record(db, 1, 1, "feed", now=t + 5)  # the background download alongside it
    assert not grabs.record(db, 1, 1, "feed", now=t + 5 + grabs.WINDOW)  # still going
    assert not grabs.record(db, 1, 1, "feed", resumed=True, now=t + 86400)  # a seek, a day later
    assert grabs.record(db, 1, 1, "feed", now=t + 2 * 86400)  # fetched from the start again: a new grab
    rows = db.execute("SELECT hits, first_at, last_at FROM grabs ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(4, t, t + 86400), (1, t + 2 * 86400, t + 2 * 86400)]


def test_what_counts_as_a_resume_and_a_play():
    assert [grabs.resumed(h) for h in (None, "", "bytes=0-", "bytes=0-1", "bytes=1000-", "bytes=-500", "junk")] == [
        False, False, False, False, True, True, False]
    assert grabs.play_needed(3600) == 300  # five minutes beats 5% of an hour
    assert grabs.play_needed(4 * 3600) == 720  # 5% of four hours beats five minutes
    assert grabs.play_needed(120) == 108  # a short one: most of it
    assert grabs.play_needed(None) == 300
