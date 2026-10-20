"""Private feeds: each podcast's ready episodes, with the repeats cut out, for any podcast app.

No login: the feed's address holds a random token, and so does every audio
link in it. Each user has their own feed of each podcast (`user_feeds`), which
leaves out the episodes they archived or filtered. Its audio links carry the
same token, so each fetch of an episode counts as a grab by that user
(`grabs.py`). Audio links from before then carry the podcast's token
(`podcasts.public_token`) and still work, counted for nobody. The feed is the
source's own <channel> and <item>s, as the source gave them, with these
changes:

- each enclosure points at the cut file, with its length and type, and
  `itunes:duration` is the cut length;
- chapters and timed transcripts point at copies whose times fit the cut
  (`chapters.py`), with a chapter at each cut; other transcripts, and
  soundbites, go, as their times no longer fit;
- other links to the source's audio go too (`media:content` for audio,
  `podcast:alternateEnclosure`), since apps may play those instead;
- `itunes:new-feed-url` goes (it would send apps back to the source), the
  `atom:link rel="self"` points here, and `itunes:block` keeps the feed out
  of directories;
- an item with no artwork of its own at a web address gets the show's, and
  the channel says what made it (`generator`) and when it last changed
  (`lastBuildDate`).

Each user also has an omni feed (`users.omni_token`, at the same kind of
address): every downloaded episode of the podcasts on their home page, newest
first, under pod-block's own channel. Its items are built as above, with the
show's name before each title (`itunes:title` too, which apps show first) and
the show as each item's author.

Each feed has a second address, `downloaded.xml` in place of `feed.xml`, that
lists the same episodes newest downloaded first. Apps sort by `pubDate`, so
there each item's `pubDate` is when its episode was downloaded and cut.
"""

from __future__ import annotations

import io
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from ..feed import ITUNES, PODCAST, PSC
from . import chapters, media
from .db import new_token, transaction
from .filters import HIDDEN
from .library import USER_JOIN
from .text import web_url

ATOM = "{http://www.w3.org/2005/Atom}"
PREFIXES = {"itunes": ITUNES[1:-1], "atom": ATOM[1:-1], "podcast": PODCAST[1:-1],
            "content": "http://purl.org/rss/1.0/modules/content/", "psc": PSC[1:-1],
            "googleplay": "http://www.google.com/schemas/play-podcasts/1.0", "media": "http://search.yahoo.com/mrss/"}
MEDIA = "{http://search.yahoo.com/mrss/}"
OMNI_TITLE = "Pod Block Omni Feed"
OMNI_ABOUT = ("Every episode you've downloaded from your podcasts, in one feed, with the repeats cut out. "
              "Made by pod-block.")
OMNI_ART = "static/omni.jpg"
FEED_FILES = {False: "feed.xml", True: "downloaded.xml"}  # by_download: the feed's file name
OUT_OF_STEP = {f"{PODCAST}chapters", f"{PODCAST}transcript", f"{PODCAST}soundbite", f"{PSC}chapters",
               f"{PODCAST}alternateEnclosure"}


def _uncut_audio(el: ET.Element) -> bool:
    """A link to the source's audio other than the enclosure, which an app might play instead."""
    if el.tag == f"{MEDIA}group":
        return any(_uncut_audio(child) for child in el)
    if el.tag != f"{MEDIA}content":
        return False
    kind = (el.get("type") or "").lower()
    return el.get("medium") == "audio" or kind.startswith("audio/") or (not kind and not el.get("medium"))


def _register(xml: str | None) -> None:
    """Write the namespace prefixes a stored piece of XML used back out, not ns0, ns1."""
    for prefix, uri in PREFIXES.items():
        ET.register_namespace(prefix, uri)
    if not xml:
        return
    try:
        for _, (prefix, uri) in ET.iterparse(io.BytesIO(xml.encode()), events=("start-ns",)):
            if prefix and not prefix.startswith("ns"):
                ET.register_namespace(prefix, uri)
    except ET.ParseError:
        pass


def feed_token(db: sqlite3.Connection, user_id: int, podcast_id: int) -> str:
    """The token of a user's feed of a podcast, made on first ask."""
    row = db.execute("SELECT token FROM user_feeds WHERE user_id = ? AND podcast_id = ?",
                     (user_id, podcast_id)).fetchone()
    return row["token"] if row else new_feed_token(db, user_id, podcast_id)


def new_feed_token(db: sqlite3.Connection, user_id: int, podcast_id: int) -> str:
    """Give a user's feed of a podcast a new address; the old one stops working at once. Audio links stay."""
    token = new_token()
    with transaction(db):
        db.execute("""INSERT INTO user_feeds (user_id, podcast_id, token, created_at) VALUES (?, ?, ?, ?)
                      ON CONFLICT (user_id, podcast_id) DO UPDATE SET token = excluded.token""",
                   (user_id, podcast_id, token, time.time()))
    return token


def omni_token(db: sqlite3.Connection, user_id: int) -> str:
    """The token of a user's omni feed, made on first ask."""
    row = db.execute("SELECT omni_token FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["omni_token"] or new_omni_token(db, user_id)


def new_omni_token(db: sqlite3.Connection, user_id: int) -> str:
    """Give a user's omni feed a new address; the old one stops working at once."""
    token = new_token()
    with transaction(db):
        db.execute("UPDATE users SET omni_token = ? WHERE id = ?", (token, user_id))
    return token


def _order(by_download: bool) -> str:
    """Newest first: released, or downloaded."""
    if by_download:
        return "COALESCE(e.ready_at, e.processed_at) IS NULL, COALESCE(e.ready_at, e.processed_at) DESC, e.id DESC"
    return "e.pub_ts IS NULL, e.pub_ts DESC, e.id DESC"


def _downloaded_at(ep: sqlite3.Row) -> float | None:
    """When an episode was downloaded and first cut."""
    return ep["ready_at"] or ep["processed_at"]


def published(db: sqlite3.Connection, podcast_id: int, user_id: int | None = None,
              by_download: bool = False) -> list[sqlite3.Row]:
    """A podcast's episodes that can be in a private feed: ready, with their cut file on disk, newest released
    (or downloaded) first. With a user, the ones in their feed: not archived by them, nor hidden by a filter."""
    mine = f"AND COALESCE(ue.archived, 0) = 0 AND NOT {HIDDEN}" if user_id is not None else ""
    rows = db.execute(
        f"""SELECT e.* FROM episodes e {USER_JOIN}
            WHERE e.podcast_id = :podcast_id AND e.state = 'ready' AND e.cut_path IS NOT NULL {mine}
            ORDER BY {_order(by_download)}""",
        {"user_id": user_id, "podcast_id": podcast_id}).fetchall()
    return [r for r in rows if Path(r["cut_path"]).exists()]


def omni_published(db: sqlite3.Connection, user_id: int, by_download: bool = False) -> list[sqlite3.Row]:
    """The episodes in a user's omni feed: ready, with their cut file on disk, of the podcasts on their home
    page, not archived by them nor hidden by a filter; newest released (or downloaded) first. Each row has its
    podcast's name, author, artwork and explicit flag."""
    rows = db.execute(
        f"""SELECT e.*, p.name AS podcast_name, p.author AS podcast_author, p.image_url AS podcast_image,
                   p.explicit AS podcast_explicit
            FROM episodes e JOIN podcasts p ON p.id = e.podcast_id AND p.followed = 1
            JOIN user_podcasts up ON up.podcast_id = p.id AND up.user_id = :user_id
            {USER_JOIN}
            WHERE e.state = 'ready' AND e.cut_path IS NOT NULL AND COALESCE(ue.archived, 0) = 0 AND NOT {HIDDEN}
            ORDER BY {_order(by_download)}""", {"user_id": user_id}).fetchall()
    return [r for r in rows if Path(r["cut_path"]).exists()]


def podcast_by_token(db: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """The podcast whose audio links carry this token."""
    if not token or len(token) > 64:
        return None
    return db.execute("SELECT * FROM podcasts WHERE public_token = ? AND followed = 1", (token,)).fetchone()


def feed_by_token(db: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """The user and podcast whose feed this is: the podcast's row, with `user_id` and `feed_token`."""
    if not token or len(token) > 64:
        return None
    return db.execute("""SELECT p.*, f.user_id, f.token AS feed_token FROM user_feeds f
                         JOIN podcasts p ON p.id = f.podcast_id AND p.followed = 1 WHERE f.token = ?""",
                      (token,)).fetchone()


def omni_by_token(db: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """The user whose omni feed this is."""
    if not token or len(token) > 64:
        return None
    return db.execute("SELECT * FROM users WHERE omni_token = ?", (token,)).fetchone()


def _hms(secs: float | None) -> str:
    secs = int(round(secs or 0))
    return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def _set(parent: ET.Element, tag: str, text: str, attrib: dict | None = None) -> ET.Element:
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    el.text = text
    el.attrib.update(attrib or {})
    return el


def _rfc822(ts: float) -> str:
    return format_datetime(datetime.fromtimestamp(ts, timezone.utc))


def _item(ep: sqlite3.Row, link: str, art: str | None = None, by_download: bool = False) -> ET.Element:
    """An episode's <item>, pointing at its cut audio, chapters and transcripts, whose addresses start with
    `link`. `art` is the show's artwork, for items with none. By download, its `pubDate` is when it was
    downloaded."""
    item = None
    if ep["item_xml"]:
        try:
            item = ET.fromstring(ep["item_xml"])
        except ET.ParseError:
            item = None
    if item is None:  # an episode the CLI added: make a plain item
        item = ET.Element("item")
        ET.SubElement(item, "title").text = ep["title"] or "Untitled"
        ET.SubElement(item, "guid", isPermaLink="false").text = ep["guid"]
        if ep["pub_ts"]:
            ET.SubElement(item, "pubDate").text = _rfc822(ep["pub_ts"])
    for el in [el for el in item if el.tag in OUT_OF_STEP or _uncut_audio(el)]:
        item.remove(el)
    for el in item.findall("enclosure")[1:]:
        item.remove(el)
    enclosure = item.find("enclosure")
    if enclosure is None:
        enclosure = ET.SubElement(item, "enclosure")
    enclosure.attrib.clear()
    enclosure.attrib.update(url=link + Path(ep["cut_path"]).suffix, length=str(ep["cut_bytes"] or 0),
                            type=ep["cut_type"] or "audio/mpeg")
    _set(item, f"{ITUNES}duration", _hms(ep["cut_duration"]))
    if chapters.has_chapters(ep):
        ET.SubElement(item, f"{PODCAST}chapters", url=f"{link}.chapters.json", type="application/json+chapters")
    for t in chapters.transcripts(ep):
        ET.SubElement(item, f"{PODCAST}transcript", url=f"{link}.transcript.{t['format']}",
                      type=chapters.MIME[t["format"]], **{k: t[k] for k in ("language", "rel") if t.get(k)})
    own = item.find(f"{ITUNES}image")
    if (own is None or not web_url(own.get("href"))) and (image := web_url(ep["image_url"]) or web_url(art)):
        _set(item, f"{ITUNES}image", "", {"href": image})
    if by_download and (when := _downloaded_at(ep)):
        _set(item, "pubDate", _rfc822(when))
    return item


def _stamp(channel: ET.Element, episodes: list[sqlite3.Row]) -> None:
    """Say what made the feed, and when it last changed: when its newest-processed episode was made ready."""
    _set(channel, "generator", "pod-block")
    changed = max((ep["processed_at"] or 0 for ep in episodes), default=0)
    if changed:
        _set(channel, "lastBuildDate", _rfc822(changed))


def _rss(channel: ET.Element) -> bytes:
    rss = ET.Element("rss", version="2.0")
    rss.append(channel)
    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def build_feed(db: sqlite3.Connection, pod: sqlite3.Row, base: str, user_id: int, token: str,
               by_download: bool = False) -> bytes:
    """A user's feed of a podcast, whose address holds `token`."""
    _register(pod["channel_xml"])
    channel = None
    if pod["channel_xml"]:
        try:
            channel = ET.fromstring(pod["channel_xml"])
        except ET.ParseError:
            channel = None
    if channel is None:  # a podcast the CLI added
        channel = ET.Element("channel")
        ET.SubElement(channel, "title").text = pod["name"]
        ET.SubElement(channel, "description").text = pod["description"] or pod["name"]
    for el in [el for el in channel if el.tag == f"{ITUNES}new-feed-url"
               or (el.tag == f"{ATOM}link" and el.get("rel") == "self")]:
        channel.remove(el)
    self_link = ET.Element(f"{ATOM}link", href=f"{base}/p/{token}/{FEED_FILES[by_download]}", rel="self",
                           type="application/rss+xml")
    channel.insert(0, self_link)
    _set(channel, f"{ITUNES}block", "Yes")
    episodes = published(db, pod["id"], user_id, by_download)
    _stamp(channel, episodes)
    for ep in episodes:
        _register(ep["item_xml"])
        channel.append(_item(ep, _link(base, token, ep), pod["image_url"], by_download))
    return _rss(channel)


def _link(base: str, token: str, ep: sqlite3.Row) -> str:
    """Where an episode's files are, through the feed whose address holds `token`, less the suffix."""
    return f"{base}/p/{token}/{media.key(ep['guid'])}"


def build_omni(db: sqlite3.Connection, user_id: int, base: str, token: str, by_download: bool = False) -> bytes:
    """A user's omni feed, whose address holds `token`: every downloaded episode of their podcasts."""
    _register(None)
    episodes = omni_published(db, user_id, by_download)
    art = f"{base}/{OMNI_ART}"
    channel = ET.Element("channel")
    ET.SubElement(channel, f"{ATOM}link", href=f"{base}/p/{token}/{FEED_FILES[by_download]}", rel="self",
                  type="application/rss+xml")
    ET.SubElement(channel, "title").text = OMNI_TITLE
    ET.SubElement(channel, "link").text = f"{base}/"
    ET.SubElement(channel, "description").text = OMNI_ABOUT
    _stamp(channel, episodes)
    image = ET.SubElement(channel, "image")
    ET.SubElement(image, "url").text = art
    ET.SubElement(image, "title").text = OMNI_TITLE
    ET.SubElement(image, "link").text = f"{base}/"
    ET.SubElement(channel, f"{ITUNES}image", href=art)
    ET.SubElement(channel, f"{ITUNES}author").text = "pod-block"
    ET.SubElement(channel, f"{ITUNES}summary").text = OMNI_ABOUT
    ET.SubElement(channel, f"{ITUNES}type").text = "episodic"
    explicit = any(ep["podcast_explicit"] for ep in episodes)
    ET.SubElement(channel, f"{ITUNES}explicit").text = "true" if explicit else "false"
    ET.SubElement(channel, f"{ITUNES}block").text = "Yes"
    ET.SubElement(channel, f"{PODCAST}locked").text = "yes"
    for ep in episodes:
        _register(ep["item_xml"])
        item = _item(ep, _link(base, token, ep), ep["podcast_image"], by_download)
        show = ep["podcast_name"] or "Untitled"
        title = (item.findtext("title") or "").strip() or ep["title"] or "Untitled"
        _set(item, "title", f"{show}: {title}")
        own = (item.findtext(f"{ITUNES}title") or "").strip() or title
        _set(item, f"{ITUNES}title", f"{show}: {own}")  # apps show it before <title>
        _set(item, f"{ITUNES}author", ep["podcast_author"] or show)
        _set(item, "guid", ep["guid"], {"isPermaLink": "false"})  # unique across shows, as the store keeps them
        channel.append(item)
    return _rss(channel)


def ready_episode(db: sqlite3.Connection, key: str, podcast_id: int | None = None) -> sqlite3.Row | None:
    """The ready episode, with its cut file on disk, whose files are named `key`: of one podcast, or of any."""
    rows = db.execute(
        f"""SELECT e.* FROM episodes e JOIN podcasts p ON p.id = e.podcast_id AND p.followed = 1
            WHERE e.state = 'ready' AND instr(e.cut_path, ?) > 0 {'AND e.podcast_id = ?' if podcast_id else ''}""",
        (key, *([podcast_id] if podcast_id else []))).fetchall()
    return next((r for r in rows if media.key(r["guid"]) == key and Path(r["cut_path"]).exists()), None)
