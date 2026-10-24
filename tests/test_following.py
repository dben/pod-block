"""Following feeds (podblock.app.following): reading them, adding and refreshing podcasts, and what a refresh
starts."""

import json

import pytest

from fakes import URL, item, rss
from podblock.app import following, library, text
from podblock.feed import parse_feed
from podblock.store import Store


def episode_titles(db, podcast_id):
    return [r["title"] for r in db.execute(
        "SELECT title FROM episodes WHERE podcast_id = ? ORDER BY pub_ts DESC", (podcast_id,))]


# ---- parsing ----------------------------------------------------------------

def test_feed_keeps_show_and_episode_metadata():
    feed = parse_feed(rss(item(1), item(2)), URL)
    assert (feed.title, feed.author, feed.language, feed.link) == (
        "Example Show", "Example Network", "en", "https://example.com/")
    assert feed.image_url == "https://example.com/art.jpg"
    assert feed.categories == ["Comedy", "Improv"]
    assert feed.explicit is True
    assert "<item" not in feed.xml and "itunes:author" in feed.xml

    newest = feed.episodes[0]
    assert newest.guid == "ep-2" and newest.number == 2
    assert newest.duration == 3723.0
    assert (newest.audio_type, newest.audio_length) == ("audio/mpeg", 2000)
    assert newest.xml.startswith("<item") and "podcast:chapters" in newest.xml  # prefixes survive


def test_show_notes_become_plain_text():
    assert text.html_to_text("<p>One &amp; <b>two</b></p><p>Three</p>") == "One & two\nThree"
    assert text.html_to_text("&lt;p&gt;Escaped twice&lt;/p&gt;") == "Escaped twice"
    assert text.html_to_text("<script>alert(1)</script>") == "alert(1)"
    assert len(following.summary("word " * 500)) <= following.SUMMARY_CHARS + 1


# ---- refreshing ---------------------------------------------------------------

def test_adding_a_feed_stores_it_all(db, web):
    web.pages[URL] = rss(item(1), item(2), item(3))
    result = following.add_podcast(db, URL)
    assert result.added == 3
    pod = db.execute("SELECT * FROM podcasts WHERE id = ?", (result.podcast_id,)).fetchone()
    assert pod["name"] == "Example Show" and json.loads(pod["categories"]) == ["Comedy", "Improv"]
    ep = db.execute("SELECT * FROM episodes WHERE guid = 'ep-3'").fetchone()
    assert ep["summary"] == "Notes for episode 3 & more. Second line."
    assert ep["feed_duration"] == 3723.0 and ep["item_xml"].startswith("<item")
    assert episode_titles(db, result.podcast_id) == ["Episode 3", "Episode 2", "Episode 1"]


def test_bad_feeds_are_refused(db, web):
    for url in ("ftp://example.com/x", "not a url", ""):
        with pytest.raises(following.RefreshError):
            following.add_podcast(db, url)
    web.pages[URL] = b"<html><body>not a feed</body></html>"
    with pytest.raises(following.RefreshError, match="RSS"):
        following.add_podcast(db, URL)
    with pytest.raises(following.RefreshError, match="404"):
        following.add_podcast(db, "https://example.com/missing.rss")
    assert db.execute("SELECT COUNT(*) FROM podcasts").fetchone()[0] == 0


def test_episodes_that_leave_the_feed_go_unless_downloaded(db, web):
    web.pages[URL] = rss(item(1), item(2), item(3))
    pod = following.add_podcast(db, URL).podcast_id
    db.execute("UPDATE episodes SET ever_downloaded = 1 WHERE guid = 'ep-1'")
    db.commit()

    web.pages[URL] = rss(item(3), item(4))
    result = following.refresh(db, pod)
    assert (result.added, result.removed, result.gone) == (1, 1, 1)
    rows = {r["guid"]: r["in_feed"] for r in db.execute("SELECT guid, in_feed FROM episodes")}
    assert rows == {"ep-1": 0, "ep-3": 1, "ep-4": 1}

    web.pages[URL] = rss(item(1), item(3), item(4))  # back in the feed
    following.refresh(db, pod)
    assert db.execute("SELECT in_feed FROM episodes WHERE guid = 'ep-1'").fetchone()[0] == 1


def test_episodes_the_detector_knows_count_as_downloaded(db, web):
    web.pages[URL] = rss(item(1), item(2))
    pod = following.add_podcast(db, URL).podcast_id
    ep = db.execute("SELECT id FROM episodes WHERE guid = 'ep-1'").fetchone()[0]
    db.execute("INSERT INTO reports VALUES (?, 0, '{}')", (ep,))
    db.commit()
    web.pages[URL] = rss(item(2))
    assert following.refresh(db, pod).gone == 1


def test_an_empty_feed_deletes_nothing(db, web):
    web.pages[URL] = rss(item(1), item(2))
    pod = following.add_podcast(db, URL).podcast_id
    web.pages[URL] = rss()
    assert following.refresh(db, pod).removed == 0
    assert len(episode_titles(db, pod)) == 2


def test_unchanged_feeds_cost_one_short_request(db, web):
    web.pages[URL] = rss(item(1))
    pod = following.add_podcast(db, URL).podcast_id
    assert following.refresh(db, pod).unchanged
    assert web.requests[-1][1] is not None  # sent the ETag


def test_failed_refresh_is_kept_on_the_podcast(db, web):
    web.pages[URL] = rss(item(1))
    pod = following.add_podcast(db, URL).podcast_id
    del web.pages[URL]
    with pytest.raises(following.RefreshError):
        following.refresh(db, pod, conditional=False)
    assert "404" in db.execute("SELECT last_error FROM podcasts").fetchone()[0]
    web.pages[URL] = rss(item(1))
    following.refresh(db, pod, conditional=False)
    assert db.execute("SELECT last_error FROM podcasts").fetchone()[0] is None


def test_another_shows_guid_does_not_steal_episodes(db, web):
    other = "https://example.org/other.rss"
    web.pages[URL] = rss(item(1))
    web.pages[other] = rss(item(1, guid="ep-1"), title="Other Show")
    a = following.add_podcast(db, URL).podcast_id
    b = following.add_podcast(db, other).podcast_id
    assert episode_titles(db, a) == ["Episode 1"] and episode_titles(db, b) == ["Episode 1"]
    following.refresh(db, a, conditional=False)
    following.refresh(db, b, conditional=False)
    assert db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 2


def test_refresh_due_skips_fresh_and_removed_feeds(db, web):
    web.pages[URL] = rss(item(1))
    pod = following.add_podcast(db, URL).podcast_id
    assert following.refresh_due(db) == []
    db.execute("UPDATE podcasts SET last_refreshed_at = 0")
    db.commit()
    assert following.refresh_due(db) == [pod]
    following.remove_podcast(db, pod)
    db.execute("UPDATE podcasts SET last_refreshed_at = 0")
    db.commit()
    assert following.refresh_due(db) == []


def test_the_detector_still_opens_a_store_the_app_has_filled(db, web, tmp_path):
    web.pages[URL] = rss(item(1), item(2))
    following.add_podcast(db, URL)
    store = Store(tmp_path / "podblock.sqlite")
    assert store.fingerprinted_episodes() == []
    assert store.podcast_id("Example Show", URL) == 1
    store.close()


def test_a_download_queues_only_the_podcasts_own_episodes(db, web):
    web.pages[URL] = rss(item(1), item(2))
    pod = following.add_podcast(db, URL).podcast_id
    web.pages["https://example.org/b.rss"] = rss(item(7, guid="b-7"), title="B")
    following.add_podcast(db, "https://example.org/b.rss")
    one, two, seven = (db.execute("SELECT id FROM episodes WHERE guid = ?", (g,)).fetchone()[0]
                       for g in ("ep-1", "ep-2", "b-7"))
    assert library.download(db, None, pod, [one, two, seven, one]) == ([one, two], 2)
    assert library.download(db, None, pod, [one]) == ([], 1)  # queued already
    db.execute("UPDATE jobs SET state = 'done'")
    db.execute("UPDATE episodes SET state = 'ready'")
    db.commit()
    assert library.download(db, None, pod, [one, two]) == ([], 2)  # ready: nothing to download
    assert library.download(db, None, pod, [one, two], again=True) == ([one, two], 2)


def test_podcast_settings_are_checked_and_say_when_the_gap_changed(db, web):
    web.pages[URL] = rss(item(1))
    pod = following.add_podcast(db, URL).podcast_id
    with pytest.raises(library.SettingsError):
        library.save_settings(db, pod, 9999, 60, 4, False, False)
    assert not library.save_settings(db, pod, 3, 60, 4, True, True)  # 4 seconds is the gap already
    assert library.save_settings(db, pod, 3, 60, 10, True, True)
    assert not library.save_settings(db, pod, 3, 60, 10, True, True, max_break=300)
    assert library.save_settings(db, pod, 3, 60, 10, True, True, max_break=420)
    with pytest.raises(library.SettingsError):
        library.save_settings(db, pod, 3, 60, 10, True, True, max_break=10)
    row = db.execute("SELECT * FROM podcasts WHERE id = ?", (pod,)).fetchone()
    assert (row["retention_days"], row["fill_gaps"], row["auto_download"], row["sort_oldest"],
            row["max_break"]) == (3, 10, 1, 1, 420)


def test_feeds_that_move_are_followed(db, web):
    web.pages[URL] = rss(item(1))
    pod = following.add_podcast(db, URL).podcast_id
    new = "https://example.org/moved.rss"
    web.moves[URL] = new  # a permanent redirect
    assert following.refresh(db, pod, conditional=False).moved_to == new
    assert db.execute("SELECT feed_url FROM podcasts").fetchone()[0] == new

    newer = "https://example.net/again.rss"
    web.pages[new] = rss(item(1), extra_channel=f"<itunes:new-feed-url>{newer}</itunes:new-feed-url>")
    following.refresh(db, pod, conditional=False)
    assert db.execute("SELECT feed_url FROM podcasts").fetchone()[0] == newer

    other = "https://example.com/other.rss"
    web.pages[other] = rss(item(9, guid="o-9"), title="Other")
    following.add_podcast(db, other)
    web.pages[newer] = rss(item(1), extra_channel=f"<itunes:new-feed-url>{other}</itunes:new-feed-url>")
    assert following.refresh(db, pod, conditional=False).moved_to is None  # that address is another podcast's
    assert db.execute("SELECT feed_url FROM podcasts WHERE id = ?", (pod,)).fetchone()[0] == newer


def test_new_episodes_download_on_their_own_when_asked(db, web):
    web.pages[URL] = rss(*(item(n) for n in range(1, 4)))
    pod = following.add_podcast(db, URL).podcast_id
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # adding a feed downloads nothing
    web.pages[URL] = rss(*(item(n) for n in range(1, 5)))
    assert following.refresh(db, pod).queued == 0  # off by default

    db.execute("UPDATE podcasts SET auto_download = 1")
    db.commit()
    web.pages[URL] = rss(*(item(n) for n in range(1, 13)))
    result = following.refresh(db, pod)
    assert result.added == 8 and result.queued == following.AUTO_DOWNLOAD_MAX
    queued = [r[0] for r in db.execute("SELECT e.guid FROM jobs j JOIN episodes e ON e.id = j.episode_id")]
    assert queued == [f"ep-{n}" for n in range(12, 7, -1)]  # the newest
    assert "5 queued to download" in result.describe()
