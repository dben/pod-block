"""Read podcast RSS feeds and download episode audio."""

from __future__ import annotations

import email.utils
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

USER_AGENT = "pod-block/0.1 (+https://github.com/dben/pod-block)"


@dataclass
class Episode:
    guid: str
    title: str
    audio_url: str
    published: datetime | None

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


@dataclass
class Feed:
    url: str
    title: str
    episodes: list[Episode]  # newest first


def parse_feed(xml: bytes, url: str = "") -> Feed:
    """The podcast's title and its episodes with an audio enclosure, newest first."""
    root = ET.fromstring(xml)
    channel = root.find("channel")
    title = (channel.findtext("title") if channel is not None else None) or url
    episodes = []
    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            continue
        audio_url = enclosure.get("url")
        guid = (item.findtext("guid") or audio_url).strip()
        episodes.append(Episode(
            guid=guid,
            title=(item.findtext("title") or "").strip(),
            audio_url=audio_url,
            published=_parse_date(item.findtext("pubDate")),
        ))
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    episodes.sort(key=lambda e: e.published or oldest, reverse=True)
    return Feed(url, title.strip(), episodes)


def fetch_feed(url: str, timeout: float = 60) -> Feed:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return parse_feed(resp.content, url)


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
