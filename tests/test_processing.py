import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi")
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")

from fastapi.testclient import TestClient
from test_feeds import FakeWeb, rss

from podblock.cut import probe
from podblock.web import background, feeds, jobs, processing
from podblock.web.app import create_app
from podblock.web.config import Settings
from podblock.web.db import connect

URL = "https://example.com/show.rss"
SR = 16000
JINGLE, AD, FILLER = 15.0, 12.0, 30.0


def fake_audio(seconds, seed):
    """Noise plus a random melody, which gives the fingerprinter plenty to grab."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    out = rng.normal(0, 0.02, n)
    note = int(0.12 * SR)
    t = np.arange(note) / SR
    for start in range(0, n - note, note):
        for freq in rng.uniform(150, 3500, size=3):
            out[start:start + note] += 0.1 * np.sin(2 * np.pi * freq * t) * np.hanning(note)
    return out


@pytest.fixture(scope="module")
def audio_files(tmp_path_factory):
    """Five episodes: a shared jingle, then show, a shared ad, more show."""
    root = tmp_path_factory.mktemp("cdn")
    jingle, ad = fake_audio(JINGLE, 99), fake_audio(AD, 98)
    files = {}
    for n in range(1, 6):
        samples = np.concatenate([jingle, fake_audio(FILLER, n), ad, fake_audio(FILLER, 100 + n)])
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        path = root / f"{n}.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
                        "-c:a", "libmp3lame", "-b:a", "64k", str(path)], input=pcm, check=True)
        files[f"https://cdn.example.com/{n}.mp3"] = path
    return files


def item(n):
    return f"""
    <item>
      <title>Episode {n}</title>
      <guid>ep-{n}</guid>
      <pubDate>{n:02d} Jan 2026 10:00:00 +0000</pubDate>
      <enclosure url="https://cdn.example.com/{n}.mp3" type="audio/mpeg" length="1000"/>
      <description>Notes for episode {n}.</description>
    </item>"""


class FakeCdn:
    def __init__(self, files):
        self.files = files
        self.fetched = []
        self.fail = set()

    def download(self, url, dest, progress=None):
        self.fetched.append(url)
        if url in self.fail:
            raise ConnectionError("the CDN is down")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.files[url], dest)
        if progress:
            progress(dest.stat().st_size, dest.stat().st_size)
        return dest


@pytest.fixture
def env(tmp_path, monkeypatch, audio_files):
    web = FakeWeb(monkeypatch)
    web.pages[URL] = rss(*(item(n) for n in range(1, 6)))
    cdn = FakeCdn(audio_files)
    monkeypatch.setattr(processing, "download", cdn.download)
    settings = Settings(data_dir=tmp_path / "data", media_dir=tmp_path / "media", api_key="k", background=False)
    app = create_app(settings)
    client = TestClient(app, follow_redirects=False)
    client.post("/login", data={"key": "k"})
    db = connect(settings.db_path)
    pod = feeds.add_podcast(db, URL).podcast_id
    ids = {r["guid"]: r["id"] for r in db.execute("SELECT id, guid FROM episodes")}
    yield type("Env", (), dict(settings=settings, client=client, db=db, pod=pod, ids=ids, cdn=cdn, web=web))
    db.close()


def episode(env, guid):
    return env.db.execute("SELECT * FROM episodes WHERE guid = ?", (guid,)).fetchone()


def test_download_finds_repeats_and_cuts_them(env):
    res = env.client.post(f"/podcasts/{env.pod}/episodes", data={"action": "download", "ids": env.ids["ep-2"]})
    assert res.status_code == 303
    assert episode(env, "ep-2")["state"] == "queued"
    assert "Jobs <span class=\"nav-count\">1</span>" in env.client.get("/jobs").text

    assert background.run_queue(env.settings) == 1

    # The show had nothing fingerprinted, so the three episodes after it came along.
    assert sorted(env.cdn.fetched) == [f"https://cdn.example.com/{n}.mp3" for n in (2, 3, 4, 5)]
    for n in (2, 3, 4, 5):
        ep = episode(env, f"ep-{n}")
        assert ep["state"] == "ready", (n, ep["state_note"])
        assert ep["ever_downloaded"] == 1
        total = JINGLE + AD + 2 * FILLER
        assert ep["cut_duration"] == pytest.approx(total - JINGLE - AD, abs=3)
        assert probe(Path(ep["cut_path"])).codec == "mp3"
    assert episode(env, "ep-1")["state"] is None

    report = env.client.get(f"/episodes/{env.ids['ep-2']}").text
    assert "What gets cut" in report and "0:00 – 0:1" in report
    assert env.db.execute("SELECT state FROM jobs").fetchone()["state"] == "done"


def test_cut_audio_streams_with_ranges(env):
    jobs.enqueue(env.db, "process", [env.ids["ep-5"]], None)
    background.run_queue(env.settings)
    audio = env.client.get(f"/episodes/{env.ids['ep-5']}/audio", headers={"range": "bytes=0-99"})
    assert audio.status_code == 206 and len(audio.content) == 100
    assert audio.headers["content-type"] == "audio/mpeg"
    whole = env.client.get(f"/episodes/{env.ids['ep-5']}/audio?download=1")
    assert whole.headers["content-disposition"] == "attachment; filename*=utf-8''Episode%205.mp3"
    assert env.client.get(f"/episodes/{env.ids['ep-1']}/audio").status_code == 404
    assert TestClient(env.client.app).get(f"/episodes/{env.ids['ep-5']}/audio").status_code == 401


def test_processing_again_reuses_the_download(env):
    jobs.enqueue(env.db, "process", [env.ids["ep-3"]], None)
    background.run_queue(env.settings)
    fetched = len(env.cdn.fetched)
    assert jobs.enqueue(env.db, "process", [env.ids["ep-3"]], None) == []  # ready: a plain download skips it
    env.client.post(f"/episodes/{env.ids['ep-3']}/process")
    background.run_queue(env.settings)
    assert len(env.cdn.fetched) == fetched
    assert episode(env, "ep-3")["state"] == "ready"


def test_a_failed_download_can_be_retried(env):
    env.cdn.fail.add("https://cdn.example.com/1.mp3")
    jobs.enqueue(env.db, "process", [env.ids["ep-1"]], None)
    background.run_queue(env.settings)
    ep = episode(env, "ep-1")
    assert ep["state"] == "failed" and "the CDN is down" in ep["state_note"]
    job = env.db.execute("SELECT * FROM jobs").fetchone()
    assert job["state"] == "failed" and "CDN" in job["error"]
    for n in (2, 3, 4):  # companions go back to rest
        assert episode(env, f"ep-{n}")["state"] is None
    assert "Retry" in env.client.get("/jobs").text

    env.cdn.fail.clear()
    env.client.post(f"/jobs/{job['id']}/retry")
    background.run_queue(env.settings)
    assert episode(env, "ep-1")["state"] == "ready"


def test_cancelling(env, monkeypatch):
    jobs.enqueue(env.db, "process", [env.ids["ep-1"], env.ids["ep-2"]], None)
    first, second = [r["id"] for r in env.db.execute("SELECT id FROM jobs ORDER BY id")]
    env.client.post(f"/jobs/{second}/cancel")
    assert episode(env, "ep-2")["state"] is None

    def cancel_midway(ctx, settings):
        ctx.step("Downloading", ctx.job["episode_id"])
        jobs.cancel(connect(settings.db_path), ctx.job["id"])
        ctx.progress(0.5)  # throttled: no check yet
        ctx._last = 0
        ctx.progress(0.6)

    monkeypatch.setitem(background.RUNNERS, "process", cancel_midway)
    background.run_queue(env.settings)
    states = {r["id"]: r["state"] for r in env.db.execute("SELECT id, state FROM jobs")}
    assert states == {first: "cancelled", second: "cancelled"}


def test_jobs_cut_short_by_a_restart_run_again(env):
    jobs.enqueue(env.db, "process", [env.ids["ep-4"]], None)
    job = jobs.claim(env.db)
    env.db.execute("UPDATE episodes SET state = 'running' WHERE id = ?", (env.ids["ep-4"],))
    env.db.commit()
    assert jobs.recover(env.db) == 1
    assert env.db.execute("SELECT state FROM jobs WHERE id = ?", (job["id"],)).fetchone()[0] == "queued"
    assert episode(env, "ep-4")["state"] == "queued"


def test_refresh_keeps_queued_episodes_that_left_the_feed(env):
    jobs.enqueue(env.db, "process", [env.ids["ep-1"]], None)
    env.web.pages[URL] = rss(*(item(n) for n in range(2, 6)))
    assert feeds.refresh(env.db, env.pod).gone == 1
    assert episode(env, "ep-1") is not None


def test_episode_page_before_and_while_processing(env):
    page = env.client.get(f"/episodes/{env.ids['ep-1']}").text
    assert "Not downloaded yet" in page and "Notes for episode 1." in page
    jobs.enqueue(env.db, "process", [env.ids["ep-1"]], None)
    page = env.client.get(f"/episodes/{env.ids['ep-1']}").text
    assert "Queued" in page and 'hx-trigger="every 3s"' in page
    row = env.client.get(f"/episodes/{env.ids['ep-1']}/row").text
    assert "<li" in row
    assert 'hx-trigger="every 3s"' in row


def test_steps_on_companions_name_them(env):
    jobs.enqueue(env.db, "process", [env.ids["ep-1"]], None)
    job = jobs.claim(env.db)
    ctx = jobs.Context(env.db, job)
    ctx.step("Downloading", env.ids["ep-1"])
    assert env.db.execute("SELECT step FROM jobs").fetchone()[0] == "Downloading"
    ctx.step("Downloading", env.ids["ep-2"])
    assert env.db.execute("SELECT step FROM jobs").fetchone()[0] == "Downloading “Episode 2”"
    assert episode(env, "ep-2")["state_note"] == "Downloading"
