"""The podcast pages: adding and browsing podcasts, the episode list and what can be done to a selection,
filters, the show page's tabs, and finding podcasts by name."""

import html
import json
import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from fakes import HTML, JSON, URL, item, rss
from podblock.app import directory, filters, following, library
from podblock.app.config import Settings
from podblock.app.db import connect
from podblock.web.app import create_app

CARD = 'class="podcast-name">Example Show<'  # its card on the home page


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
    monkeypatch.setattr("podblock.web.routes.podcasts.PAGE_SIZE", 2)
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


def test_lists_narrow_to_downloaded_grabbed_and_cleaned_up(client, web):
    pod = add(client, web, *(item(n) for n in range(1, 5)))
    two, three = ids_of(client, pod, "ep-2", "ep-3")
    db = connect(client.db_path)
    db.execute("UPDATE episodes SET state = 'expired' WHERE id IN (?, ?)", (two, three))
    db.execute("UPDATE episodes SET state = 'ready' WHERE guid = 'ep-4'")
    db.commit()
    client.post(f"/podcasts/{pod}/episodes", data={"action": "archive", "ids": [three]}, headers=JSON)

    def shown(query):
        page = client.get(f"/podcasts/{pod}?{query}", headers=HTML).text
        return [n for n in range(1, 10) if f">Episode {n}<" in page], page

    assert shown("downloaded=1")[0] == [2, 4] and shown("view=downloaded")[0] == [2, 4]  # the old address works
    found, page = shown("view=archived&downloaded=1")
    assert found == [3]
    assert 'data-count="active">2<' in page and 'data-count="archived">1<' in page  # counts narrow too
    assert f'href="/podcasts/{pod}?downloaded=1"' in page  # the other lists keep it on
    assert f'href="/podcasts/{pod}?view=archived" role="button" aria-pressed="true"' in page  # turns it off
    found, page = shown("view=archived&downloaded=1&grabbed=1")
    assert found == [] and "Nothing archived is downloaded and grabbed" in page
    assert '<input type="hidden" name="grabbed" value="1">' in page  # searching keeps them on

    found, page = shown("cleaned=1")
    assert found == [2] and 'data-count="active">1<' in page and 'data-count="archived">1<' in page
    assert shown("view=archived&cleaned=1")[0] == [3]
    assert f'href="/podcasts/{pod}?cleaned=1">Show it</a>' in shown("")[1]  # from the clean-up note
    found, page = shown("cleaned=1&grabbed=1")
    assert found == [] and "Nothing cleaned up and grabbed" in page


def test_archive_this_and_older(client, web):
    pod = add(client, web, *(item(n) for n in range(1, 6)))
    (three,) = ids_of(client, pod, "ep-3")
    res = client.post(f"/podcasts/{pod}/episodes", data={"action": "archive_older", "ids": three,
                                                          "next": f"/podcasts/{pod}"})
    assert res.status_code == 303
    page = client.get(f"/podcasts/{pod}", headers=HTML).text
    assert "Archived 3 episodes." in page
    assert "Episode 5" in page and "Episode 4" in page and "Episode 3" not in page
    assert ">2 episodes</span>" in client.get("/", headers=HTML).text  # the home page counts what's left


def test_archiving_is_per_user(client, web):
    pod = add(client, web, item(1), item(2))
    (one,) = ids_of(client, pod, "ep-1")
    client.post(f"/podcasts/{pod}/episodes", data={"action": "archive", "ids": one}, headers=JSON)
    db = connect(client.db_path)
    other = db.execute("INSERT INTO users (username, created_at) VALUES ('guest', 0)").lastrowid
    db.commit()
    assert [r["title"] for r in library.episodes(db, other, pod, archived=False)[0]] == ["Episode 2", "Episode 1"]
    db.close()


def test_shared_query_fragments_take_named_parameters():
    # A `?` in a fragment would take whatever value sits at its place in each caller's arguments.
    for fragment in (library.ARCHIVED, library.KEPT_ANY, library.DOWNLOADED, library.USER_JOIN, library.GRABBED,
                     library.GRABS, filters.HIDDEN):
        assert "?" not in fragment


def test_episodes_of_another_podcast_are_left_alone(client, web):
    pod = add(client, web, item(1))
    web.pages["https://example.org/b.rss"] = rss(item(7, guid="b-7"), title="B")
    other = int(client.post("/podcasts", data={"url": "https://example.org/b.rss"}).headers["location"].split("/")[-1])
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

    assert "Remove podcast" not in bob.get(f"/podcasts/{pod}?tab=settings", headers=HTML).text
    show = bob.get(f"/podcasts/{pod}?tab=feed", headers=HTML).text
    assert "Make a new address" in show
    private = re.search(r'value="(http[^"]+/p/[^"]+/feed\.xml)"', show).group(1)
    assert bob.get(private).status_code == 200  # every user gets the private feed of any podcast
    assert bob.post(f"/podcasts/{pod}/remove").status_code == 403
    assert "Remove podcast" in client.get(f"/podcasts/{pod}?tab=settings", headers=HTML).text

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
    assert res.headers["location"] == f"/podcasts/{pod}?tab=filters#tabs"
    page = client.get(res.headers["location"], headers=HTML).text
    assert "It hides 2 episodes of this podcast from you." in page and "EPISODE [23]$" in page

    assert titles(client, pod) == [1, 4] and titles(client, pod, "filtered") == [2, 3]
    assert "2 episodes</span>" in page
    assert 'Filtered <span class="count">2</span>' in client.get(f"/podcasts/{pod}", headers=HTML).text
    assert titles(bob, pod) == [1, 2, 3, 4]
    assert "Filtered <span" not in bob.get(f"/podcasts/{pod}", headers=HTML).text

    (f,) = connect(client.db_path).execute("SELECT id FROM episode_filters").fetchall()
    assert bob.post(f"/filters/{f['id']}/remove").status_code == 403
    client.post(f"/filters/{f['id']}/remove", data={"next": f"/podcasts/{pod}"})
    assert titles(client, pod) == [1, 2, 3, 4]


def test_filters_for_every_podcast_and_bad_patterns(client, web):
    pod = add(client, web, item(1), item(2))
    web.pages["https://example.org/b.rss"] = rss(item(3, guid="b-3"), title="B")
    other = int(client.post("/podcasts", data={"url": "https://example.org/b.rss"}).headers["location"].split("/")[-1])
    client.post(f"/podcasts/{pod}/filters", data={"pattern": "episode [13]", "scope": "all"})
    assert titles(client, pod) == [2] and titles(client, other) == []
    assert "Episode 2" not in client.get(f"/podcasts/{other}", headers=HTML).text

    bad = [("(oops", "valid regular expression"), (".*", "matches every title"), ("  ", "Type a pattern")]
    for pattern, why in bad:
        res = client.post(f"/podcasts/{pod}/filters", data={"pattern": pattern})
        assert "tab=filters" in res.headers["location"]
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
    assert "Filtered from processing" in client.get(f"/podcasts/{pod}?tab=filters", headers=HTML).text

    db = connect(client.db_path)
    def marked():
        return [r["guid"] for r in db.execute("SELECT guid FROM episodes WHERE skip_processing = 1 ORDER BY id")]

    assert marked() == ["ep-2"]
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0  # nothing was learned from it

    # New episodes that match are marked as they arrive, and don't download on their own.
    db.execute("UPDATE podcasts SET auto_download = 1")
    db.commit()
    web.pages[URL] = rss(*(item(n) for n in range(1, 6)))
    assert following.refresh(db, pod).queued == 1
    assert marked() == ["ep-2", "ep-4"]
    assert [r[0] for r in db.execute("SELECT e.guid FROM jobs j JOIN episodes e ON e.id = j.episode_id")] == ["ep-5"]

    bob_id = db.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0]
    client.post(f"/users/{bob_id}/remove")  # his own filter goes; the one for everyone stays
    (f,) = db.execute("SELECT id, user_id FROM episode_filters").fetchall()
    assert f["user_id"] is None
    assert "added by a removed user" in client.get(f"/podcasts/{pod}?tab=filters", headers=HTML).text
    assert client.post(f"/filters/{f['id']}/remove").status_code == 303  # the admin may remove anyone's
    assert marked() == []
    assert titles(client, pod) == [1, 2, 3, 4, 5]
    db.close()


# ---- show tabs, order, paging and downloads -----------------------------------------

def test_show_page_tabs(client, web):
    pod = add(client, web, item(1))
    show = client.get(f"/podcasts/{pod}", headers=HTML).text
    assert 'id="tabs"' in show and "Episode 1" in show and "More actions" not in show
    assert "Make a new address" not in show and "Add filter" not in show and "Clean up downloads" not in show
    assert "Make a new address" in client.get(f"/podcasts/{pod}?tab=feed", headers=HTML).text
    assert "Make a new address" in client.get(f"/podcasts/{pod}?feed=1", headers=HTML).text  # the old address
    filters_tab = client.get(f"/podcasts/{pod}?tab=filters", headers=HTML).text
    assert "Add filter" in filters_tab and "Episode 1" not in filters_tab
    assert "Clean up downloads" in client.get(f"/podcasts/{pod}?tab=settings", headers=HTML).text


def test_episodes_can_be_listed_oldest_first(client, web, monkeypatch):
    monkeypatch.setattr("podblock.web.routes.podcasts.PAGE_SIZE", 2)
    pod = add(client, web, *(item(n) for n in range(1, 6)))
    assert titles(client, pod) == [4, 5] and "Newest first" in client.get(f"/podcasts/{pod}").text

    res = client.post(f"/podcasts/{pod}/settings", data={"retention_days": 7, "refresh_minutes": 60, "sort": "oldest"})
    assert res.headers["location"] == f"/podcasts/{pod}?tab=settings#tabs"
    assert "Oldest first</option>" in client.get(f"/podcasts/{pod}?tab=settings").text.replace("selected>", ">")
    first = client.get(f"/podcasts/{pod}").text
    assert titles(client, pod) == [1, 2] and "Oldest first" in first
    assert ">Episode 3<" in client.get(f"/podcasts/{pod}?page=2&partial=1").text

    client.post(f"/podcasts/{pod}/settings", data={"retention_days": 7, "refresh_minutes": 60, "sort": "newest"})
    assert titles(client, pod) == [4, 5]


def test_the_next_page_loads_as_it_scrolls_into_view(client, web, monkeypatch):
    monkeypatch.setattr("podblock.web.routes.podcasts.PAGE_SIZE", 2)
    pod = add(client, web, *(item(n) for n in range(1, 4)))
    more = re.search(r'<li class="ep-more">.*?</li>', client.get(f"/podcasts/{pod}").text, re.S).group(0)
    assert 'hx-trigger="revealed, click"' in more and f'href="/podcasts/{pod}?page=2"' in more
    assert "ep-more" not in client.get(f"/podcasts/{pod}?page=2&partial=1").text  # the last page


def test_each_row_has_a_download_button(client, web):
    pod = add(client, web, item(1))
    (one,) = ids_of(client, pod, "ep-1")
    row = client.get(f"/episodes/{one}/row").text
    assert 'class="icon-btn ep-dl"' in row and 'value="download"' in row and "Download and cut" in row
    res = client.post(f"/podcasts/{pod}/episodes", data={"action": "download", "ids": one}, headers=JSON)
    assert res.json() == {"action": "download", "changed": [one]}
    row = client.get(f"/episodes/{one}/row").text
    assert "ep-dl-busy" in row and 'value="download"' not in row and 'hx-trigger="every 3s"' in row


def test_back_to_the_show_keeps_the_list_it_came_from(client, web):
    pod = add(client, web, item(1))
    (one,) = ids_of(client, pod, "ep-1")

    def back(referer=None):
        headers = {"referer": referer} if referer else {}
        text = client.get(f"/episodes/{one}", headers=headers).text
        return html.unescape(re.search(r'<a class="back" href="([^"]*)"', text).group(1))

    assert back() == f"/podcasts/{pod}"
    assert back(f"http://testserver/podcasts/{pod}?view=archived&downloaded=1&q=night&page=3") == \
        f"/podcasts/{pod}?view=archived&downloaded=1&q=night"
    assert back(f"http://testserver/podcasts/{pod}?view=grabbed") == f"/podcasts/{pod}?grabbed=1"
    assert back(f"http://testserver/podcasts/{pod}?tab=settings") == f"/podcasts/{pod}"
    assert back(f"http://testserver/podcasts/{pod + 1}?view=archived") == f"/podcasts/{pod}"
    assert back(f"http://elsewhere.example/podcasts/{pod}?view=archived") == f"/podcasts/{pod}"


def test_only_the_admin_sees_downloads(client, web):
    add(client, web, item(1))
    page = client.get("/downloads", headers=HTML)
    assert page.status_code == 200 and "Nothing on disk" in page.text and 'href="/downloads"' in page.text
    bob = bob_logged_in(client)
    assert bob.get("/downloads", headers=HTML).status_code == 403
    assert bob.post("/downloads/delete", data={"ids": 1}).status_code == 403
    assert 'href="/downloads"' not in bob.get("/", headers=HTML).text


# ---- finding podcasts by name ------------------------------------------------------

def test_what_reads_as_an_address():
    assert directory.as_address(" https://example.com/show.rss ") == "https://example.com/show.rss"
    assert directory.as_address("feed://example.com/show.rss") == "https://example.com/show.rss"
    assert directory.as_address("following.example.com/show") == "https://following.example.com/show"
    for text in ("radiolab", "this american life", "mr.robot", "99.9 percent invisible", ""):
        assert directory.as_address(text) is None


class FakeDirectory:
    """Stands in for Apple's search and lookup."""

    def __init__(self, monkeypatch):
        self.shows: list[dict] = []
        self.calls: list[tuple] = []
        monkeypatch.setattr(directory, "_get", self.get)

    def get(self, url, **params):
        self.calls.append((url, params))
        if url == directory.LOOKUP_URL:
            return [s for s in self.shows if str(s["collectionId"]) == params["id"]]
        return self.shows

    def show(self, n, feed_url, **kw):
        self.shows.append({"collectionId": n, "collectionName": f"Found {n}", "artistName": "Someone",
                           "feedUrl": feed_url, "artworkUrl600": f"https://img.example.com/{n}.jpg",
                           "primaryGenreName": "Comedy", "trackCount": 12, **kw})


@pytest.fixture
def apple(monkeypatch):
    return FakeDirectory(monkeypatch)


def test_a_name_searches_the_directory(client, web, apple):
    pod = add(client, web, item(1))
    apple.show(1, URL)
    apple.show(2, "https://example.org/b.rss")
    apple.shows.append({"collectionId": 3, "collectionName": "No feed"})

    res = client.post("/podcasts", data={"url": " found shows "})
    assert res.status_code == 303 and res.headers["location"] == "/?search=found+shows"
    page = client.get(res.headers["location"], headers=HTML).text
    assert apple.calls[-1][1]["term"] == "found shows"
    assert "2 shows for “found shows”" in page and "No feed" not in page
    assert f'href="/podcasts/{pod}">Open<' in page  # already on your home page
    assert 'name="url" value="https://example.org/b.rss"' in page and "Found 2" in page
    assert 'value="found shows"' in page  # the box keeps what was typed

    web.pages["https://example.org/b.rss"] = rss(item(1)).replace(b"Example Show", b"Found 2")
    res = client.post("/podcasts", data={"url": "https://example.org/b.rss"})
    assert res.status_code == 303 and "Found 2" in client.get("/", headers=HTML).text


def test_a_search_with_nothing_found_or_no_answer(client, web, apple, monkeypatch):
    assert "Nothing in Apple" in client.get("/?search=zzz", headers=HTML).text

    def down(*a, **kw):
        raise directory.DirectoryError("couldn't reach the podcast directory (ConnectionError)")
    monkeypatch.setattr(directory, "search", down)
    page = client.get("/?search=zzz", headers=HTML).text
    assert "Couldn&#39;t search: couldn&#39;t reach the podcast directory" in page
    assert client.post("/podcasts", data={"url": "  "}).headers["location"] == "/"


def test_an_apple_podcasts_link_follows_its_feed(client, web, apple):
    web.pages[URL] = rss(item(1))
    apple.show(152249110, URL)
    res = client.post("/podcasts", data={"url": "https://podcasts.apple.com/us/podcast/example/id152249110?i=1"})
    assert res.status_code == 303 and res.headers["location"].startswith("/podcasts/")
    assert apple.calls == [(directory.LOOKUP_URL, {"id": "152249110", "entity": "podcast"})]
    res = client.post("/podcasts", data={"url": "https://podcasts.apple.com/us/podcast/gone/id999"})
    assert "Couldn&#39;t add that feed: Apple Podcasts has no feed for that show." in res.text


def test_episode_page_counts_every_section_the_timeline_cuts(client, web, tmp_path):
    pod = add(client, web, item(1))
    (one,) = ids_of(client, pod, "ep-1")
    original = tmp_path / "original.mp3"
    original.write_bytes(b"")

    def section(start, end, *types):
        return {"start": start, "end": end, "length": end - start, "blocks": [], "types": list(types)}

    report = {"duration": 3723, "sections": [section(0, 30, "intro"), section(600, 690, "break"),
                                             section(1800, 1890, "break"), section(2400, 2430, "repeated")],
              "kept": [section(3000, 3020, "repeated")], "not_repeats": []}
    db = connect(client.db_path)
    try:
        db.execute("INSERT INTO reports VALUES (?, 0, ?)", (one, json.dumps(report)))
        # cut by hand, over a break
        db.execute("INSERT INTO edits (episode_id, start, end, created_at) VALUES (?, 650, 700, 0)", (one,))
        db.execute("UPDATE episodes SET state = 'ready', duration = 3723, cut_duration = 3423, original_path = ?, "
                   "missing_cuts = ?, missing_kept = ?, phrase_cuts = ? WHERE id = ?",
                   (str(original), json.dumps([{"type": "missing", "start": 1200, "end": 1260}]),
                    json.dumps([[2700, 2760]]), json.dumps([{"type": "outro", "start": 3650, "end": 3723}]), one))
        db.commit()
    finally:
        db.close()
    text = client.get(f"/episodes/{one}").text
    assert "in 7 sections." in text and "7 sections cut" in text  # 4 found, 1 by hand, 1 missing, 1 phrase

    db = connect(client.db_path)
    try:
        db.execute("UPDATE episodes SET original_path = NULL WHERE id = ?", (one,))
        db.commit()
    finally:
        db.close()
    text = client.get(f"/episodes/{one}").text
    assert "What gets cut" in text and '<span class="muted">7 sections</span>' in text
