"""Read podcast RSS feeds and download episode audio."""

from __future__ import annotations

import email.utils
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

USER_AGENT = "pod-block/0.1 (+https://github.com/dben/pod-block)"
MAX_FEED_BYTES = 64 << 20  # the largest feeds seen run to a few tens of MB

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"


class FeedError(Exception):
    pass


@dataclass
class Episode:
    guid: str
    title: str
    audio_url: str
    published: datetime | None
    audio_type: str | None = None
    audio_length: int | None = None  # bytes, as the enclosure states it
    description: str | None = None  # show notes, HTML as the feed gives them
    image_url: str | None = None
    link: str | None = None
    duration: float | None = None  # seconds, as the feed states it
    season: int | None = None
    number: int | None = None
    episode_type: str | None = None  # full, trailer or bonus
    explicit: bool | None = None
    xml: str | None = None  # the whole <item>, so it can be served again unchanged

    @property
    def pub_date(self) -> str | None:
        return self.published.isoformat() if self.published else None


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(text.strip())
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _text(el: ET.Element | None, path: str) -> str | None:
    value = (el.findtext(path) or "").strip() if el is not None else ""
    return value or None


def _int(text: str | None) -> int | None:
    try:
        return int(text.strip()) if text else None
    except ValueError:
        return None


def _duration(text: str | None) -> float | None:
    """Seconds from an itunes:duration: `3723`, `62:03` or `1:02:03`."""
    if not text or not re.fullmatch(r"\d+(\.\d+)?(:\d{1,2}(\.\d+)?){0,2}", text.strip()):
        return None
    seconds = 0.0
    for part in text.strip().split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def _explicit(text: str | None) -> bool | None:
    value = (text or "").strip().lower()
    if value in {"yes", "true", "explicit"}:
        return True
    if value in {"no", "false", "clean"}:
        return False
    return None


@dataclass
class Feed:
    url: str
    title: str
    episodes: list[Episode]  # newest first
    author: str | None = None
    description: str | None = None
    image_url: str | None = None
    link: str | None = None
    language: str | None = None
    categories: list[str] = field(default_factory=list)
    explicit: bool | None = None
    xml: str | None = None  # the <channel> without its items


def _keep_prefixes(xml: bytes) -> None:
    """Have ElementTree write the feed's own namespace prefixes (itunes:, podcast:) back out."""
    try:
        for _, (prefix, uri) in ET.iterparse(io.BytesIO(xml), events=("start-ns",)):
            if prefix:
                try:
                    ET.register_namespace(prefix, uri)
                except ValueError:  # a prefix ElementTree keeps for itself, like ns0
                    pass
    except ET.ParseError:
        pass  # fromstring reports it


def parse_feed(xml: bytes, url: str = "") -> Feed:
    """The podcast's metadata and its episodes with an audio enclosure, newest first."""
    _keep_prefixes(xml)
    try:
        root = ET.fromstring(xml.lstrip())
    except ET.ParseError as e:
        raise FeedError(f"not a feed: {e}") from None
    channel = root.find("channel")
    if channel is None:
        raise FeedError("not an RSS feed: it has no <channel>")
    title = _text(channel, "title") or url

    episodes = []
    for item in channel.iter("item"):
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            continue
        audio_url = enclosure.get("url").strip()
        image = item.find(f"{ITUNES}image")
        episodes.append(Episode(
            guid=_text(item, "guid") or audio_url,
            title=_text(item, "title") or "",
            audio_url=audio_url,
            published=_parse_date(item.findtext("pubDate")),
            audio_type=enclosure.get("type"),
            audio_length=_int(enclosure.get("length")),
            description=(_text(item, f"{CONTENT}encoded") or _text(item, "description")
                         or _text(item, f"{ITUNES}summary")),
            image_url=image.get("href") if image is not None else None,
            link=_text(item, "link"),
            duration=_duration(item.findtext(f"{ITUNES}duration")),
            season=_int(item.findtext(f"{ITUNES}season")),
            number=_int(item.findtext(f"{ITUNES}episode")),
            episode_type=_text(item, f"{ITUNES}episodeType"),
            explicit=_explicit(item.findtext(f"{ITUNES}explicit")),
            xml=ET.tostring(item, encoding="unicode"),
        ))
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    episodes.sort(key=lambda e: e.published or oldest, reverse=True)

    image = channel.find(f"{ITUNES}image")
    categories = []
    for cat in channel.iter(f"{ITUNES}category"):
        name = (cat.get("text") or "").strip()
        if name and name not in categories:
            categories.append(name)
    bare = ET.Element(channel.tag, channel.attrib)
    bare.extend(child for child in channel if child.tag != "item")
    return Feed(
        url=url,
        title=title,
        episodes=episodes,
        author=_text(channel, f"{ITUNES}author") or _text(channel, "managingEditor"),
        description=_text(channel, "description") or _text(channel, f"{ITUNES}summary"),
        image_url=(image.get("href") if image is not None else None) or _text(channel, "image/url"),
        link=_text(channel, "link"),
        language=_text(channel, "language"),
        categories=categories,
        explicit=_explicit(channel.findtext(f"{ITUNES}explicit")),
        xml=ET.tostring(bare, encoding="unicode"),
    )


@dataclass
class Fetched:
    xml: bytes | None  # None when the server says nothing changed
    etag: str | None
    last_modified: str | None


def get_feed(url: str, etag: str | None = None, last_modified: str | None = None,
             timeout: float = 60, max_bytes: int = MAX_FEED_BYTES) -> Fetched:
    """Fetch a feed's XML. With the `etag` and `last_modified` of the last fetch, an
    unchanged feed costs one short request and comes back with `xml` None."""
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as resp:
        if resp.status_code == 304:
            return Fetched(None, etag, last_modified)
        resp.raise_for_status()
        body = bytearray()
        for chunk in resp.iter_content(chunk_size=1 << 16):
            body += chunk
            if len(body) > max_bytes:
                raise FeedError(f"feed is over {max_bytes >> 20} MB")
        return Fetched(bytes(body), resp.headers.get("ETag"), resp.headers.get("Last-Modified"))


def fetch_feed(url: str, timeout: float = 60) -> Feed:
    return parse_feed(get_feed(url, timeout=timeout).xml, url)


def download(url: str, dest: Path, timeout: float = 60) -> Path:
    """Download to `dest`, skipping the request when the file already exists."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.rename(dest)
    return dest
