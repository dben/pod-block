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
    assert "<v" not in chapters.transcript(episode(db), "vtt")  # not in the feeds' captions, for now
    # The page's player gets them, lettered by how much each talks.
    assert "00:00:02.000 --> 00:00:03.500\n<v A>The show itself starts here." in chapters.captions(episode(db))

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


def test_captions_keep_each_words_time_and_an_intro_phrase_heard_cuts_again(db, setup, service):
    from podblock.app import phrases
    db.execute("UPDATE episodes SET duration = 20 WHERE id = 1")
    db.execute("UPDATE podcasts SET intro_phrases = ? WHERE id = 1", (json.dumps(["You will hear AGAIN"]),))
    db.commit()
    captions.queue(db, setup, [1], None)
    assert worker.run_job(setup, db, jobs.claim(db, kinds=(captions.KIND,))) == "done"
    assert phrases.words(episode(db))[:3] == [(1.0, 1.3, "an"), (1.3, 1.6, "ad"), (1.6, 1.9, "you")]
    assert phrases.for_episode(db, episode(db)) == [
        {"type": "intro", "start": 0.0, "end": 3.05, "phrase": "You will hear AGAIN"}]
    queued = db.execute("SELECT kind, episode_id, payload FROM jobs WHERE state = 'queued'").fetchall()
    assert [tuple(r) for r in queued] == [("process", 1, '{"recut": true}')]
    assert episode(db)["state_note"] == "Waiting to be cut again: an intro or outro phrase was heard"

    from podblock.app import retention
    retention.clear(db, episode(db))
    assert not list((setup.media_dir / "transcripts").iterdir())  # the word times go with the download


def test_transcribing_again_replaces_the_feeds_transcript_and_earlier_captions(db, setup, service, tmp_path):
    feeds = tmp_path / "media/transcripts/feed.srt"
    feeds.parent.mkdir(parents=True)
    feeds.write_text("1\n00:00:01,000 --> 00:00:02,000\nThe feed's own\n")
    db.execute("UPDATE episodes SET transcripts = ? WHERE id = 1",
               (json.dumps([{"format": "srt", "path": str(feeds)}]),))
    db.execute("UPDATE podcasts SET transcript_check = 0")
    db.commit()
    assert captions.queue(db, setup, [1], None) == []  # it has one
    assert captions.queue(db, setup, [1], None, force=True) == [1]
    assert captions.queue(db, setup, [1], None, force=True) == []  # one is waiting already
    assert worker.run_job(setup, db, jobs.claim(db, kinds=(captions.KIND,))) == "done"
    (saved,) = chapters.transcripts(episode(db))
    assert saved["made"] == "whisper" and not feeds.exists()
    files = {saved["path"], saved["words"]}

    captions.queue(db, setup, [1], None, force=True)  # again: written over the same files, which stay
    assert worker.run_job(setup, db, jobs.claim(db, kinds=(captions.KIND,))) == "done"
    assert [t["path"] for t in chapters.transcripts(episode(db))] == [saved["path"]]
    assert all(Path(f).exists() for f in files) and len(service.model.calls) == 2


def test_a_feed_transcript_is_checked_against_what_is_heard_and_stays_the_one_served(db, setup, service, tmp_path):
    from podblock.app import missing, phrases
    feeds = tmp_path / "media/transcripts/feed.srt"
    feeds.parent.mkdir(parents=True)
    feeds.write_text("1\n00:00:02,000 --> 00:00:03,500\nThe show itself starts here.\n")
    db.execute("UPDATE episodes SET transcripts = ?, duration = 20 WHERE id = 1",
               (json.dumps([{"format": "srt", "path": str(feeds)}]),))
    db.commit()
    assert missing.enabled(db, setup, 1)  # on by itself: the feed has transcripts
    assert captions.queue(db, setup, [1], None) == [1]
    assert worker.run_job(setup, db, jobs.claim(db, kinds=(captions.KIND,))) == "done"

    (saved,) = chapters.transcripts(episode(db))
    assert saved["path"] == str(feeds) and Path(saved["words"]).exists()  # the feed's, with what was heard
    assert phrases.feed_words(episode(db))[0][2] == "the"
    assert phrases.words(episode(db))[0] == (1.0, 1.3, "an")
    queued = db.execute("SELECT kind, episode_id, payload FROM jobs WHERE state = 'queued'").fetchall()
    assert [tuple(r) for r in queued] == [("process", 1, '{"recut": true}')]
    assert captions.queue(db, setup, [1], None) == []  # checked once

    db.execute("UPDATE podcasts SET transcript_check = 0")
    assert missing.fit(db, setup, episode(db)) is None  # turned off


def test_the_downloads_page_queues_episodes_to_transcribe_again(db, setup):
    pytest.importorskip("fastapi")
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from podblock.web.app import create_app
    client = TestClient(create_app(replace(setup, api_key="k", background=False)), follow_redirects=False)
    client.post("/login", data={"key": "k"})
    db.execute("UPDATE episodes SET transcripts = '[{\"format\": \"vtt\", \"path\": \"x\"}]' WHERE id = 1")
    db.commit()
    assert 'formaction="/downloads/transcribe"' in client.get("/downloads").text
    res = client.post("/downloads/transcribe", data={"ids": [1, 2]})
    assert res.status_code == 303 and res.headers["location"] == "/downloads"
    (job,) = db.execute("SELECT kind, payload FROM jobs").fetchall()
    assert tuple(job) == (captions.KIND, '{"episode": 1, "force": true}')

    off = TestClient(create_app(replace(setup, api_key="k", background=False, whisper_url=None)))
    off.post("/login", data={"key": "k"})
    assert "/downloads/transcribe" not in off.get("/downloads").text  # no transcriber, no button


# ---- more than one replica, and replicas that stop ---------------------------------

class Steps:
    def __init__(self):
        self.steps = []

    def step(self, text):
        self.steps.append(text)

    def progress(self, fraction):
        pass


def later(seconds, do):
    timer = threading.Timer(seconds, do)
    timer.start()
    return timer


def test_a_busy_replica_says_so_at_once(service, tmp_path):
    import requests

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    service.module.turn.acquire()
    try:
        with requests.post(f"{service.url}/transcribe", data=audio.read_bytes(), stream=True, timeout=3) as resp:
            assert json.loads(next(captions._lines(resp))) == {"waiting": True}  # not after WAIT_NOTE
    finally:
        service.module.turn.release()


def test_with_patience_a_busy_replica_is_asked_again_and_without_it_waited_for(service, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    for patience, moved_on in ((5.0, True), (0.0, False)):
        service.module.turn.acquire()
        later(0.3, service.module.turn.release)
        said = Steps()
        heard = captions.ask(said, service.url, "transcribe", audio, patience=patience)
        assert len(heard["segments"]) == 2
        assert ("Waiting for a free transcriber" in said.steps) == moved_on


def test_a_stopping_replica_sends_away_those_waiting_and_refuses_new_ones(service, tmp_path):
    import requests

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    service.module.WAIT_NOTE = 0.1
    service.module.turn.acquire()
    try:
        later(0.2, service.module.stopping.set)
        with requests.post(f"{service.url}/transcribe", data=b"audio", stream=True, timeout=3) as resp:
            assert [json.loads(r) for r in resp.iter_lines() if r][-1] == {"waiting": True}  # and no "done"
        assert requests.post(f"{service.url}/transcribe", data=b"audio", timeout=3).status_code == 503
        with pytest.raises(captions.CutOff, match="isn't ready"):  # asked once more, then given up on
            captions.ask(Steps(), service.url, "transcribe", audio)
    finally:
        service.module.turn.release()
    assert not service.model.calls


def test_a_transcription_cut_off_is_asked_for_once_more(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    asked = []

    class Flaky(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            asked.append(self.path)
            self.send_response(200)
            self.end_headers()
            lines = [{"language": "en", "duration": 20.0}, segment(1, "An ad you will hear again.")]
            if len(asked) > len(cut_after):
                lines.append({"done": True})
            self.wfile.write(b"".join(json.dumps(x).encode() + b"\n" for x in lines))

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        cut_after = [1]  # the first answer ends with no "done"
        said = Steps()
        heard = captions.ask(said, url, "transcribe", audio)
        assert len(asked) == 2 and len(heard["segments"]) == 1 and "Asking the transcriber again" in said.steps
        asked.clear()
        cut_after = [1, 2]  # twice: the job fails
        with pytest.raises(captions.CutOff, match="part way"):
            captions.ask(Steps(), url, "transcribe", audio)
        assert len(asked) == 2
    finally:
        server.shutdown()


def test_captions_jobs_get_as_many_threads_as_asked_for(tmp_path):
    settings = Settings(data_dir=tmp_path, media_dir=tmp_path / "media", caption_workers=3)
    names = [t.name for t in worker.Background(settings, workers=2)._threads]
    assert sum(n.startswith("podblock-captions-") for n in names) == 3
    assert sum(n.startswith("podblock-worker-") for n in names) == 2


def test_speaker_models_land_whole_and_once_in_a_shared_folder(tmp_path, monkeypatch):
    import io
    import tarfile

    module = load_server()
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:bz2") as tar:
        data = b"segmentation model"
        info = tarfile.TarInfo("sherpa-onnx-pyannote-segmentation-3-0/model.onnx")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    fetched = []

    def urlopen(url):
        fetched.append(url)
        return io.BytesIO(packed.getvalue())

    def urlretrieve(url, to):
        fetched.append(url)
        Path(to).write_bytes(b"embedding model")

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(module.urllib.request, "urlretrieve", urlretrieve)
    folder = tmp_path / "speakers"
    (folder / "sherpa-onnx-pyannote-segmentation-3-0").mkdir(parents=True)  # half done by an older version
    with module.fetching(tmp_path):
        found = module.Speakers(folder, threads=1)
    assert found.segmentation.read_bytes() == b"segmentation model"
    assert found.embedding.read_bytes() == b"embedding model"
    assert sorted(p.name for p in folder.iterdir()) == ["sherpa-onnx-pyannote-segmentation-3-0",
                                                       found.embedding.name]  # no temporary files left
    module.Speakers(folder, threads=1)  # a second replica finds them there
    assert len(fetched) == 2


def test_a_captions_job_times_sending_waiting_transcribing_and_speakers(db, setup, service):
    service.module.speakers = FakeSpeakers()
    captions.queue(db, setup, [1], None)
    worker.run_queue(setup)
    rows = {r["step"]: r for r in db.execute("SELECT * FROM timings WHERE kind = 'captions'")}
    assert set(rows) == {"captions.upload", "captions.transcribe", "captions.speakers", "job"}
    assert rows["captions.upload"]["count"] == len(b"not really audio")
    assert rows["captions.transcribe"]["audio"] == rows["captions.speakers"]["audio"] == 20.0


def test_time_spent_on_busy_replicas_counts_as_waiting(service, tmp_path):
    from podblock.timing import Timer

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    service.module.turn.acquire()
    later(0.3, service.module.turn.release)
    timer = Timer()
    captions.ask(Steps(), service.url, "transcribe", audio, patience=5.0, timer=timer)
    rows = {r["step"]: r for r in timer.rows}
    assert rows["captions.wait"]["count"] > 1 and rows["captions.wait"]["seconds"] >= 0.25  # tries
    assert set(rows) == {"captions.upload", "captions.wait", "captions.transcribe"}
