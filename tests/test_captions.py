"""Captions from the transcription service: split into cues, saved, and moved to fit the cut."""

import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from podblock.app import captions, chapters, jobs, speakers, worker
from podblock.app.config import Settings


def words(start, text, each=0.3):
    return [[round(start + i * each, 3), round(start + (i + 1) * each, 3), f" {w}"]
            for i, w in enumerate(text.split())]


def segment(start, text, each=0.3):
    w = words(start, text, each)
    return {"start": w[0][0], "end": w[-1][1], "text": text, "words": w}


def test_a_caption_ends_at_a_sentence_a_pause_or_two_lines():
    found = captions.cues([
        segment(0, "Welcome back to the show everybody. Today we have a guest."),
        segment(20, "Hi."),  # after a pause, so a caption of its own
        {"start": 30, "end": 34, "text": " No word times here.", "words": []},
        segment(40, " ".join(["word"] * 40), each=0.1),
    ])
    assert [(round(a, 1), round(b, 1), t) for a, b, t in found[:4]] == [
        (0.0, 1.8, "Welcome back to the show everybody."),
        (1.8, 3.3, "Today we have a guest."),
        (20.0, 20.3, "Hi."),
        (30.0, 34.0, "No word times here."),
    ]
    long = found[4:]
    assert len(long) == 3 and all(len(line) <= captions.LINE for _, _, t in long for line in t.split("\n"))
    assert all(b - a <= captions.MAX_SEC for a, b, _ in found)

    split = captions.cues([segment(0, "ask not what your country"), segment(1.5, "can do for you,")])
    assert [t for _, _, t in split] == ["ask not what your country can do for you,"]  # whisper broke it, not a pause

    paused = captions.cues([{"start": 0, "end": 9, "text": "", "words": [[0, 0.5, " Before"], [3, 3.5, " after"]]}])
    assert [t for _, _, t in paused] == ["Before", "after"]


def test_webvtt_escapes_what_would_read_as_markup():
    assert captions.webvtt([(1, 2.5, "A <b> & C")]) == (
        "WEBVTT\n\nNOTE Made by pod-block with faster-whisper.\n\n00:00:01.000 --> 00:00:02.500\nA &lt;b&gt; &amp; C\n")


# ---- the job, against the real service with a stand-in for the model ----------------------

def load_server():
    spec = importlib.util.spec_from_file_location("whisper_server", Path(__file__).parent.parent / "whisper/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSpeakers:
    """Two voices: a guest reading the ad, then the host."""

    def __init__(self):
        self.calls = []

    def run(self, samples, opts, progress):
        self.calls.append(opts)
        progress(0.5)
        return [[0.9, 3.0, 0], [11.5, 19.0, 1]]


class FakeModel:
    supported_languages = ["en", "fr"]

    def __init__(self):
        self.calls = []
        self.fail = False

    def transcribe(self, samples, language=None, **options):
        self.calls.append({"bytes": samples, "language": language, **options})
        if self.fail:
            raise RuntimeError("out of memory")
        segs = [segment(1, "An ad you will hear again."), segment(12, "The show itself starts here.")]
        return ((SimpleNamespace(**{**s, "words": [SimpleNamespace(start=a, end=b, word=w) for a, b, w in s["words"]]})
                 for s in segs), SimpleNamespace(language=language or "en", duration=20.0))


@pytest.fixture
def service():
    server_module = load_server()
    server_module.model = FakeModel()
    server_module.decode = lambda path: Path(path).read_bytes()  # the "samples" the stand-in models get
    server = server_module.ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", model=server_module.model,
                          module=server_module)
    server.shutdown()


@pytest.fixture
def setup(db, tmp_path, service):
    settings = Settings(data_dir=tmp_path, media_dir=tmp_path / "media", whisper_url=service.url)
    audio = tmp_path / "media/original/ep.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"not really audio")
    db.execute("INSERT INTO podcasts (id, name, language) VALUES (1, 'Show', 'en-US')")
    db.execute("INSERT INTO episodes (id, guid, podcast_id, state, original_path, transcripts, cut_spec, cut_duration) "
               "VALUES (1, 'ep-1', 1, 'ready', ?, '[]', '[[0, 1], [11, 20]]', 10)", (str(audio),))
    db.commit()
    return settings


def episode(db):
    return db.execute("SELECT * FROM episodes WHERE id = 1").fetchone()


def test_captions_are_made_once_and_fit_the_cut(db, setup, service):
    assert captions.queue(db, setup, [1, 1], None) == [1]
    assert captions.queue(db, setup, [1], None) == []  # one is waiting already
    assert jobs.claim(db, besides=(captions.KIND,)) is None  # the downloads' threads leave it be
    assert jobs.listing(db)["queued"][0]["about_id"] == 1  # the jobs page names its episode

    job = jobs.claim(db, kinds=(captions.KIND,))
    assert worker.run_job(setup, db, job) == "done", db.execute("SELECT error FROM jobs").fetchone()[0]
    assert service.model.calls[0]["bytes"] == b"not really audio"
    assert service.model.calls[0]["language"] == "en"  # the show's language
    assert episode(db)["state"] == "ready"  # in the feeds all along

    saved = chapters.transcripts(episode(db))
    assert [(t["format"], t["rel"], t["language"]) for t in saved] == [("vtt", "captions", "en")]
    shifted = chapters.transcript(episode(db), "vtt")
    assert "An ad" not in shifted  # it played in the cut
    assert "00:00:02.000 --> 00:00:03.500\nThe show itself starts here." in shifted

    assert captions.queue(db, setup, [1], None) == []  # it has its transcript


def test_a_feeds_own_transcript_wins(db, setup):
    db.execute("UPDATE episodes SET transcripts = ? WHERE id = 1", (json.dumps([{"format": "srt", "path": "x"}]),))
    db.commit()
    assert captions.queue(db, setup, [1], None) == []
    assert captions.queue(db, Settings(data_dir=setup.data_dir, media_dir=setup.media_dir), [1], None) == []


def test_a_failure_in_the_service_fails_the_job(db, setup, service):
    service.model.fail = True
    captions.queue(db, setup, [1], None)
    job = jobs.claim(db, kinds=(captions.KIND,))
    assert worker.run_job(setup, db, job) == "failed"
    assert "out of memory" in db.execute("SELECT error FROM jobs").fetchone()[0]
    assert episode(db)["transcripts"] == "[]" and episode(db)["state"] == "ready"
    assert not (setup.media_dir / "transcripts").exists()


def test_speaker_turns_are_saved_with_the_captions_and_compared_with_the_repeats(db, setup, service):
    service.module.speakers = FakeSpeakers()
    db.execute("INSERT INTO reports VALUES (1, 0, ?)",
               (json.dumps({"sections": [{"start": 1.0, "end": 11.0, "types": ["repeat"]}]}),))
    db.commit()
    captions.queue(db, setup, [1], None)
    assert worker.run_job(setup, db, jobs.claim(db, kinds=(captions.KIND,))) == "done"
    found = speakers.saved(episode(db))
    assert found["turns"] == [[0.9, 3.0, 0], [11.5, 19.0, 1]]
    assert found["options"] == {"threshold": 0.8, "count": 0, "min_on": 0.3, "min_off": 0.5}
    assert "<v" not in chapters.transcript(episode(db), "vtt")  # not in the captions, for now

    short = {"lone_share": 0.3, "lone_min": 1}  # a short episode
    people = speakers.summary(found["turns"], [{"start": 1.0, "end": 11.0}], **short)
    assert [(p["speaker"], round(p["in_repeats"], 2), p["lone"]) for p in people] == [(1, 0.0, False), (0, 0.95, True)]
    text = speakers.report(found["turns"], [{"start": 1.0, "end": 11.0, "types": ["repeat"]}], **short)
    assert "S0 100%  <- lone voice" in text

    # Other options, for the speakers alone; the stand-in hears the same either way.
    heard = captions.ask(SimpleNamespace(step=lambda t: None, progress=lambda f: None), service.url, "speakers",
                         Path(episode(db)["original_path"]), {"threshold": 0.6, "count": 2})
    assert service.module.speakers.calls[-1] == {"threshold": 0.6, "count": 2, "min_on": 0.3, "min_off": 0.5}
    assert heard["speakers"] and not heard["segments"]

    from podblock.app import retention
    retention.clear(db, episode(db))
    assert not list((setup.media_dir / "transcripts").iterdir())  # the turns go with the download
