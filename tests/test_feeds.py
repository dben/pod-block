import json
import re

import pytest

from podblock.feed import Fetched, parse_feed

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from podblock.store import Store
from podblock.web import feeds, library
from podblock.web.app import create_app
from podblock.web.config import Settings
from podblock.web.db import connect, prepare

HTML = {"accept": "text/html"}
JSON = {"accept": "application/json"}
URL = "https://example.com/show.rss"


def item(n, day=None, guid=None, extra=""):
    day = day or n
    return f"""
    <item>
      <title>Episode {n}</title>
      <guid isPermaLink="false">{guid or f"ep-{n}"}</guid>
      <pubDate>{day:02d} Jan 2026 10:00:00 +0000</pubDate>
      <enclosure url="https://cdn.example.com/{n}.mp3" type="audio/mpeg" length="{n * 1000}"/>
      <description><![CDATA[<p>Notes for <b>episode {n}</b> &amp; more.</p><p>Second line.</p>]]></description>
      <itunes:duration>1:02:03</itunes:duration>
      <itunes:episode>{n}</itunes:episode>
      <podcast:chapters url="https://example.com/{n}.json" type="application/json+chapters"/>
      {extra}
    </item>"""


def rss(*items, title="Example Show", extra_channel=""):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{title}</title>
    <link>https://example.com/</link>
    <language>en</language>
    <description>A show about &lt;b&gt;examples&lt;/b&gt;.</description>
    <itunes:author>Example Network</itunes:author>
    <itunes:image href="https://example.com/art.jpg"/>
    <itunes:category text="Comedy"><itunes:category text="Improv"/></itunes:category>
    <itunes:explicit>yes</itunes:explicit>
    {extra_channel}
    {"".join(items)}
  </channel>
</rss>""".encode()


class FakeWeb:
    """Stands in for the network: serves whatever XML a test puts at a URL."""

    def __init__(self, monkeypatch):
        self.pages: dict[str, bytes] = {}
        self.requests: list[tuple] = []
        self.moves: dict[str, str] = {}  # url -> where a permanent redirect ends up
        monkeypatch.setattr(feeds, "get_feed", self.get)

    def get(self, url, etag=None, last_modified=None):
        self.requests.append((url, etag))
        if url not in self.pages:
            raise feeds.RefreshError("the server answered 404")
        body = self.pages[url]
        tag = f'"{hash(body)}"'
        return Fetched(None if etag == tag else body, tag, None, self.moves.get(url))


@pytest.fixture
def web(monkeypatch):
    return FakeWeb(monkeypatch)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "podblock.sqlite"
    prepare(path)
    conn = connect(path)
    conn.execute("INSERT INTO users (username, is_admin, created_at) VALUES ('admin', 1, 0)")
    conn.commit()
    yield conn
    conn.close()


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
    assert feeds.html_to_text("<p>One &amp; <b>two</b></p><p>Three</p>") == "One & two\nThree"
    assert feeds.html_to_text("&lt;p&gt;Escaped twice&lt;/p&gt;") == "Escaped twice"
    assert feeds.html_to_text("<script>alert(1)</script>") == "alert(1)"
    assert len(feeds.summary("word " * 500)) <= feeds.SUMMARY_CHARS + 1


# ---- refreshing ---------------------------------------------------------------

def test_adding_a_feed_stores_it_all(db, web):
    web.pages[URL] = rss(item(1), item(2), item(3))
    result = feeds.add_podcast(db, URL)
    assert result.added == 3
    pod = db.execute("SELECT * FROM podcasts WHERE id = ?", (result.podcast_id,)).fetchone()
    assert pod["name"] == "Example Show" and json.loads(pod["categories"]) == ["Comedy", "Improv"]
    ep = db.execute("SELECT * FROM episodes WHERE guid = 'ep-3'").fetchone()
    assert ep["summary"] == "Notes for episode 3 & more. Second line."
    assert ep["feed_duration"] == 3723.0 and ep["item_xml"].startswith("<item")
    assert episode_titles(db, result.podcast_id) == ["Episode 3", "Episode 2", "Episode 1"]


def test_bad_feeds_are_refused(db, web):
    for url in ("ftp://example.com/x", "not a url", ""):
        with pytest.raises(feeds.RefreshError):
            feeds.add_podcast(db, url)
    web.pages[URL] = b"<html><body>not a feed</body></html>"
    with pytest.raises(feeds.RefreshError, match="RSS"):
        feeds.add_podcast(db, URL)
    with pytest.raises(feeds.RefreshError, match="404"):
        feeds.add_podcast(db, "https://example.com/missing.rss")
    assert db.execute("SELECT COUNT(*) FROM podcasts").fetchone()[0] == 0


def test_episodes_that_leave_the_feed_go_unless_downloaded(db, web):
    web.pages[URL] = rss(item(1), item(2), item(3))
    pod = feeds.add_podcast(db, URL).podcast_id
    db.execute("UPDATE episodes SET ever_downloaded = 1 WHERE guid = 'ep-1'")
    db.commit()

    web.pages[URL] = rss(item(3), item(4))
    result = feeds.refresh(db, pod)
    assert (result.added, result.removed, result.gone) == (1, 1, 1)
    rows = {r["guid"]: r["in_feed"] for r in db.execute("SELECT guid, in_feed FROM episodes")}
    assert rows == {"ep-1": 0, "ep-3": 1, "ep-4": 1}

    web.pages[URL] = rss(item(1), item(3), item(4))  # back in the feed
    feeds.refresh(db, pod)
    assert db.execute("SELECT in_feed FROM episodes WHERE guid = 'ep-1'").fetchone()[0] == 1


def test_episodes_the_detector_knows_count_as_downloaded(db, web):
    web.pages[URL] = rss(item(1), item(2))
    pod = feeds.add_podcast(db, URL).podcast_id
    ep = db.execute("SELECT id FROM episodes WHERE guid = 'ep-1'").fetchone()[0]
    db.execute("INSERT INTO reports VALUES (?, 0, '{}')", (ep,))
    db.commit()
    web.pages[URL] = rss(item(2))
    assert feeds.refresh(db, pod).gone == 1


def test_an_empty_feed_deletes_nothing(db, web):
    web.pages[URL] = rss(item(1), item(2))
    pod = feeds.add_podcast(db, URL).podcast_id
    web.pages[URL] = rss()
    assert feeds.refresh(db, pod).removed == 0
    assert len(episode_titles(db, pod)) == 2


def test_unchanged_feeds_cost_one_short_request(db, web):
    web.pages[URL] = rss(item(1))
    pod = feeds.add_podcast(db, URL).podcast_id
    assert feeds.refresh(db, pod).unchanged
    assert web.requests[-1][1] is not None  # sent the ETag


def test_failed_refresh_is_kept_on_the_podcast(db, web):
    web.pages[URL] = rss(item(1))
    pod = feeds.add_podcast(db, URL).podcast_id
    del web.pages[URL]
    with pytest.raises(feeds.RefreshError):
        feeds.refresh(db, pod, conditional=False)
    assert "404" in db.execute("SELECT last_error FROM podcasts").fetchone()[0]
    web.pages[URL] = rss(item(1))
    feeds.refresh(db, pod, conditional=False)
    assert db.execute("SELECT last_error FROM podcasts").fetchone()[0] is None


def test_another_shows_guid_does_not_steal_episodes(db, web):
    other = "https://example.org/other.rss"
    web.pages[URL] = rss(item(1))
    web.pages[other] = rss(item(1, guid="ep-1"), title="Other Show")
    a = feeds.add_podcast(db, URL).podcast_id
    b = feeds.add_podcast(db, other).podcast_id
    assert episode_titles(db, a) == ["Episode 1"] and episode_titles(db, b) == ["Episode 1"]
    feeds.refresh(db, a, conditional=False)
    feeds.refresh(db, b, conditional=False)
    assert db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 2


def test_refresh_due_skips_fresh_and_removed_feeds(db, web):
    web.pages[URL] = rss(item(1))
    pod = feeds.add_podcast(db, URL).podcast_id
    assert feeds.refresh_due(db) == []
    db.execute("UPDATE podcasts SET last_refreshed_at = 0")
    db.commit()
    assert feeds.refresh_due(db) == [pod]
    feeds.remove_podcast(db, pod)
    db.execute("UPDATE podcasts SET last_refreshed_at = 0")
    db.commit()
    assert feeds.refresh_due(db) == []


def test_the_detector_still_opens_a_store_the_app_has_filled(db, web, tmp_path):
    web.pages[URL] = rss(item(1), item(2))
    feeds.add_podcast(db, URL)
    store = Store(tmp_path / "podblock.sqlite")
    assert store.fingerprinted_episodes() == []
    assert store.podcast_id("Example Show", URL) == 1
    store.close()


# ---- pages ----------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, web):
    app = create_app(Settings(data_dir=tmp_path / "data", media_dir=tmp_path / "media", api_key="k",
                              background=False))
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"key": "k"})
    c.db_path = tmp_path / "data" / "podblock.sqlite"
    return c


def add(client, web, *items):
    web.pages[URL] = rss(*items)
    res = client.post("/podcasts", data={"url": URL})
    assert res.status_code == 303
    return int(res.headers["location"].rsplit("/", 1)[1])


def ids_of(client, pod, *guids):
    db = connect(client.db_path)
    try:
        return [db.execute("SELECT id FROM episodes WHERE guid = ?", (g,)).fetchone()[0] for g in guids]
    finally:
        db.close()


def test_add_and_browse_a_podcast(client, web):
    pod = add(client, web, *(item(n) for n in range(1, 4)))
    show = client.get(f"/podcasts/{pod}", headers=HTML)
    assert show.status_code == 200
    text = show.text
    assert "Added, with 3 episodes." in text
    assert text.index("Episode 3") < text.index("Episode 1")
    assert "1 hr 2 min" in text and "A show about examples." in text

    home = client.get("/", headers=HTML).text
    assert "Example Show" in home and "3 episodes" in home and "Added" not in home

    assert "add that feed: the server answered 404" in client.post("/podcasts", data={"url": "https://example.com/nope"}).text


def test_episode_list_pages_and_searches(client, web, monkeypatch):
    monkeypatch.setattr("podblock.web.podcasts.PAGE_SIZE", 2)
    pod = add(client, web, *(item(n) for n in range(1, 6)))
    first = client.get(f"/podcasts/{pod}").text
    assert "Episode 5" in first and "Episode 3" not in first and "page=2" in first
    more = client.get(f"/podcasts/{pod}?page=2&partial=1").text
    assert "Episode 3" in more and "<html" not in more

    found = client.get(f"/podcasts/{pod}?q=episode 4").text
    assert "Episode 4" in found and "Episode 5" not in found
    assert "Nothing matches" in client.get(f"/podcasts/{pod}?q=100%25").text


def test_archive_a_selection_and_undo(client, web):
    pod = add(client, web, *(item(n) for n in range(1, 5)))
    two, three = ids_of(client, pod, "ep-2", "ep-3")
    res = client.post(f"/podcasts/{pod}/episodes", data={"action": "archive", "ids": [two, three]}, headers=JSON)
    assert sorted(res.json()["changed"]) == sorted([two, three])

    active = client.get(f"/podcasts/{pod}").text
    assert "Episode 2" not in active and "Episode 4" in active
    archived = client.get(f"/podcasts/{pod}?view=archived").text
    assert "Episode 2" in archived and "Episode 4" not in archived

    again = client.post(f"/podcasts/{pod}/episodes", data={"action": "archive", "ids": [two]}, headers=JSON)
    assert again.json()["changed"] == []  # already archived

    client.post(f"/podcasts/{pod}/episodes", data={"action": "unarchive", "ids": [two, three]}, headers=JSON)
    assert "Episode 2" in client.get(f"/podcasts/{pod}").text


def test_archive_this_and_older(client, web):
    pod = add(client, web, *(item(n) for n in range(1, 6)))
    (three,) = ids_of(client, pod, "ep-3")
    res = client.post(f"/podcasts/{pod}/episodes", data={"action": "archive_older", "ids": three,
                                                          "next": f"/podcasts/{pod}"})
    assert res.status_code == 303
    page = client.get(f"/podcasts/{pod}", headers=HTML).text
    assert "Archived 3 episodes." in page
    assert "Episode 5" in page and "Episode 4" in page and "Episode 3" not in page


def test_archiving_is_per_user(client, web):
    pod = add(client, web, item(1), item(2))
    (one,) = ids_of(client, pod, "ep-1")
    client.post(f"/podcasts/{pod}/episodes", data={"action": "archive", "ids": one}, headers=JSON)
    db = connect(client.db_path)
    other = db.execute("INSERT INTO users (username, created_at) VALUES ('guest', 0)").lastrowid
    db.commit()
    assert [r["title"] for r in library.episodes(db, other, pod, archived=False)[0]] == ["Episode 2", "Episode 1"]
    db.close()


def test_episodes_of_another_podcast_are_left_alone(client, web):
    pod = add(client, web, item(1))
    web.pages["https://example.org/b.rss"] = rss(item(7, guid="b-7"), title="B")
    other = int(client.post("/podcasts", data={"url": "https://example.org/b.rss"}).headers["location"].rsplit("/", 1)[1])
    (theirs,) = ids_of(client, other, "b-7")
    res = client.post(f"/podcasts/{pod}/episodes", data={"action": "archive", "ids": theirs}, headers=JSON)
    assert res.json()["changed"] == []


def test_remove_a_podcast_and_add_it_back(client, web):
    pod = add(client, web, item(1), item(2))
    db = connect(client.db_path)
    db.execute("UPDATE episodes SET ever_downloaded = 1 WHERE guid = 'ep-1'")
    db.commit()
    res = client.post(f"/podcasts/{pod}/remove")
    assert res.headers["location"] == "/"
    assert "1 downloaded episodes stay" in client.get("/", headers=HTML).text
    assert client.get(f"/podcasts/{pod}").status_code == 404
    assert [r["guid"] for r in db.execute("SELECT guid FROM episodes")] == ["ep-1"]
    db.close()

    assert add(client, web, item(1), item(2)) == pod
    assert "Episode 2" in client.get(f"/podcasts/{pod}").text


def test_refresh_from_the_page(client, web):
    pod = add(client, web, item(1))
    web.pages[URL] = rss(item(1), item(2))
    client.post(f"/podcasts/{pod}/refresh")
    assert "1 new episode." in client.get(f"/podcasts/{pod}", headers=HTML).text
    del web.pages[URL]
    client.post(f"/podcasts/{pod}/refresh")
    assert "Refresh failed" in client.get(f"/podcasts/{pod}", headers=HTML).text


def test_feed_links_that_are_not_web_links_are_dropped(client, web):
    pod = add(client, web, item(1, extra="<itunes:image href=\"javascript:alert(1)\"/>"))
    page = client.get(f"/podcasts/{pod}").text
    assert "javascript:" not in page


def test_feeds_that_move_are_followed(db, web):
    web.pages[URL] = rss(item(1))
    pod = feeds.add_podcast(db, URL).podcast_id
    new = "https://example.org/moved.rss"
    web.moves[URL] = new  # a permanent redirect
    assert feeds.refresh(db, pod, conditional=False).moved_to == new
    assert db.execute("SELECT feed_url FROM podcasts").fetchone()[0] == new

    newer = "https://example.net/again.rss"
    web.pages[new] = rss(item(1), extra_channel=f"<itunes:new-feed-url>{newer}</itunes:new-feed-url>")
    feeds.refresh(db, pod, conditional=False)
    assert db.execute("SELECT feed_url FROM podcasts").fetchone()[0] == newer

    other = "https://example.com/other.rss"
    web.pages[other] = rss(item(9, guid="o-9"), title="Other")
    feeds.add_podcast(db, other)
    web.pages[newer] = rss(item(1), extra_channel=f"<itunes:new-feed-url>{other}</itunes:new-feed-url>")
    assert feeds.refresh(db, pod, conditional=False).moved_to is None  # that address is another podcast's
    assert db.execute("SELECT feed_url FROM podcasts WHERE id = ?", (pod,)).fetchone()[0] == newer


def test_new_episodes_download_on_their_own_when_asked(db, web):
    web.pages[URL] = rss(*(item(n) for n in range(1, 4)))
    pod = feeds.add_podcast(db, URL).podcast_id
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # adding a feed downloads nothing
    web.pages[URL] = rss(*(item(n) for n in range(1, 5)))
    assert feeds.refresh(db, pod).queued == 0  # off by default

    db.execute("UPDATE podcasts SET auto_download = 1")
    db.commit()
    web.pages[URL] = rss(*(item(n) for n in range(1, 13)))
    result = feeds.refresh(db, pod)
    assert result.added == 8 and result.queued == feeds.AUTO_DOWNLOAD_MAX
    queued = [r[0] for r in db.execute("SELECT e.guid FROM jobs j JOIN episodes e ON e.id = j.episode_id")]
    assert queued == [f"ep-{n}" for n in range(12, 7, -1)]  # the newest
    assert "5 queued to download" in result.describe()


CARD = 'class="podcast-name">Example Show<'  # its card on the home page


def test_each_user_picks_the_podcasts_on_their_home_page(client, web):
    pod = add(client, web, item(1), item(2))
    assert CARD in client.get("/", headers=HTML).text  # adding it put it on the admin's page
    client.post("/users", data={"username": "bob", "password": "password1"})
    bob = TestClient(client.app, follow_redirects=False)
    bob.post("/login", data={"username": "bob", "password": "password1"})

    home = bob.get("/", headers=HTML).text
    assert CARD not in home and "Pick your podcasts" in home
    every = bob.get("/?view=all", headers=HTML).text
    assert CARD in every and ">\n        Subscribe</button>" in every

    res = bob.post(f"/podcasts/{pod}/subscribe", data={"on": "true", "next": "/?view=all"})
    assert res.headers["location"] == "/?view=all"
    assert CARD in bob.get("/", headers=HTML).text
    assert "Unsubscribe" in bob.get("/?view=all", headers=HTML).text

    client.post(f"/podcasts/{pod}/subscribe", data={"on": "false"})
    assert CARD not in client.get("/", headers=HTML).text
    assert CARD in bob.get("/", headers=HTML).text  # the admin's choice leaves bob's alone
    assert "Subscribe" in client.get(f"/podcasts/{pod}", headers=HTML).text


def test_adding_a_feed_someone_else_added_subscribes_you(client, web):
    pod = add(client, web, item(1))
    client.post("/users", data={"username": "bob", "password": "password1"})
    bob = TestClient(client.app, follow_redirects=False)
    bob.post("/login", data={"username": "bob", "password": "password1"})
    assert bob.post("/podcasts", data={"url": URL}).headers["location"] == f"/podcasts/{pod}"
    assert CARD in bob.get("/", headers=HTML).text


def test_only_the_admin_removes_a_podcast(client, web):
    pod = add(client, web, item(1))
    client.post("/users", data={"username": "bob", "password": "password1"})
    bob = TestClient(client.app, follow_redirects=False)
    bob.post("/login", data={"username": "bob", "password": "password1"})
    bob.post(f"/podcasts/{pod}/subscribe", data={"on": "true"})

    show = bob.get(f"/podcasts/{pod}", headers=HTML).text
    assert "Remove podcast" not in show and "Make a new address" in show
    private = re.search(r'value="(http[^"]+/p/[^"]+/feed\.xml)"', show).group(1)
    assert bob.get(private).status_code == 200  # every user gets the private feed of any podcast
    assert bob.post(f"/podcasts/{pod}/remove").status_code == 403
    assert "Remove podcast" in client.get(f"/podcasts/{pod}", headers=HTML).text

    client.post(f"/podcasts/{pod}/remove")
    assert "No podcasts yet" in bob.get("/", headers=HTML).text
    add(client, web, item(1))  # adding it again brings it back for whoever adds it
    assert CARD in client.get("/", headers=HTML).text
    assert CARD not in bob.get("/", headers=HTML).text


def bob_logged_in(client):
    client.post("/users", data={"username": "bob", "password": "password1"})
    bob = TestClient(client.app, follow_redirects=False)
    bob.post("/login", data={"username": "bob", "password": "password1"})
    return bob


def titles(c, pod, view=""):
    page = c.get(f"/podcasts/{pod}" + (f"?view={view}" if view else ""), headers=HTML).text
    return [n for n in range(1, 10) if f">Episode {n}<" in page]


def test_a_filter_hides_episodes_from_its_owner_only(client, web):
    pod = add(client, web, *(item(n) for n in range(1, 5)))
    bob = bob_logged_in(client)
    res = client.post(f"/podcasts/{pod}/filters", data={"pattern": "EPISODE [23]$", "scope": "podcast"})
    assert res.headers["location"] == f"/podcasts/{pod}?filters=1"
    page = client.get(res.headers["location"], headers=HTML).text
    assert "It hides 2 episodes of this podcast from you." in page and "EPISODE [23]$" in page

    assert titles(client, pod) == [1, 4] and titles(client, pod, "filtered") == [2, 3]
    assert 'Filtered <span class="count">2</span>' in page and "2 episodes</span>" in page
    assert titles(bob, pod) == [1, 2, 3, 4]
    assert "Filtered <span" not in bob.get(f"/podcasts/{pod}", headers=HTML).text

    (f,) = connect(client.db_path).execute("SELECT id FROM episode_filters").fetchall()
    assert bob.post(f"/filters/{f['id']}/remove").status_code == 403
    client.post(f"/filters/{f['id']}/remove", data={"next": f"/podcasts/{pod}"})
    assert titles(client, pod) == [1, 2, 3, 4]


def test_filters_for_every_podcast_and_bad_patterns(client, web):
    pod = add(client, web, item(1), item(2))
    web.pages["https://example.org/b.rss"] = rss(item(3, guid="b-3"), title="B")
    other = int(client.post("/podcasts", data={"url": "https://example.org/b.rss"}).headers["location"].rsplit("/", 1)[1])
    client.post(f"/podcasts/{pod}/filters", data={"pattern": "episode [13]", "scope": "all"})
    assert titles(client, pod) == [2] and titles(client, other) == []
    assert "Episode 2" not in client.get(f"/podcasts/{other}", headers=HTML).text

    for pattern, why in [("(oops", "valid regular expression"), (".*", "matches every title"), ("  ", "Type a pattern")]:
        res = client.post(f"/podcasts/{pod}/filters", data={"pattern": pattern})
        assert "filters=1" in res.headers["location"]
        assert why in client.get(res.headers["location"], headers=HTML).text
    db = connect(client.db_path)
    assert db.execute("SELECT COUNT(*) FROM episode_filters").fetchone()[0] == 1
    db.close()


def test_a_processing_filter_hides_episodes_from_everyone_and_skips_them(client, web):
    pod = add(client, web, *(item(n) for n in range(1, 4)))
    bob = bob_logged_in(client)
    bob.post(f"/podcasts/{pod}/subscribe", data={"on": "true"})
    bob.post(f"/podcasts/{pod}/filters", data={"pattern": "episode [24]", "processing": "true"})
    bob.post(f"/podcasts/{pod}/filters", data={"pattern": "episode 1"})
    assert titles(client, pod) == [1, 3] and titles(bob, pod) == [3]
    assert titles(client, pod, "filtered") == [2]
    assert "Filtered from processing" in client.get(f"/podcasts/{pod}", headers=HTML).text

    db = connect(client.db_path)
    marked = lambda: [r["guid"] for r in db.execute("SELECT guid FROM episodes WHERE skip_processing = 1 ORDER BY id")]
    assert marked() == ["ep-2"]
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # nothing was learned from it

    # New episodes that match are marked as they arrive, and don't download on their own.
    db.execute("UPDATE podcasts SET auto_download = 1")
    db.commit()
    web.pages[URL] = rss(*(item(n) for n in range(1, 6)))
    assert feeds.refresh(db, pod).queued == 1
    assert marked() == ["ep-2", "ep-4"]
    assert [r[0] for r in db.execute("SELECT e.guid FROM jobs j JOIN episodes e ON e.id = j.episode_id")] == ["ep-5"]

    bob_id = db.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0]
    client.post(f"/users/{bob_id}/remove")  # his own filter goes; the one for everyone stays
    (f,) = db.execute("SELECT id, user_id FROM episode_filters").fetchall()
    assert f["user_id"] is None and "added by a removed user" in client.get(f"/podcasts/{pod}", headers=HTML).text
    assert client.post(f"/filters/{f['id']}/remove").status_code == 303  # the admin may remove anyone's
    assert marked() == []
    assert titles(client, pod) == [1, 2, 3, 4, 5]
    db.close()
