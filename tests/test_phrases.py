"""Intro and outro phrases: found in a transcript's words, and the cuts they make."""

import json

import pytest

from podblock.app import phrases


def said(start, text, each=0.5):
    return [(start + i * each, start + (i + 1) * each - 0.1, w) for i, w in enumerate(phrases.tokens(text))]


def test_words_match_without_case_or_punctuation():
    assert phrases.tokens("Let’s get INTO it!") == ["lets", "get", "into", "it"]
    assert phrases.parse(" let's  get into it \n\nLETS GET INTO IT\nthat's all, folks\n") == [
        "let's get into it", "that's all, folks"]
    for bad in ("?!", "x" * 101, "\n".join(f"phrase {i}" for i in range(21))):
        with pytest.raises(phrases.PhraseError):
            phrases.parse(bad)


def test_an_intro_ends_at_its_first_hearing_and_an_outro_starts_at_its_last():
    heard = [*said(0, "welcome back everybody lets get into it today we talk"),
             *said(300, "and lets get into it once more"),  # past the first third: not the intro's end
             *said(500, "thanks for listening see you next week"),
             *said(560, "thanks for listening this was a production of somebody")]
    found = phrases.find(heard, 600, ["let's get into it", "never said"], ["Thanks for listening!"])
    assert found == [
        {"type": "intro", "start": 0.0, "end": 3.5, "phrase": "let's get into it"},
        {"type": "outro", "start": pytest.approx(559.75), "end": 600, "phrase": "Thanks for listening!"},
    ]
    assert found[0]["end"] == heard[7][0]  # padded, but not into the next word

    assert phrases.find(said(300, "lets get into it"), 600, ["lets get into it"], []) == []
    assert phrases.find(said(100, "thanks for listening"), 600, [], ["thanks for listening"]) == []  # too early
    assert phrases.find([], 600, ["anything"], ["anything"]) == []


def test_a_feeds_transcript_times_words_by_where_they_fall_in_a_cue(tmp_path):
    vtt = tmp_path / "t.vtt"
    vtt.write_text("WEBVTT\n\n00:00:10.000 --> 00:00:14.000\n<v Host>Okay, let's &amp; go\n")
    ep = {"transcripts": json.dumps([{"format": "vtt", "path": str(vtt)}])}
    words = phrases.words(ep)
    assert [w for _, _, w in words] == ["okay", "lets", "go"]
    assert words[0][0] == pytest.approx(10) and words[-1][1] == pytest.approx(14)
    assert words[1][0] == pytest.approx(10 + 4 * 6 / 16)

    js = tmp_path / "t.json"
    js.write_text(json.dumps({"segments": [{"startTime": 1, "endTime": 2, "body": "one two"},
                                           {"startTime": 5, "body": "three"}]}))
    words = phrases.words({"transcripts": json.dumps([{"format": "json", "path": str(js)}])})
    assert [(w, round(a, 2)) for a, _, w in words] == [("one", 1), ("two", 1.57), ("three", 5)]


def test_captions_word_times_win_over_their_cues(tmp_path):
    vtt, timed = tmp_path / "c.vtt", tmp_path / "c.words.json"
    vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:09.000\nwrong times\n")
    timed.write_text(json.dumps([[1.0, 1.4, " That's"], [1.5, 2.0, " it."]]))
    ep = {"transcripts": json.dumps([{"format": "vtt", "path": str(vtt), "words": str(timed)}])}
    assert phrases.words(ep) == [(1.0, 1.4, "thats"), (1.5, 2.0, "it")]
