import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("fastapi")
pytestmark = [pytest.mark.slow, pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")]

from fastapi.testclient import TestClient

from fakes import AD, FILLER, JINGLE, SR, URL, FakeWeb, fake_audio, rss
from podblock.app import accounts, chapters, following, jobs, library, media, private_feeds, processing, worker
from podblock.app.config import Settings
from podblock.app.db import connect
from podblock.cut import probe
from podblock.store import Store
from podblock.web.app import create_app


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
    extras = {}  # chapters and transcript files, by address
    monkeypatch.setattr(chapters, "get", lambda url: extras[url])  # a KeyError, like a 404, leaves one out
    settings = Settings(data_dir=tmp_path / "data", media_dir=tmp_path / "media", api_key="k", background=False)
    app = create_app(settings)
    client = TestClient(app, follow_redirects=False)
    client.post("/login", data={"key": "k"})
    db = connect(settings.db_path)
    pod = following.add_podcast(db, URL).podcast_id
    library.subscribe(db, accounts.admin_id(db), pod)
    ids = {r["guid"]: r["id"] for r in db.execute("SELECT id, guid FROM episodes")}
    yield type("Env", (), dict(settings=settings, client=client, db=db, pod=pod, ids=ids, cdn=cdn, web=web,
                               extras=extras))
    db.close()


def episode(env, guid):
    return env.db.execute("SELECT * FROM episodes WHERE guid = ?", (guid,)).fetchone()


def test_download_finds_repeats_and_cuts_them(env):
    res = env.client.post(f"/podcasts/{env.pod}/episodes", data={"action": "download", "ids": env.ids["ep-2"]})
    assert res.status_code == 303
    assert episode(env, "ep-2")["state"] == "queued"
    assert "Jobs <span class=\"nav-count\">1</span>" in env.client.get("/jobs").text

    assert worker.run_queue(env.settings) == 1

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
    worker.run_queue(env.settings)
    audio = env.client.get(f"/episodes/{env.ids['ep-5']}/audio", headers={"range": "bytes=0-99"})
    assert audio.status_code == 206 and len(audio.content) == 100
    assert audio.headers["content-type"] == "audio/mpeg"
    whole = env.client.get(f"/episodes/{env.ids['ep-5']}/audio?download=1")
    assert whole.headers["content-disposition"] == "attachment; filename*=utf-8''Episode%205.mp3"
    assert env.client.get(f"/episodes/{env.ids['ep-1']}/audio").status_code == 404
    assert TestClient(env.client.app).get(f"/episodes/{env.ids['ep-5']}/audio").status_code == 401


def test_processing_again_reuses_the_download(env):
    jobs.enqueue(env.db, "process", [env.ids["ep-3"]], None)
    worker.run_queue(env.settings)
    fetched = len(env.cdn.fetched)
    assert jobs.enqueue(env.db, "process", [env.ids["ep-3"]], None) == []  # ready: a plain download skips it
    env.client.post(f"/episodes/{env.ids['ep-3']}/process")
    worker.run_queue(env.settings)
    assert len(env.cdn.fetched) == fetched
    assert episode(env, "ep-3")["state"] == "ready"


def test_a_failed_download_can_be_retried(env):
    env.cdn.fail.add("https://cdn.example.com/1.mp3")
    jobs.enqueue(env.db, "process", [env.ids["ep-1"]], None)
    worker.run_queue(env.settings)
    ep = episode(env, "ep-1")
    assert ep["state"] == "failed" and "the CDN is down" in ep["state_note"]
    job = env.db.execute("SELECT * FROM jobs").fetchone()
    assert job["state"] == "failed" and "CDN" in job["error"]
    for n in (2, 3, 4):  # companions go back to rest
        assert episode(env, f"ep-{n}")["state"] is None
    assert "Retry" in env.client.get("/jobs").text

    env.cdn.fail.clear()
    env.client.post(f"/jobs/{job['id']}/retry")
    worker.run_queue(env.settings)
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

    monkeypatch.setitem(worker.RUNNERS, "process", cancel_midway)
    worker.run_queue(env.settings)
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
    assert following.refresh(env.db, env.pod).gone == 1
    assert episode(env, "ep-1") is not None


def test_episode_page_before_and_while_processing(env):
    page = env.client.get(f"/episodes/{env.ids['ep-1']}").text
    assert f'action="/episodes/{env.ids["ep-1"]}/process"' in page and "Notes for episode 1." in page
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
    worker.run_queue(env.settings)
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

    worker.run_queue(env.settings)
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
    worker.run_queue(env.settings)
    assert episode(env, "ep-4")["cut_duration"] == pytest.approx(full - 10, abs=0.5)
    edit = env.db.execute("SELECT * FROM edits").fetchone()
    assert (edit["start"], edit["end"]) == (20, 30)
    assert env.db.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'process' AND state = 'done'").fetchone()[0] == 1

    page = env.client.get(f"/episodes/{ep_id}").text
    assert f'"edit": {edit["id"]}' in page or f'"edit":{edit["id"]}' in page
    env.client.post(f"/episodes/{ep_id}/edits", json={"remove": [edit["id"]]})
    worker.run_queue(env.settings)
    assert episode(env, "ep-4")["cut_duration"] == pytest.approx(full, abs=0.5)


def test_changes_that_fail_partway_land_none_of_them(env, monkeypatch):
    processed(env)
    first = report_of(env, "ep-4")["sections"][0]

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(Store, "add_segment", broken)  # the last of the changes fails
    env.client.post(f"/episodes/{env.ids['ep-4']}/edits", json={
        "types": [{"type": "ignored", "blocks": first["blocks"], "bumpers": []}],
        "add": [{"start": 20, "end": 30, "as_block": False}, {"start": 40, "end": 50, "as_block": True}]})
    worker.run_queue(env.settings)
    job = env.db.execute("SELECT * FROM jobs WHERE kind = 'edit'").fetchone()
    assert job["state"] == "failed" and "disk full" in job["error"]
    types = dict(env.db.execute("SELECT id, type FROM segments").fetchall())
    assert not any(types[b] == "ignored" for b in first["blocks"])
    assert env.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0
    assert episode(env, "ep-4")["state"] == "ready"  # its cut from before stays


def test_a_section_saved_as_a_block_is_found_in_other_episodes(env, tmp_path):
    processed(env)
    blocks = env.db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    # a stretch of episode 4's own show audio: unique, so nothing else has it
    env.client.post(f"/episodes/{env.ids['ep-4']}/edits",
                    json={"add": [{"start": 20, "end": 35, "as_block": True, "type": "outro"}]})
    worker.run_queue(env.settings)
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
    # not shaped as the timeline sends them: refused before they are looked at
    for body in ({"add": [{"start": "soon", "end": 30}]}, {"types": [{"blocks": [1]}]}, {"remove": "all"}):
        res = env.client.post(f"/episodes/{ep_id}/edits", json=body)
        assert res.status_code == 422 and res.json()["error"].startswith("That request didn't make sense"), body
    res = env.client.post(f"/episodes/{ep_id}/edits", content=b"not json", headers={"content-type": "application/json"})
    assert res.status_code == 422 and "JSON" in res.json()["error"]
    assert episode(env, "ep-3")["state"] == "ready"


def test_intro_marks_only_the_block_that_ends_the_section(env):
    from podblock.app.processing import check_edits
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

def feed_of(env, user=None):
    """The token of a user's feed (the admin's by default), and a client with no login."""
    token = private_feeds.feed_token(env.db, user or accounts.admin_id(env.db), env.pod)
    return token, TestClient(env.client.app)


def audio_token(env):
    return env.db.execute("SELECT public_token FROM podcasts WHERE id = ?", (env.pod,)).fetchone()[0]


def test_private_feed_lists_ready_episodes_with_cut_audio(env):
    import xml.etree.ElementTree as ET

    from podblock.feed import parse_feed
    env.web.pages[URL] = env.web.pages[URL].replace(
        b"<description>Notes for episode 3.</description>",
        b'<description>Notes for episode 3.</description><podcast:chapters url="https://example.com/c.json" '
        b'type="application/json+chapters"/><media:content xmlns:media="http://search.yahoo.com/mrss/" '
        b'url="https://cdn.example.com/3.mp3" type="audio/mpeg"/>'
        b'<media:content xmlns:media="http://search.yahoo.com/mrss/" '
        b'url="https://example.com/3.jpg" medium="image"/>')
    following.refresh(env.db, env.pod, conditional=False)
    processed(env)
    token, anon = feed_of(env)
    res = anon.get(f"/p/{token}/feed.xml")
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/rss+xml")
    body = res.content
    assert b"ns0:" not in body and b"<itunes:block>Yes</itunes:block>" in body
    assert b"https://example.com/c.json" not in body  # the source's chapters no longer fit
    assert f'/p/{token}/{media.key("ep-3")}.chapters.json'.encode() in body  # ours do
    assert b"https://cdn.example.com/" not in body  # nothing points at the uncut audio
    assert b"https://example.com/3.jpg" in body  # images stay
    assert f'/p/{token}/feed.xml'.encode() in body  # its own self link

    feed = parse_feed(body, "x")
    assert feed.title == "Example Show" and feed.author == "Example Network"
    assert [e.title for e in feed.episodes] == ["Episode 5", "Episode 4", "Episode 3", "Episode 2"]
    newest = feed.episodes[0]
    ep5 = episode(env, "ep-5")
    assert newest.guid == "ep-5" and newest.audio_url.startswith(f"http://testserver/p/{token}/")  # whose it is
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
    name = audio_url(env, anon, token).rsplit("/", 1)[1]
    for path in ("/p/nope/feed.xml", f"/p/{audio_token(env)}/0000000000000000.mp3", f"/p/{token[:-1]}/feed.xml",
                 f"/p/{token}/{name[:-4]}.ogg", f"/p/{token}/{name}x", f"/p/{token[:-1]}/{name}",
                 f"/p/{audio_token(env)}/{name[:-4]}.chapters.json"):  # chapters only come through a feed
        res = anon.get(path)
        assert res.status_code == 404 and res.text == "Not found", path
    assert anon.head(f"/p/{audio_token(env)}/{name}").status_code == 200  # links from before feeds had their own

    audio = audio_url(env, anon, token)
    env.client.post(f"/podcasts/{env.pod}/feed-token")
    assert anon.get(f"/p/{token}/feed.xml").status_code == 404
    assert anon.head(audio).status_code == 404  # its audio links stop working too
    new, _ = feed_of(env)
    assert new != token and anon.get(f"/p/{new}/feed.xml").status_code == 200
    assert anon.head(audio_url(env, anon, new)).status_code == 200

    env.client.post(f"/podcasts/{env.pod}/remove")
    assert anon.get(f"/p/{new}/feed.xml").status_code == 404


def audio_url(env, anon, token):
    """The newest episode's audio link in a feed, as a path."""
    from podblock.feed import parse_feed
    feed = parse_feed(anon.get(f"/p/{token}/feed.xml").content, "x")
    return feed.episodes[0].audio_url.replace("http://testserver", "")


def test_each_user_has_their_own_feed(env):
    processed(env)
    env.client.post("/users", data={"username": "bob", "password": "password1"})
    bob = TestClient(env.client.app, follow_redirects=False)
    bob.post("/login", data={"username": "bob", "password": "password1"})
    bob_id = env.db.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0]
    mine, anon = feed_of(env)
    his, _ = feed_of(env, bob_id)
    assert mine != his and f"/p/{his}/feed.xml" in bob.get(f"/podcasts/{env.pod}?tab=feed").text

    def guids(token):
        from podblock.feed import parse_feed
        return [e.guid for e in parse_feed(anon.get(f"/p/{token}/feed.xml").content, "x").episodes]

    assert guids(mine) == guids(his) == ["ep-5", "ep-4", "ep-3", "ep-2"]
    bob.post(f"/podcasts/{env.pod}/episodes", data={"action": "archive", "ids": env.ids["ep-4"]})
    bob.post(f"/podcasts/{env.pod}/filters", data={"pattern": "episode 3"})
    assert guids(his) == ["ep-5", "ep-2"] and guids(mine) == ["ep-5", "ep-4", "ep-3", "ep-2"]
    assert audio_url(env, anon, mine) != audio_url(env, anon, his)  # each says whose feed it came through

    env.client.post(f"/podcasts/{env.pod}/filters", data={"pattern": "episode 5", "processing": "true"})
    assert guids(his) == ["ep-2"] and guids(mine) == ["ep-4", "ep-3", "ep-2"]
    assert anon.head(f"/p/{audio_token(env)}/{media.key('ep-5')}.mp3").status_code == 200

    bob.post(f"/podcasts/{env.pod}/feed-token")  # a new address for bob leaves the admin's alone
    assert anon.get(f"/p/{his}/feed.xml").status_code == 404 and anon.get(f"/p/{mine}/feed.xml").status_code == 200
    his, _ = feed_of(env, bob_id)
    env.client.post(f"/users/{bob_id}/remove")
    assert anon.get(f"/p/{his}/feed.xml").status_code == 404


def test_show_page_offers_the_private_feed(env):
    processed(env)
    token, _ = feed_of(env)
    page = env.client.get(f"/podcasts/{env.pod}?tab=feed").text
    assert f"http://testserver/p/{token}/feed.xml" in page and "<svg" in page


def test_public_url_setting_is_used_for_links(env):
    processed(env)
    env.settings.public_url = "https://pods.example.com"
    token, anon = feed_of(env)
    assert b"https://pods.example.com/p/" in anon.get(f"/p/{token}/feed.xml").content


def test_feed_items_get_the_shows_artwork_and_a_build_date(env):
    processed(env)
    token, anon = feed_of(env)
    body = anon.get(f"/p/{token}/feed.xml").content
    assert body.count(b'<itunes:image href="https://example.com/art.jpg"') == 1 + 4  # the channel's and each item's
    assert b"<generator>pod-block</generator>" in body and b"<lastBuildDate>" in body


def test_feeds_also_come_newest_downloaded_first(env):
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    from podblock.feed import parse_feed
    processed(env)
    for n, guid in enumerate(["ep-5", "ep-3", "ep-4", "ep-2"]):  # downloaded in this order, an hour apart
        env.db.execute("UPDATE episodes SET ready_at = ? WHERE guid = ?", (1_700_000_000 + n * 3600, guid))
    env.db.commit()
    token, anon = feed_of(env)
    released = anon.get(f"/p/{token}/feed.xml").content
    res = anon.get(f"/p/{token}/downloaded.xml")
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/rss+xml")
    assert f"/p/{token}/downloaded.xml".encode() in res.content  # its own self link
    feed = parse_feed(res.content, "x")
    assert [e.guid for e in feed.episodes] == ["ep-2", "ep-4", "ep-3", "ep-5"]
    dates = [ET.fromstring(e.xml).findtext("pubDate") for e in feed.episodes]
    assert [parsedate_to_datetime(d).timestamp() for d in dates] == [1_700_000_000 + n * 3600 for n in (3, 2, 1, 0)]
    assert [e.guid for e in parse_feed(released, "x").episodes] == ["ep-5", "ep-4", "ep-3", "ep-2"]  # unchanged

    page = env.client.get(f"/podcasts/{env.pod}?tab=feed").text
    assert f"http://testserver/p/{token}/feed.xml" in page and f"http://testserver/p/{token}/downloaded.xml" in page
    assert page.count("<svg") >= 2 and "By download date" in page

    omni_token = private_feeds.omni_token(env.db, accounts.admin_id(env.db))
    assert f"http://testserver/p/{omni_token}/downloaded.xml" in env.client.get("/").text
    feed = parse_feed(anon.get(f"/p/{omni_token}/downloaded.xml").content, "x")
    assert [e.guid for e in feed.episodes] == ["ep-2", "ep-4", "ep-3", "ep-5"]

    env.client.post(f"/podcasts/{env.pod}/feed-token")  # a new address replaces both
    assert anon.get(f"/p/{token}/downloaded.xml").status_code == 404


def omni(env, anon, token):
    import xml.etree.ElementTree as ET
    res = anon.get(f"/p/{token}/feed.xml")
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/rss+xml")
    return ET.fromstring(res.content).find("channel")


def test_omni_feed_has_every_downloaded_episode_of_your_podcasts(env):
    from podblock.feed import ITUNES, parse_feed
    processed(env)
    env.web.pages["https://example.org/b.rss"] = rss(
        item(6).replace("Episode 6", "B one").replace("ep-6", "b-1").replace("6.mp3", "1.mp3").replace(
            "</item>", '<itunes:title> B one </itunes:title><itunes:image href=""/></item>'), title="Show B")
    b = following.add_podcast(env.db, "https://example.org/b.rss").podcast_id
    b1 = env.db.execute("SELECT id FROM episodes WHERE guid = 'b-1'").fetchone()[0]
    jobs.enqueue(env.db, "process", [b1], None)
    worker.run_queue(env.settings)
    assert episode(env, "b-1")["state"] == "ready"

    home = env.client.get("/").text
    token = re.search(r'value="http://testserver/p/([^/"]+)/feed\.xml"', home).group(1)
    assert "Omni feed" in home
    anon = TestClient(env.client.app)
    channel = omni(env, anon, token)
    assert channel.findtext("title") == "Pod Block Omni Feed"
    assert channel.find(f"{ITUNES}image").get("href") == "http://testserver/static/omni.jpg"
    assert anon.get("/static/omni.jpg").headers["content-type"] == "image/jpeg"
    assert channel.findtext(f"{ITUNES}block") == "Yes"
    assert [i.findtext("title") for i in channel.findall("item")] == [  # Show B isn't on the admin's page yet
        f"Example Show: Episode {n}" for n in (5, 4, 3, 2)]

    env.client.post(f"/podcasts/{b}/subscribe", data={"on": "true"})
    env.client.post(f"/podcasts/{env.pod}/episodes", data={"action": "archive", "ids": env.ids["ep-4"]})
    feed = parse_feed(anon.get(f"/p/{token}/feed.xml").content, "x")
    assert [e.guid for e in feed.episodes] == ["b-1", "ep-5", "ep-3", "ep-2"]
    assert feed.episodes[0].title == "Show B: B one" and feed.episodes[0].audio_type == "audio/mpeg"
    items = omni(env, anon, token).findall("item")
    assert [i.findtext(f"{ITUNES}title") for i in items] == [
        "Show B: B one", *(f"Example Show: Episode {n}" for n in (5, 3, 2))]  # apps show this one first
    assert [i.find(f"{ITUNES}image").get("href") for i in items] == ["https://example.com/art.jpg"] * 4
    audio = feed.episodes[0].audio_url.replace("http://testserver", "")
    assert anon.get(audio, headers={"range": "bytes=0-9"}).status_code == 206
    assert channel.find("item").findtext(f"{ITUNES}author") == "Example Network"

    env.client.post("/omni-token")
    assert anon.get(f"/p/{token}/feed.xml").status_code == 404
    new = re.search(r'value="http://testserver/p/([^/"]+)/feed\.xml"', env.client.get("/?omni=1").text).group(1)
    assert new != token and omni(env, anon, new).findtext("title") == "Pod Block Omni Feed"


def test_each_user_has_their_own_omni_feed(env):
    processed(env)
    env.client.post("/users", data={"username": "bob", "password": "password1"})
    bob_id = env.db.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0]
    anon = TestClient(env.client.app)
    his = private_feeds.omni_token(env.db, bob_id)
    assert omni(env, anon, his).findall("item") == []  # no podcasts on his page yet
    library.subscribe(env.db, bob_id, env.pod)
    assert len(omni(env, anon, his).findall("item")) == 4
    env.client.post(f"/users/{bob_id}/remove")
    assert anon.get(f"/p/{his}/feed.xml").status_code == 404


# ---- downloads ------------------------------------------------------------------------

def test_downloads_page_shows_sizes_and_deletes(env):
    from podblock.feed import parse_feed
    from podblock.web.templating import filesize
    processed(env)
    page = env.client.get("/downloads").text
    sizes = [sum(Path(p).stat().st_size for p in (ep["original_path"], ep["cut_path"], ep["peaks_path"]) if p)
             for ep in env.db.execute("SELECT * FROM episodes WHERE state = 'ready'")]
    assert "in 4 episodes" in page and filesize(sum(sizes)) in page
    assert page.count('class="ep"') == 4 and "Example Show" in page

    three = episode(env, "ep-3")
    files = [Path(three[k]) for k in ("original_path", "cut_path")]
    res = env.client.post("/downloads/delete", data={"ids": [three["id"]]})
    assert res.status_code == 303 and res.headers["location"] == "/downloads"
    assert not any(f.exists() for f in files)
    gone = episode(env, "ep-3")
    assert gone["state"] == "expired" and gone["cut_path"] is None and gone["expired_at"]
    token, anon = feed_of(env)
    assert "ep-3" not in [e.guid for e in parse_feed(anon.get(f"/p/{token}/feed.xml").content, "x").episodes]
    assert "in 3 episodes" in env.client.get("/downloads").text

    jobs.enqueue(env.db, "process", [env.ids["ep-3"]], None)  # being processed: left alone
    env.client.post("/downloads/delete", data={"ids": [env.ids["ep-3"], env.ids["ep-4"]]})
    assert episode(env, "ep-3")["state"] == "queued" and episode(env, "ep-4")["state"] == "expired"


# ---- retention ------------------------------------------------------------------------

def age(env, guid, days):
    env.db.execute("UPDATE episodes SET ready_at = ready_at - ? WHERE guid = ?", (days * 86400, guid))
    env.db.commit()


def test_downloads_are_cleaned_up_after_the_retention_period(env):
    from podblock.app import retention
    processed(env)
    ep = episode(env, "ep-3")
    files = [Path(ep[k]) for k in ("original_path", "cut_path", "peaks_path")]
    assert all(f.exists() for f in files) and ep["ready_at"]
    assert retention.expire_due(env.db) == []

    age(env, "ep-3", 8)
    assert retention.expire_due(env.db) == [env.ids["ep-3"]]
    ep = episode(env, "ep-3")
    assert ep["state"] == "expired" and ep["expired_at"] and not any(f.exists() for f in files)
    assert ep["cut_path"] is None
    assert env.db.execute("SELECT 1 FROM reports WHERE episode_id = ?", (ep["id"],)).fetchone()

    token, anon = feed_of(env)
    assert b"Episode 3" not in anon.get(f"/p/{token}/feed.xml").content
    row = env.client.get(f"/podcasts/{env.pod}").text
    assert "Cleaned up" in row and "Download again" in row
    assert "Download again" in env.client.get(f"/episodes/{ep['id']}").text


def test_kept_episodes_and_keep_forever_podcasts_stay(env):
    from podblock.app import library, retention
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


def test_episodes_nobody_wants_are_cleaned_up_early(env):
    from podblock.app import filters, retention
    processed(env)
    admin = accounts.admin_id(env.db)
    bob = env.db.execute("INSERT INTO users (username, created_at) VALUES ('bob', 0)").lastrowid
    env.db.commit()
    library.subscribe(env.db, bob, env.pod)
    library.set_archived(env.db, admin, env.pod, [env.ids[g] for g in ("ep-2", "ep-3", "ep-4", "ep-5")], True)
    library.set_archived(env.db, bob, env.pod, [env.ids["ep-3"], env.ids["ep-5"]], True)
    filters.add(env.db, bob, env.pod, "episode 4$", False)  # filtered out counts as archived
    library.set_kept(env.db, bob, env.pod, [env.ids["ep-5"]], True)  # kept still stays
    assert retention.expire_due(env.db) == []  # a day to play one first
    for guid in ("ep-2", "ep-3", "ep-4", "ep-5"):
        age(env, guid, 2)  # well inside the podcast's 7 days
    assert sorted(retention.expire_due(env.db)) == sorted([env.ids["ep-3"], env.ids["ep-4"]])
    assert episode(env, "ep-2")["state"] == "ready"  # bob still has it in his list

    library.subscribe(env.db, bob, env.pod, False)  # only those with the podcast on their home page count
    env.client.post(f"/podcasts/{env.pod}/settings", data={"retention_days": 0, "refresh_minutes": 60})
    assert retention.expire_due(env.db) == [env.ids["ep-2"]]  # even when the podcast keeps downloads for good


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
    worker.run_queue(env.settings)
    assert episode(env, "ep-3")["ready_at"] == before


def test_a_cleaned_up_episode_downloads_again(env):
    from podblock.app import retention
    processed(env)
    age(env, "ep-3", 8)
    retention.expire_due(env.db)
    fetched = len(env.cdn.fetched)
    env.client.post(f"/podcasts/{env.pod}/episodes", data={"action": "download", "ids": env.ids["ep-3"]})
    worker.run_queue(env.settings)
    ep = episode(env, "ep-3")
    assert ep["state"] == "ready" and Path(ep["cut_path"]).exists() and ep["expired_at"] is None
    assert len(env.cdn.fetched) == fetched + 1 and ep["ready_at"] > 0


def test_archiving_everything_cleaned_up(env):
    from podblock.app import retention
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

    from podblock.app import retention
    env.cdn.fail.add("https://cdn.example.com/2.mp3")  # a companion fails after the chosen one downloaded
    jobs.enqueue(env.db, "process", [env.ids["ep-1"]], None)
    worker.run_queue(env.settings)
    ep = episode(env, "ep-1")
    assert ep["state"] == "failed" and Path(ep["original_path"]).exists()
    old = Path(ep["original_path"]).stat().st_mtime - 8 * 86400
    os.utime(ep["original_path"], (old, old))
    retention.expire_due(env.db)
    assert episode(env, "ep-1")["original_path"] is None


def test_daily_prune_spares_fingerprints_in_use(env):
    import os

    from podblock.app import retention
    processed(env)
    fp = Path(episode(env, "ep-3")["fingerprint_path"])
    old = fp.stat().st_mtime - 90 * 86400
    os.utime(fp, (old, old))
    assert retention.prune_store(env.db) is not None
    assert fp.exists()
    assert retention.prune_store(env.db) is None  # once a day



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
    worker.run_queue(env.settings)
    recut = [r["episode_id"] for r in env.db.execute(
        "SELECT episode_id FROM jobs WHERE kind = 'process' AND state = 'done' ORDER BY id")][2:]
    assert sorted(recut) == sorted(env.ids[g] for g in ("ep-2", "ep-3", "ep-4", "ep-5"))


def test_time_cut_is_shown(env):
    processed(env)
    assert "min cut" in env.client.get("/").text
    assert "cut so far" in env.client.get(f"/podcasts/{env.pod}").text


@pytest.fixture
def rewind(env, tmp_path):
    """Episode 5 becomes a rewind: the jingle, the first half of episode 2 played again, and a new bit."""
    samples = np.concatenate([fake_audio(JINGLE, 99), fake_audio(FILLER, 2), fake_audio(FILLER, 500)])
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    path = tmp_path / "rewind.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(path)], input=pcm, check=True)
    env.cdn.files = {**env.cdn.files, "https://cdn.example.com/5.mp3": path}
    return env


def blocks(env):
    return env.db.execute("SELECT COUNT(*) FROM segments").fetchone()[0]


def test_a_processing_filter_keeps_an_episode_out_of_finding_repeats(rewind):
    env = rewind
    env.client.post(f"/podcasts/{env.pod}/filters", data={"pattern": "^episode 5$", "processing": "true"})
    jobs.enqueue(env.db, "process", [env.ids["ep-2"]], None)
    worker.run_queue(env.settings)
    assert "https://cdn.example.com/5.mp3" not in env.cdn.fetched  # never taken along to compare with
    assert episode(env, "ep-5")["state"] is None
    assert episode(env, "ep-2")["cut_duration"] == pytest.approx(2 * FILLER, abs=3)  # only the jingle and the ad

    known = blocks(env)
    env.client.post(f"/podcasts/{env.pod}/episodes", data={"action": "download", "ids": env.ids["ep-5"]})
    worker.run_queue(env.settings)
    ep = episode(env, "ep-5")
    assert ep["state"] == "ready" and ep["cut_duration"] == pytest.approx(2 * FILLER, abs=3)  # the jingle is cut
    assert blocks(env) == known  # and nothing was learned from it
    assert "filter from processing matches this episode" in env.client.get(f"/episodes/{env.ids['ep-5']}").text


def test_filtering_an_episode_forgets_what_it_taught(rewind):
    env = rewind
    jobs.enqueue(env.db, "process", [env.ids["ep-2"]], None)
    worker.run_queue(env.settings)
    assert episode(env, "ep-5")["state"] == "ready"
    # the rewind made its first half a repeat
    assert episode(env, "ep-2")["cut_duration"] == pytest.approx(FILLER, abs=3)

    env.client.post(f"/podcasts/{env.pod}/filters", data={"pattern": "^episode 5$", "processing": "true"})
    job = env.db.execute("SELECT * FROM jobs WHERE kind = 'unlearn'").fetchone()
    assert json.loads(job["payload"]) == {"episodes": [env.ids["ep-5"]]}
    assert "Forget repeats found in filtered episodes" in env.client.get("/jobs").text
    worker.run_queue(env.settings)

    assert episode(env, "ep-2")["cut_duration"] == pytest.approx(2 * FILLER, abs=3)  # its first half is back
    assert episode(env, "ep-5")["cut_duration"] == pytest.approx(2 * FILLER, abs=3)  # still loses the jingle
    assert env.db.execute("SELECT COUNT(*) FROM compared_pairs WHERE a_id = ? OR b_id = ?",
                          (env.ids["ep-5"], env.ids["ep-5"])).fetchone()[0] == 0
    for n in (1, 3, 4):
        assert episode(env, f"ep-{n}")["cut_duration"] == pytest.approx(2 * FILLER, abs=3)


# ---- chapters and transcripts ---------------------------------------------------------

def with_extras(env):
    """Episode 3 gets chapters and a WebVTT transcript in the feed. Each episode is a jingle (0-20 s), show,
    an ad (50-68 s) and more show; the jingle and the ad are cut."""
    env.extras["https://example.com/3.json"] = json.dumps({"version": "1.2.0", "chapters": [
        {"startTime": 0, "title": "Welcome"}, {"startTime": 25, "title": "Main"},
        {"startTime": 52, "title": "Sponsor"}, {"startTime": 66, "title": "More"}]}).encode()
    env.extras["https://example.com/3.vtt"] = (
        b"WEBVTT\n\n00:00:21.000 --> 00:00:24.000\nShow talk\n\n"
        b"00:00:55.000 --> 00:00:59.000\nAn ad read\n\n00:01:20.000 --> 00:01:22.000\nAfter the ad\n")
    env.web.pages[URL] = env.web.pages[URL].replace(
        b"<description>Notes for episode 3.</description>",
        b'<description>Notes for episode 3.</description>'
        b'<podcast:chapters url="https://example.com/3.json" type="application/json+chapters"/>'
        b'<podcast:transcript url="https://example.com/3.vtt" type="text/vtt" language="en" rel="captions"/>'
        b'<podcast:transcript url="https://example.com/3.html" type="text/html"/>'
        b'<podcast:transcript url="https://example.com/missing.srt" type="application/x-subrip"/>')
    following.refresh(env.db, env.pod, conditional=False)


def test_chapters_and_transcripts_fit_the_cut_audio(env):
    import xml.etree.ElementTree as ET

    from podblock.cut import chapters as file_chapters
    from podblock.feed import PODCAST
    with_extras(env)
    processed(env)
    three = episode(env, "ep-3")
    shift = chapters.Shift(json.loads(three["cut_spec"]))
    assert len(shift.joins()) == 2  # the jingle and the ad

    found = json.loads(three["cut_chapters"])
    titles = [c["title"] for c in found]
    assert titles[0].startswith("Welcome (after 20 sec repeat cut") and titles[1] == "Main"
    assert "Sponsor" not in titles  # it was all inside the ad
    assert re.fullmatch(r"More \(after 1[78] sec repeat cut\)", titles[2]) and len(titles) == 3
    assert found[1]["start"] == pytest.approx(shift(25)) and found[2]["start"] == pytest.approx(shift.joins()[1][0])
    in_file = file_chapters(Path(three["cut_path"]))
    assert [c["title"] for c in in_file] == titles  # written into the cut file too
    assert [c["start"] for c in in_file] == pytest.approx([c["start"] for c in found], abs=0.01)

    token, anon = feed_of(env)
    item = next(i for i in ET.fromstring(anon.get(f"/p/{token}/feed.xml").content).iter("item")
                if i.findtext("guid") == "ep-3")
    served = anon.get(item.find(f"{PODCAST}chapters").get("url").replace("http://testserver", ""))
    assert served.status_code == 200 and [c["title"] for c in served.json()["chapters"]] == titles
    links = item.findall(f"{PODCAST}transcript")
    assert [(t.get("type"), t.get("language"), t.get("rel")) for t in links] == [("text/vtt", "en", "captions")]
    vtt = anon.get(links[0].get("url").replace("http://testserver", ""))
    assert vtt.status_code == 200 and vtt.headers["content-type"].startswith("text/vtt")
    cues = re.findall(r"(\S+) --> (\S+)\n(.+)", vtt.text)
    assert [c[2] for c in cues] == ["Show talk", "After the ad"]
    assert chapters._clock(cues[0][0]) == pytest.approx(shift(21), abs=0.01)
    assert chapters._clock(cues[1][0]) == pytest.approx(shift(80), abs=0.01)
    assert anon.get(links[0].get("url").replace("http://testserver", "").replace(".vtt", ".srt")).status_code == 404

    # Episodes without chapters of their own get one at each cut.
    two = json.loads(episode(env, "ep-2")["cut_chapters"])
    assert [c["start"] for c in two] == pytest.approx([0, shift.joins()[1][0]], abs=2)
    assert all(re.fullmatch(r"\d+ sec repeat cut", c["title"]) for c in two)

    # Cleaning up deletes the saved transcript, and the next download looks for them all again.
    saved = Path(json.loads(three["transcripts"])[0]["path"])
    assert saved.exists()
    env.client.post("/downloads/delete", data={"ids": [three["id"]]})
    assert not saved.exists() and episode(env, "ep-3")["source_chapters"] is None


def test_episodes_cut_before_chapters_get_them_when_cut_again(env):
    processed(env)
    env.db.execute("UPDATE episodes SET cut_chapters = NULL, source_chapters = NULL")  # as cut by an older version
    env.db.commit()
    two = episode(env, "ep-2")
    token, anon = feed_of(env)
    name = audio_url(env, anon, token).rsplit("/", 1)[1].replace(media.key("ep-5"),
                                                                   media.key("ep-2"))
    served = anon.get(f"/p/{token}/{name[:-4]}.chapters.json").json()["chapters"]  # worked out from the report
    assert len(served) == 2 and served[1]["title"].endswith("repeat cut")
    jobs.enqueue(env.db, "process", [two["id"]], None, skip_ready=False)
    worker.run_queue(env.settings)
    two = episode(env, "ep-2")
    assert [c["title"] for c in json.loads(two["cut_chapters"])] == [c["title"] for c in served]
    assert len(probe_chapters(two)) == 2


def probe_chapters(ep):
    from podblock.cut import chapters as file_chapters
    return file_chapters(Path(ep["cut_path"]))


# ---- grabs ------------------------------------------------------------------------------

def grab_rows(env):
    return [tuple(r) for r in env.db.execute("SELECT user_id, episode_id, via, hits FROM grabs ORDER BY id")]


def test_fetching_through_a_feed_counts_as_a_grab(env):
    processed(env)
    admin = accounts.admin_id(env.db)
    token, anon = feed_of(env)
    audio = audio_url(env, anon, token)
    five = env.ids["ep-5"]
    assert anon.head(audio).status_code == 200 and grab_rows(env) == []  # a HEAD isn't a grab
    anon.get(audio, headers={"range": "bytes=0-1"})  # a player streams it...
    anon.get(audio)  # ...while a download runs alongside
    anon.get(audio, headers={"range": "bytes=5000-"})
    assert grab_rows(env) == [(admin, five, "feed", 3)]

    env.db.execute("UPDATE grabs SET last_at = last_at - 86400")  # a day later
    env.db.commit()
    anon.get(audio, headers={"range": "bytes=9000-"})  # a seek: the same listen
    assert grab_rows(env) == [(admin, five, "feed", 4)]
    env.db.execute("UPDATE grabs SET last_at = last_at - 86400")
    env.db.commit()
    anon.get(audio)  # from the start again
    assert grab_rows(env) == [(admin, five, "feed", 4), (admin, five, "feed", 1)]
    anon.get(f"/p/{token}/{audio.rsplit('/', 1)[1][:-4]}.chapters.json")  # chapters aren't grabs
    anon.get(f"/p/{audio_token(env)}/{audio.rsplit('/', 1)[1]}")  # links from before then say nothing of whose
    assert len(grab_rows(env)) == 2

    omni_token = private_feeds.omni_token(env.db, admin)
    from podblock.feed import parse_feed
    omni_audio = parse_feed(anon.get(f"/p/{omni_token}/feed.xml").content, "x").episodes[-1].audio_url
    assert f"/p/{omni_token}/" in omni_audio
    anon.get(omni_audio.replace("http://testserver", ""))
    assert grab_rows(env)[-1] == (admin, env.ids["ep-2"], "omni", 1)


def test_grabs_show_in_the_list_with_a_view_of_their_own(env):
    processed(env)
    env.client.post("/users", data={"username": "bob", "password": "password1"})
    bob_id = env.db.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0]
    bob = TestClient(env.client.app, follow_redirects=False)
    bob.post("/login", data={"username": "bob", "password": "password1"})
    mine, anon = feed_of(env)
    anon.get(audio_url(env, anon, mine))  # the admin's app gets episode 5
    his, _ = feed_of(env, bob_id)
    for _ in range(2):
        anon.get(audio_url(env, anon, his))  # bob's gets it twice, a day apart
        env.db.execute("UPDATE grabs SET last_at = last_at - 86400")
        env.db.commit()

    page = env.client.get(f"/podcasts/{env.pod}").text
    assert page.count("badge-grab") == 1
    assert "Grabbed</span>" in page and "Grabbed ×2" not in page
    assert "Grabbed ×2" in bob.get(f"/podcasts/{env.pod}").text
    grabbed = env.client.get(f"/podcasts/{env.pod}?grabbed=1").text
    assert grabbed.count('class="ep') - grabbed.count('class="ep-') == 1 and "Episode 5" in grabbed
    assert 'data-count="active">1<' in grabbed
    env.client.post(f"/podcasts/{env.pod}/episodes", data={"action": "archive", "ids": env.ids["ep-5"]})
    assert "Nothing grabbed yet" in env.client.get(f"/podcasts/{env.pod}?view=grabbed").text
    assert "Grabbed ×2" in bob.get(f"/podcasts/{env.pod}?view=grabbed").text  # archiving is per user
    users = env.client.get("/users").text
    assert "1 grab<" in users and "2 grabs<" in users


def test_playing_or_downloading_on_the_episode_page_counts(env):
    processed(env)
    two = env.ids["ep-2"]
    page = env.client.get(f"/episodes/{two}").text
    needed = re.findall(r'data-grab="/episodes/\d+/played" data-grab-after="(\d+)"', page)
    short = episode(env, "ep-2")["cut_duration"]  # about a minute: it counts once 90% of it has played
    assert needed == [str(round(0.9 * short))] and short < 300  # the timeline's player doesn't count
    assert env.client.post(f"/episodes/{two}/played").json() == {"new": True}
    assert env.client.post(f"/episodes/{two}/played").json() == {"new": False}  # the same listen
    assert env.client.get(f"/episodes/{two}/audio").status_code == 200 and len(grab_rows(env)) == 1  # the player
    env.db.execute("UPDATE grabs SET last_at = last_at - 86400")
    env.db.commit()
    assert env.client.get(f"/episodes/{two}/audio?download=1").status_code == 200
    assert [r[2:] for r in grab_rows(env)] == [("app", 2), ("download", 1)]
    assert "Grabbed ×2" in env.client.get(f"/episodes/{two}").text


def test_the_home_page_shows_the_space_downloads_take(env):
    processed(env)
    page = env.client.get("/").text
    assert "used by downloads" in page and 'href="/downloads" title="See every download"' in page
    env.client.post("/users", data={"username": "bob", "password": "password1"})
    bob = TestClient(env.client.app, follow_redirects=False)
    bob.post("/login", data={"username": "bob", "password": "password1"})
    page = bob.get("/").text
    assert "used by downloads" in page and 'href="/downloads"' not in page  # the page is the admin's
