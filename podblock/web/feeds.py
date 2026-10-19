"""Follow feeds: add them, refresh them, and keep our own copy of each episode.

An episode that leaves its feed is deleted, unless it was ever downloaded:
then it stays, marked `in_feed = 0`, so the private feed and its report
outlive the source.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser

import requests

from ..feed import CONTENT, ITUNES, Feed, FeedError, get_feed, parse_feed
from . import filters, jobs
from .db import new_token

log = logging.getLogger(__name__)

SUMMARY_CHARS = 400
AUTO_DOWNLOAD_MAX = 5  # a refresh queues at most this many new episodes, should a feed re-issue its back catalogue
_refreshing = threading.Lock()  # one refresh at a time, so two can't insert the same episode


class RefreshError(Exception):
    pass


@dataclass
class Refreshed:
    podcast_id: int
    added: int = 0
    updated: int = 0
    removed: int = 0  # deleted: left the feed, never downloaded
    gone: int = 0  # left the feed but kept, since they were downloaded
    unchanged: bool = False  # the server said nothing changed since the last refresh
    new_ids: list[int] = field(default_factory=list)  # episodes added
    queued: int = 0  # new episodes queued to download, for podcasts that download them on their own
    moved_to: str | None = None  # the feed's new address, when it moved

    def describe(self) -> str:
        if self.unchanged or not (self.added or self.removed or self.gone):
            return "No new episodes." + (" The feed has moved; following it." if self.moved_to else "")
        parts = []
        if self.added:
            parts.append(f"{self.added} new {'episode' if self.added == 1 else 'episodes'}")
        if self.removed:
            parts.append(f"{self.removed} no longer in the feed removed")
        if self.gone:
            parts.append(f"{self.gone} downloaded {'one' if self.gone == 1 else 'ones'} left the feed and kept")
        if self.queued:
            parts.append(f"{self.queued} queued to download")
        return ", ".join(parts).capitalize() + "." + (" The feed has moved; following it." if self.moved_to else "")


class _Text(HTMLParser):
    BREAKS = {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str | None) -> str:
    """Show notes as plain text, one paragraph per line. Safe to show anywhere."""
    if not html:
        return ""
    for _ in range(2):  # some feeds escape their HTML twice
        parser = _Text()
        parser.feed(html)
        parser.close()
        html = "".join(parser.parts)
        if not re.search(r"<(p|br|a|div|span|li)\b", html, re.I):
            break
    lines = (re.sub(r"[ \t\r\f\v ]+", " ", line).strip() for line in html.split("\n"))
    return "\n".join(line for line in lines if line)


def summary(html: str | None) -> str:
    text = " ".join(html_to_text(html).split("\n"))
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"


def _fetch(url: str, etag: str | None = None, last_modified: str | None = None):
    try:
        return get_feed(url, etag, last_modified)
    except requests.HTTPError as e:
        raise RefreshError(f"the server answered {e.response.status_code}") from None
    except requests.RequestException as e:
        raise RefreshError(f"couldn't reach it ({type(e).__name__})") from None
    except FeedError as e:
        raise RefreshError(str(e)) from None


def _parse(xml: bytes, url: str) -> Feed:
    try:
        return parse_feed(xml, url)
    except FeedError as e:
        raise RefreshError(str(e)) from None


def add_podcast(db: sqlite3.Connection, url: str) -> Refreshed:
    """Follow a feed, or follow it again if it was removed, and fetch it."""
    url = url.strip()
    if not re.match(r"https?://[^\s/]+", url, re.I):
        raise RefreshError("that isn't a web address; it should start with http:// or https://")
    row = db.execute("SELECT id FROM podcasts WHERE feed_url = ?", (url,)).fetchone()
    if row:
        db.execute("UPDATE podcasts SET followed = 1 WHERE id = ?", (row["id"],))
        db.commit()
        return refresh(db, row["id"], conditional=False)
    fetched = _fetch(url)
    feed = _parse(fetched.xml, url)
    with _refreshing:
        cur = db.execute("INSERT INTO podcasts (name, feed_url, added_at, public_token) VALUES (?, ?, ?, ?)",
                         (feed.title, url, time.time(), new_token()))
        result = _apply(db, cur.lastrowid, feed, fetched.etag, fetched.last_modified)
    return result


def refresh(db: sqlite3.Connection, podcast_id: int, conditional: bool = True) -> Refreshed:
    """Fetch a followed feed again and bring our copy up to date.

    Failures are kept in `last_error` as well as raised.
    """
    pod = db.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,)).fetchone()
    if pod is None or not pod["feed_url"]:
        raise RefreshError("this podcast has no feed to refresh")
    try:
        fetched = _fetch(pod["feed_url"], *((pod["feed_etag"], pod["feed_last_modified"]) if conditional else ()))
        if fetched.xml is None:
            db.execute("UPDATE podcasts SET last_refreshed_at = ?, last_error = NULL WHERE id = ?",
                       (time.time(), podcast_id))
            db.commit()
            return Refreshed(podcast_id, unchanged=True)
        feed = _parse(fetched.xml, pod["feed_url"])
    except RefreshError as e:
        db.execute("UPDATE podcasts SET last_refreshed_at = ?, last_error = ? WHERE id = ?",
                   (time.time(), str(e), podcast_id))
        db.commit()
        raise
    with _refreshing:
        result = _apply(db, podcast_id, feed, fetched.etag, fetched.last_modified)
    result.moved_to = _follow_move(db, pod, fetched.moved_to or feed.new_url)
    if pod["auto_download"] and result.new_ids:
        filtered = {r["id"] for r in db.execute("SELECT id FROM episodes WHERE podcast_id = ? AND skip_processing = 1",
                                                (podcast_id,))}
        wanted = [ep_id for ep_id in result.new_ids if ep_id not in filtered]
        result.queued = len(jobs.enqueue(db, "process", wanted[:AUTO_DOWNLOAD_MAX], None))
    return result


def _follow_move(db: sqlite3.Connection, pod: sqlite3.Row, new_url: str | None) -> str | None:
    """Point a podcast at its feed's new address, unless that is another podcast's already."""
    if not new_url or new_url == pod["feed_url"] or not re.match(r"https?://[^\s/]+", new_url, re.I):
        return None
    if db.execute("SELECT 1 FROM podcasts WHERE feed_url = ?", (new_url,)).fetchone():
        log.warning("podcast %d's feed moved to %s, which another podcast has", pod["id"], new_url)
        return None
    db.execute("UPDATE podcasts SET feed_url = ?, feed_etag = NULL, feed_last_modified = NULL WHERE id = ?",
               (new_url, pod["id"]))
    db.commit()
    log.info("podcast %d's feed moved to %s", pod["id"], new_url)
    return new_url


def _episode_key(db: sqlite3.Connection, podcast_id: int, feed_url: str, guid: str) -> tuple[str, int | None]:
    """The guid we store an episode under, and its row id if we have it.

    Guids are unique across the store. In the rare case another show already
    uses this one, this show's copy is stored under its feed URL plus guid.
    """
    for key in (guid, f"{feed_url}#{guid}"):
        row = db.execute("SELECT id, podcast_id FROM episodes WHERE guid = ?", (key,)).fetchone()
        if row is None:
            return key, None
        if row["podcast_id"] in (podcast_id, None):
            return key, row["id"]
    return f"{feed_url}#{guid}", None


def _apply(db: sqlite3.Connection, podcast_id: int, feed: Feed, etag: str | None,
           last_modified: str | None) -> Refreshed:
    result = Refreshed(podcast_id)
    now = time.time()
    db.execute(
        """UPDATE podcasts SET name = ?, author = ?, description = ?, image_url = ?, link = ?, language = ?,
                  categories = ?, explicit = ?, channel_xml = ?, feed_etag = ?, feed_last_modified = ?,
                  last_refreshed_at = ?, last_error = NULL
           WHERE id = ?""",
        (feed.title, feed.author, feed.description, feed.image_url, feed.link, feed.language,
         json.dumps(feed.categories), feed.explicit, feed.xml, etag, last_modified, now, podcast_id))

    seen: set[int] = set()
    for ep in feed.episodes:
        key, ep_id = _episode_key(db, podcast_id, feed.url, ep.guid)
        if ep_id in seen:  # the feed lists the same guid twice; keep the first, newest
            continue
        fields = dict(
            podcast_id=podcast_id, title=ep.title, pub_date=ep.pub_date,
            pub_ts=ep.published.timestamp() if ep.published else None,
            audio_url=ep.audio_url, audio_type=ep.audio_type, audio_length=ep.audio_length,
            summary=summary(ep.description), image_url=ep.image_url, link=ep.link,
            feed_duration=ep.duration, season=ep.season, episode_number=ep.number,
            episode_type=ep.episode_type, explicit=ep.explicit, item_xml=ep.xml, in_feed=1,
        )
        if ep_id is None:
            cols = ["guid", "added_at", *fields]
            cur = db.execute(f"INSERT INTO episodes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [key, now, *fields.values()])
            ep_id = cur.lastrowid
            result.added += 1
            result.new_ids.append(ep_id)
        else:
            db.execute(f"UPDATE episodes SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                       [*fields.values(), ep_id])
            result.updated += 1
        seen.add(ep_id)

    if feed.episodes:  # an empty feed is more likely broken than emptied; leave what we have
        missing = [r for r in db.execute(
            "SELECT id, in_feed, ever_downloaded, fingerprint_path, state FROM episodes WHERE podcast_id = ?",
            (podcast_id,)) if r["id"] not in seen]
        keep = _known_to_detector(db)
        for r in missing:
            if _downloaded(r, keep):
                if r["in_feed"]:
                    db.execute("UPDATE episodes SET in_feed = 0 WHERE id = ?", (r["id"],))
                    result.gone += 1
            else:
                db.execute("DELETE FROM compared_pairs WHERE a_id = ? OR b_id = ?", (r["id"], r["id"]))
                db.execute("DELETE FROM episodes WHERE id = ?", (r["id"],))
                result.removed += 1
    db.commit()
    filters.mark(db, podcast_id)  # new episodes, or new titles, may match a processing filter
    log.info("refreshed podcast %d: %s", podcast_id, result.describe())
    return result


def _known_to_detector(db: sqlite3.Connection) -> set[int]:
    return {r["id"] for r in db.execute(
        "SELECT DISTINCT episode_id AS id FROM occurrences UNION SELECT episode_id FROM reports")}


def _downloaded(row: sqlite3.Row, known: set[int]) -> bool:
    """Whether an episode is ours to keep: downloaded, being downloaded, or used by the detector."""
    return bool(row["ever_downloaded"] or row["fingerprint_path"] or row["id"] in known
                or row["state"] in jobs.ACTIVE)


def remove_podcast(db: sqlite3.Connection, podcast_id: int) -> int:
    """Stop following a podcast, and take it off every user's home page. Episodes never downloaded go;
    returns how many stayed.

    The podcast row stays, hidden, with its downloaded episodes and the blocks
    learned from it: those blocks may be ads shared with other shows.
    """
    for job in db.execute("""SELECT j.id FROM jobs j JOIN episodes e ON e.id = j.episode_id
                             WHERE e.podcast_id = ? AND j.state IN ('queued', 'running')""",
                          (podcast_id,)).fetchall():
        jobs.cancel(db, job["id"])
    stayed = 0
    keep = _known_to_detector(db)
    for r in db.execute("SELECT id, ever_downloaded, fingerprint_path, state FROM episodes WHERE podcast_id = ?",
                        (podcast_id,)).fetchall():
        if _downloaded(r, keep):
            stayed += 1
        else:
            db.execute("DELETE FROM compared_pairs WHERE a_id = ? OR b_id = ?", (r["id"], r["id"]))
            db.execute("DELETE FROM episodes WHERE id = ?", (r["id"],))
    db.execute("UPDATE podcasts SET followed = 0, feed_etag = NULL, feed_last_modified = NULL WHERE id = ?",
               (podcast_id,))
    db.execute("DELETE FROM user_podcasts WHERE podcast_id = ?", (podcast_id,))
    db.commit()
    return stayed


def refresh_due(db: sqlite3.Connection, now: float | None = None) -> list[int]:
    """Refresh every followed feed whose refresh is due. Returns their ids."""
    now = now or time.time()
    due = [r["id"] for r in db.execute(
        """SELECT id FROM podcasts
           WHERE followed = 1 AND feed_url IS NOT NULL
             AND (last_refreshed_at IS NULL OR last_refreshed_at + refresh_minutes * 60 <= ?)
           ORDER BY last_refreshed_at IS NOT NULL, last_refreshed_at""", (now,))]
    for podcast_id in due:
        try:
            refresh(db, podcast_id)
        except RefreshError as e:
            log.warning("refreshing podcast %d failed: %s", podcast_id, e)
    return due


def show_notes(item_xml: str | None) -> str:
    """An episode's full show notes as plain text, from the <item> we kept."""
    if not item_xml:
        return ""
    try:
        item = ET.fromstring(item_xml)
    except ET.ParseError:
        return ""
    for path in (f"{CONTENT}encoded", "description", f"{ITUNES}summary"):
        text = item.findtext(path)
        if text and text.strip():
            return html_to_text(text)
    return ""
