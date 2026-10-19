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
from podblock.web import accounts, background, feeds, jobs, library, processing
from podblock.web.app import create_app
from podblock.web.config import Settings
from podblock.web.db import connect

URL = "https://example.com/show.rss"
SR = 16000
JINGLE, AD, FILLER = 20.0, 18.0, 30.0


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
    library.subscribe(db, accounts.admin_id(db), pod)
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
    assert "Timeline" in report and 'id="timeline-data"' in report
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


# ---- timeline edits -------------------------------------------------------------

def processed(env):
    jobs.enqueue(env.db, "process", [env.ids["ep-2"]], None)
    background.run_queue(env.settings)
    return env.db.execute("SELECT * FROM jobs WHERE kind = 'process'").fetchone()


def report_of(env, guid):
    import json
    row = env.db.execute("SELECT report FROM reports WHERE episode_id = ?", (env.ids[guid],)).fetchone()
    return json.loads(row["report"])


def test_episode_page_has_a_timeline(env):
    processed(env)
    page = env.client.get(f"/episodes/{env.ids['ep-3']}").text
    assert 'id="timeline-data"' in page and "timeline.js" in page
    peaks = env.client.get(f"/episodes/{env.ids['ep-3']}/peaks").json()
    assert peaks["per_second"] == 10
    duration = episode(env, "ep-3")["duration"]
    assert abs(len(peaks["peaks"]) - duration * 10) < 20 and max(peaks["peaks"]) > 200


def test_marking_a_block_changes_it_everywhere_and_recuts(env):
    processed(env)
    first = report_of(env, "ep-3")["sections"][0]
    assert first["start"] < 0.5 and first["blocks"]
    before = {g: episode(env, g)["processed_at"] for g in ("ep-2", "ep-4", "ep-5")}

    res = env.client.post(f"/episodes/{env.ids['ep-3']}/edits",
                          json={"types": [{"type": "ignored", "blocks": first["blocks"], "bumpers": []}]})
    assert res.status_code == 200, res.text
    assert episode(env, "ep-3")["state"] == "queued"
    assert env.client.post(f"/episodes/{env.ids['ep-3']}/edits", json={"types": []}).status_code == 409  # busy

    background.run_queue(env.settings)
    types = dict(env.db.execute("SELECT id, type FROM segments").fetchall())
    assert all(types[b] == "ignored" for b in first["blocks"])
    for guid in ("ep-2", "ep-3", "ep-4", "ep-5"):
        ep = episode(env, guid)
        assert ep["state"] == "ready", (guid, ep["state_note"])
        assert ep["cut_duration"] == pytest.approx(JINGLE + 2 * FILLER, abs=3), guid  # the jingle stays now
        assert report_of(env, guid)["sections"][0]["start"] > 0
    assert all(episode(env, g)["processed_at"] > before[g] for g in before)


def test_cutting_a_stretch_by_hand(env):
    processed(env)
    ep_id = env.ids["ep-4"]
    full = episode(env, "ep-4")["cut_duration"]
    res = env.client.post(f"/episodes/{ep_id}/edits", json={"add": [{"start": 20, "end": 30, "as_block": False}]})
    assert res.status_code == 200
    background.run_queue(env.settings)
    assert episode(env, "ep-4")["cut_duration"] == pytest.approx(full - 10, abs=0.5)
    edit = env.db.execute("SELECT * FROM edits").fetchone()
    assert (edit["start"], edit["end"]) == (20, 30)
    assert env.db.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'process' AND state = 'done'").fetchone()[0] == 1

    page = env.client.get(f"/episodes/{ep_id}").text
    assert f'"edit": {edit["id"]}' in page or f'"edit":{edit["id"]}' in page
    env.client.post(f"/episodes/{ep_id}/edits", json={"remove": [edit["id"]]})
    background.run_queue(env.settings)
    assert episode(env, "ep-4")["cut_duration"] == pytest.approx(full, abs=0.5)


def test_a_section_saved_as_a_block_is_found_in_other_episodes(env, tmp_path):
    processed(env)
    blocks = env.db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    # a stretch of episode 4's own show audio: unique, so nothing else has it
    env.client.post(f"/episodes/{env.ids['ep-4']}/edits",
                    json={"add": [{"start": 20, "end": 35, "as_block": True, "type": "outro"}]})
    background.run_queue(env.settings)
    assert env.db.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == blocks + 1
    new = env.db.execute("SELECT * FROM segments ORDER BY id DESC LIMIT 1").fetchone()
    assert new["type"] == "outro" and new["type_manual"] == 1
    assert episode(env, "ep-4")["state"] == "ready"
    # the show's other ready episodes were checked again; they don't have it, so their cut files stay
    assert env.db.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'process' AND state = 'done'").fetchone()[0] == 4


def test_edits_that_make_no_sense_are_refused(env):
    processed(env)
    ep_id = env.ids["ep-3"]
    for body in ({}, {"types": [{"type": "intro", "blocks": [99999]}]}, {"add": [{"start": 5, "end": 5.5}]},
                 {"types": [{"type": "bogus", "blocks": report_of(env, "ep-3")["sections"][0]["blocks"]}]},
                 {"remove": [123]}):
        assert env.client.post(f"/episodes/{ep_id}/edits", json=body).status_code == 400, body
    assert env.client.post(f"/episodes/{ep_id}/edits", content=b"not json",
                           headers={"content-type": "application/json"}).status_code == 400
    assert episode(env, "ep-3")["state"] == "ready"


def test_intro_marks_only_the_block_that_ends_the_section(env):
    from podblock.web.processing import check_edits
    processed(env)
    ep_id = env.ids["ep-3"]
    plays = env.db.execute("SELECT segment_id, start, end FROM occurrences WHERE episode_id = ? ORDER BY start",
                           (ep_id,)).fetchall()
    blocks = [r["segment_id"] for r in plays]
    out = check_edits(env.db, ep_id, {"types": [{"type": "intro", "blocks": blocks, "start": 0, "end": 1e9}]})
    assert out["types"][0]["blocks"] == [max(plays, key=lambda r: r["end"])["segment_id"]]
    out = check_edits(env.db, ep_id, {"types": [{"type": "outro", "blocks": blocks, "start": 0, "end": 1e9}]})
    assert out["types"][0]["blocks"] == [plays[0]["segment_id"]]
    out = check_edits(env.db, ep_id, {"types": [{"type": "ignored", "blocks": blocks}]})
    assert sorted(out["types"][0]["blocks"]) == sorted(blocks)


# ---- the private feed --------------------------------------------------------------

def feed_of(env):
    token = env.db.execute("SELECT public_token FROM podcasts WHERE id = ?", (env.pod,)).fetchone()[0]
    return token, TestClient(env.client.app)  # no login


def test_private_feed_lists_ready_episodes_with_cut_audio(env):
    from podblock.feed import parse_feed
    import xml.etree.ElementTree as ET
    env.web.pages[URL] = env.web.pages[URL].replace(
        b"<description>Notes for episode 3.</description>",
        b'<description>Notes for episode 3.</description><podcast:chapters url="https://example.com/c.json" '
        b'type="application/json+chapters"/><media:content xmlns:media="http://search.yahoo.com/mrss/" '
        b'url="https://cdn.example.com/3.mp3" type="audio/mpeg"/><media:content xmlns:media="http://search.yahoo.com/mrss/" '
        b'url="https://example.com/3.jpg" medium="image"/>')
    feeds.refresh(env.db, env.pod, conditional=False)
    processed(env)
    token, anon = feed_of(env)
    res = anon.get(f"/p/{token}/feed.xml")
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/rss+xml")
    body = res.content
    assert b"ns0:" not in body and b"<itunes:block>Yes</itunes:block>" in body
    assert b"podcast:chapters" not in body
    assert b"https://cdn.example.com/" not in body  # nothing points at the uncut audio
    assert b"https://example.com/3.jpg" in body  # images stay
    assert f'/p/{token}/feed.xml'.encode() in body  # its own self link

    feed = parse_feed(body, "x")
    assert feed.title == "Example Show" and feed.author == "Example Network"
    assert [e.title for e in feed.episodes] == ["Episode 5", "Episode 4", "Episode 3", "Episode 2"]
    newest = feed.episodes[0]
    ep5 = episode(env, "ep-5")
    assert newest.guid == "ep-5" and newest.audio_url.startswith(f"http://testserver/p/{token}/")
    assert newest.audio_length == ep5["cut_bytes"] and newest.audio_type == "audio/mpeg"
    assert newest.duration == pytest.approx(ep5["cut_duration"], abs=1)
    assert ET.fromstring(newest.xml).findtext("description") == "Notes for episode 5."

    audio = anon.get(newest.audio_url.replace("http://testserver", ""), headers={"range": "bytes=0-9"})
    assert audio.status_code == 206 and len(audio.content) == 10
    assert anon.head(newest.audio_url.replace("http://testserver", "")).status_code == 200

    again = anon.get(f"/p/{token}/feed.xml", headers={"if-none-match": res.headers["etag"]})
    assert again.status_code == 304


def test_private_feed_is_hidden_behind_its_token(env):
    processed(env)
    token, anon = feed_of(env)
    for path in ("/p/nope/feed.xml", f"/p/{token}/0000000000000000.mp3", f"/p/{token[:-1]}/feed.xml"):
        res = anon.get(path)
        assert res.status_code == 404 and res.text == "Not found", path

    env.client.post(f"/podcasts/{env.pod}/feed-token")
    assert anon.get(f"/p/{token}/feed.xml").status_code == 404
    new, _ = feed_of(env)
    assert new != token and anon.get(f"/p/{new}/feed.xml").status_code == 200

    env.client.post(f"/podcasts/{env.pod}/remove")
    assert anon.get(f"/p/{new}/feed.xml").status_code == 404


def test_show_page_offers_the_private_feed(env):
    processed(env)
    token, _ = feed_of(env)
    page = env.client.get(f"/podcasts/{env.pod}").text
    assert f"http://testserver/p/{token}/feed.xml" in page and "<svg" in page and "4 episodes" in page


def test_public_url_setting_is_used_for_links(env):
    processed(env)
    env.settings.public_url = "https://pods.example.com"
    token, anon = feed_of(env)
    assert b"https://pods.example.com/p/" in anon.get(f"/p/{token}/feed.xml").content


# ---- retention ------------------------------------------------------------------------

def age(env, guid, days):
    env.db.execute("UPDATE episodes SET ready_at = ready_at - ? WHERE guid = ?", (days * 86400, guid))
    env.db.commit()


def test_downloads_are_cleaned_up_after_the_retention_period(env):
    from podblock.web import retention
    processed(env)
    ep = episode(env, "ep-3")
    files = [Path(ep[k]) for k in ("original_path", "cut_path", "peaks_path")]
    assert all(f.exists() for f in files) and ep["ready_at"]
    assert retention.expire_due(env.db) == []

    age(env, "ep-3", 8)
    assert retention.expire_due(env.db) == [env.ids["ep-3"]]
    ep = episode(env, "ep-3")
    assert ep["state"] == "expired" and ep["expired_at"] and not any(f.exists() for f in files)
    assert ep["cut_path"] is None and env.db.execute("SELECT 1 FROM reports WHERE episode_id = ?", (ep["id"],)).fetchone()

    token, anon = feed_of(env)
    assert b"Episode 3" not in anon.get(f"/p/{token}/feed.xml").content
    row = env.client.get(f"/podcasts/{env.pod}").text
    assert "Cleaned up" in row and "Download again" in row
    assert "Download again" in env.client.get(f"/episodes/{ep['id']}").text


def test_kept_episodes_and_keep_forever_podcasts_stay(env):
    from podblock.web import library, retention
    processed(env)
    for guid in ("ep-2", "ep-3", "ep-4"):
        age(env, guid, 30)
    env.client.post(f"/podcasts/{env.pod}/episodes", data={"action": "keep", "ids": env.ids["ep-2"]})
    other = env.db.execute("INSERT INTO users (username, created_at) VALUES ('guest', 0)").lastrowid
    env.db.commit()
    library.set_kept(env.db, other, env.pod, [env.ids["ep-3"]], True)  # kept by someone else counts too
    assert retention.expire_due(env.db) == [env.ids["ep-4"]]
    assert "Kept" in env.client.get(f"/podcasts/{env.pod}?view=downloaded").text

    env.client.post(f"/podcasts/{env.pod}/settings", data={"retention_days": 0, "refresh_minutes": 60})
    age(env, "ep-5", 400)
    assert retention.expire_due(env.db) == []
    env.client.post(f"/podcasts/{env.pod}/settings", data={"retention_days": 9999, "refresh_minutes": 60})
    assert env.db.execute("SELECT retention_days FROM podcasts").fetchone()[0] == 0  # out of range: unchanged


def test_ready_shows_when_it_will_be_cleaned_up(env):
    processed(env)
    page = env.client.get(f"/episodes/{env.ids['ep-3']}").text
    assert "cleaned up in 7 days" in page
    assert "Cleaned up in 7 days" in env.client.get(f"/podcasts/{env.pod}").text
    env.client.post(f"/episodes/{env.ids['ep-3']}/keep", data={"keep": "true"})
    assert "You're keeping it" in env.client.get(f"/episodes/{env.ids['ep-3']}").text


def test_processing_again_doesnt_restart_the_clock(env):
    processed(env)
    age(env, "ep-3", 3)
    before = episode(env, "ep-3")["ready_at"]
    env.client.post(f"/episodes/{env.ids['ep-3']}/process")
    background.run_queue(env.settings)
    assert episode(env, "ep-3")["ready_at"] == before


def test_a_cleaned_up_episode_downloads_again(env):
    from podblock.web import retention
    processed(env)
    age(env, "ep-3", 8)
    retention.expire_due(env.db)
    fetched = len(env.cdn.fetched)
    env.client.post(f"/podcasts/{env.pod}/episodes", data={"action": "download", "ids": env.ids["ep-3"]})
    background.run_queue(env.settings)
    ep = episode(env, "ep-3")
    assert ep["state"] == "ready" and Path(ep["cut_path"]).exists() and ep["expired_at"] is None
    assert len(env.cdn.fetched) == fetched + 1 and ep["ready_at"] > 0


def test_archiving_everything_cleaned_up(env):
    from podblock.web import retention
    processed(env)
    for guid in ("ep-2", "ep-3"):
        age(env, guid, 8)
    retention.expire_due(env.db)
    page = env.client.get(f"/podcasts/{env.pod}").text
    assert "2 episodes were downloaded and cleaned up since." in page
    assert "Archive 2 cleaned-up episodes" in env.client.get("/").text

    env.client.post(f"/podcasts/{env.pod}/episodes", data={"action": "archive_expired"})
    page = env.client.get(f"/podcasts/{env.pod}").text
    assert "cleaned up since" not in page and "Episode 3" not in page and "Episode 4" in page
    assert "Episode 3" in env.client.get(f"/podcasts/{env.pod}?view=archived").text

    age(env, "ep-4", 8)
    retention.expire_due(env.db)
    env.client.post("/episodes/archive-expired")
    assert "Episode 4" not in env.client.get(f"/podcasts/{env.pod}?view=downloaded").text


def test_leftover_downloads_of_failed_jobs_go_too(env):
    import os
    from podblock.web import retention
    env.cdn.fail.add("https://cdn.example.com/2.mp3")  # a companion fails after the chosen one downloaded
    jobs.enqueue(env.db, "process", [env.ids["ep-1"]], None)
    background.run_queue(env.settings)
    ep = episode(env, "ep-1")
    assert ep["state"] == "failed" and Path(ep["original_path"]).exists()
    old = Path(ep["original_path"]).stat().st_mtime - 8 * 86400
    os.utime(ep["original_path"], (old, old))
    retention.expire_due(env.db)
    assert episode(env, "ep-1")["original_path"] is None


def test_daily_prune_spares_fingerprints_in_use(env):
    import os
    from podblock.web import retention
    processed(env)
    fp = Path(episode(env, "ep-3")["fingerprint_path"])
    old = fp.stat().st_mtime - 90 * 86400
    os.utime(fp, (old, old))
    assert retention.prune_store(env.db, env.settings) is not None
    assert fp.exists()
    assert retention.prune_store(env.db, env.settings) is None  # once a day



# ---- more per-podcast settings ------------------------------------------------------

def test_a_wider_gap_joins_the_jingle_and_the_ad(env):
    env.client.post(f"/podcasts/{env.pod}/settings",
                    data={"retention_days": 7, "refresh_minutes": 60, "fill_gaps": 35})
    assert env.db.execute("SELECT fill_gaps FROM podcasts").fetchone()[0] == 35
    processed(env)
    first = report_of(env, "ep-3")["sections"][0]
    assert first["start"] < 0.5 and first["end"] == pytest.approx(JINGLE + FILLER + AD, abs=2)
    total = JINGLE + AD + 2 * FILLER
    assert episode(env, "ep-3")["cut_duration"] == pytest.approx(total - (JINGLE + FILLER + AD), abs=3)


def test_new_repeats_recut_the_shows_other_ready_episodes(env, monkeypatch):
    processed(env)
    jobs.enqueue(env.db, "process", [env.ids["ep-1"]], None)
    monkeypatch.setattr(processing, "find_common_blocks", lambda *a, **k: 1)  # as if it found one new block
    background.run_queue(env.settings)
    recut = [r["episode_id"] for r in env.db.execute(
        "SELECT episode_id FROM jobs WHERE kind = 'process' AND state = 'done' ORDER BY id")][2:]
    assert sorted(recut) == sorted(env.ids[g] for g in ("ep-2", "ep-3", "ep-4", "ep-5"))


def test_time_cut_is_shown(env):
    processed(env)
    assert "min cut" in env.client.get("/").text
    assert "cut so far" in env.client.get(f"/podcasts/{env.pod}").text
