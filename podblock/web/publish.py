"""The private feed: each podcast's ready episodes, with the repeats cut out, for any podcast app.

No login: the feed's address holds a random token, and so does every audio
link in it. The feed is the source's own <channel> and <item>s, as the source
gave them, with these changes:

- each enclosure points at the cut file, with its length and type, and
  `itunes:duration` is the cut length;
- chapters, transcripts and soundbites go, since their times no longer fit
  (until we shift them);
- other links to the source's audio go too (`media:content` for audio,
  `podcast:alternateEnclosure`), since apps may play those instead;
- `itunes:new-feed-url` goes (it would send apps back to the source), the
  `atom:link rel="self"` points here, and `itunes:block` keeps the feed out
  of directories.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from ..feed import ITUNES
from .db import new_token
from .deps import get_db
from .processing import media_key

ATOM = "{http://www.w3.org/2005/Atom}"
PODCAST = "{https://podcastindex.org/namespace/1.0}"
PSC = "{http://podlove.org/simple-chapters}"
PREFIXES = {"itunes": ITUNES[1:-1], "atom": ATOM[1:-1], "podcast": PODCAST[1:-1],
            "content": "http://purl.org/rss/1.0/modules/content/", "psc": PSC[1:-1],
            "googleplay": "http://www.google.com/schemas/play-podcasts/1.0", "media": "http://search.yahoo.com/mrss/"}
MEDIA = "{http://search.yahoo.com/mrss/}"
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

router = APIRouter()


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


def ensure_token(db: sqlite3.Connection, podcast_id: int) -> str:
    row = db.execute("SELECT public_token FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    if row["public_token"]:
        return row["public_token"]
    token = new_token()
    db.execute("UPDATE podcasts SET public_token = ? WHERE id = ?", (token, podcast_id))
    db.commit()
    return token


def new_feed_token(db: sqlite3.Connection, podcast_id: int) -> str:
    """Give a podcast a new feed address; the old one stops working at once."""
    token = new_token()
    db.execute("UPDATE podcasts SET public_token = ? WHERE id = ?", (token, podcast_id))
    db.commit()
    return token


def base_url(request: Request) -> str:
    return request.app.state.settings.public_url or str(request.base_url).rstrip("/")


def feed_url(request: Request, token: str) -> str:
    return f"{base_url(request)}/p/{token}/feed.xml"


def published(db: sqlite3.Connection, podcast_id: int) -> list[sqlite3.Row]:
    """A podcast's episodes that are in its private feed: ready, with their cut file on disk, newest first."""
    rows = db.execute(
        """SELECT * FROM episodes WHERE podcast_id = ? AND state = 'ready' AND cut_path IS NOT NULL
           ORDER BY pub_ts IS NULL, pub_ts DESC, id DESC""", (podcast_id,)).fetchall()
    return [r for r in rows if Path(r["cut_path"]).exists()]


def _podcast_by_token(db: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    if not token or len(token) > 64:
        return None
    return db.execute("SELECT * FROM podcasts WHERE public_token = ? AND followed = 1", (token,)).fetchone()


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


def _item(ep: sqlite3.Row, audio_url: str) -> ET.Element:
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
            ET.SubElement(item, "pubDate").text = format_datetime(datetime.fromtimestamp(ep["pub_ts"], timezone.utc))
    for el in [el for el in item if el.tag in OUT_OF_STEP or _uncut_audio(el)]:
        item.remove(el)
    for el in item.findall("enclosure")[1:]:
        item.remove(el)
    enclosure = item.find("enclosure")
    if enclosure is None:
        enclosure = ET.SubElement(item, "enclosure")
    enclosure.attrib.clear()
    enclosure.attrib.update(url=audio_url, length=str(ep["cut_bytes"] or 0), type=ep["cut_type"] or "audio/mpeg")
    _set(item, f"{ITUNES}duration", _hms(ep["cut_duration"]))
    return item


def build_feed(db: sqlite3.Connection, pod: sqlite3.Row, base: str) -> bytes:
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
    self_link = ET.Element(f"{ATOM}link", href=f"{base}/p/{pod['public_token']}/feed.xml", rel="self",
                           type="application/rss+xml")
    channel.insert(0, self_link)
    _set(channel, f"{ITUNES}block", "Yes")
    for ep in published(db, pod["id"]):
        _register(ep["item_xml"])
        suffix = Path(ep["cut_path"]).suffix
        channel.append(_item(ep, f"{base}/p/{pod['public_token']}/{media_key(ep['guid'])}{suffix}"))
    rss = ET.Element("rss", version="2.0")
    rss.append(channel)
    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


NOT_FOUND = PlainTextResponse("Not found", status_code=404)


@router.api_route("/p/{token}/feed.xml", methods=["GET", "HEAD"], include_in_schema=False)
def private_feed(request: Request, token: str, db: sqlite3.Connection = Depends(get_db)) -> Response:
    pod = _podcast_by_token(db, token)
    if pod is None:
        return NOT_FOUND
    body = build_feed(db, pod, base_url(request))
    etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=300", "X-Robots-Tag": "noindex"}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(body if request.method == "GET" else b"", media_type="application/rss+xml; charset=utf-8",
                    headers=headers)


@router.api_route("/p/{token}/{name}", methods=["GET", "HEAD"], include_in_schema=False)
def private_audio(token: str, name: str, db: sqlite3.Connection = Depends(get_db)) -> Response:
    pod = _podcast_by_token(db, token)
    if pod is None:
        return NOT_FOUND
    key, _, suffix = name.partition(".")
    for ep in published(db, pod["id"]):
        if media_key(ep["guid"]) == key and Path(ep["cut_path"]).suffix == f".{suffix}":
            return FileResponse(ep["cut_path"], media_type=ep["cut_type"] or "audio/mpeg",
                                headers={"Cache-Control": "public, max-age=3600", "X-Robots-Tag": "noindex"})
    return NOT_FOUND
