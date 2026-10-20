"""Find podcasts by name in Apple's podcast directory, which gives each show's RSS feed address."""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

from ..feed import USER_AGENT

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
LIMIT = 24
TIMEOUT = 15

_APPLE_PAGE = re.compile(r"https?://(?:podcasts|itunes)\.apple\.com/.*?\bid(\d+)", re.I)
_BARE_ADDRESS = re.compile(r"[\w-]+(\.[\w-]+)+/\S*")  # example.com/feed, with no scheme


class DirectoryError(Exception):
    pass


@dataclass
class Show:
    name: str
    author: str
    feed_url: str
    image_url: str | None = None
    genre: str | None = None
    episodes: int | None = None


def as_address(text: str) -> str | None:
    """The web address in what someone typed, or None when it reads as a search.

    Takes `feed://` and the like, which podcast apps use, and a host and path with no scheme.
    """
    text = text.strip()
    if re.match(r"(https?|feed|itpc|pcast|podcast)://\S+$", text, re.I):
        return re.sub(r"^(feed|itpc|pcast|podcast)://", "https://", text, flags=re.I)
    if _BARE_ADDRESS.fullmatch(text):
        return "https://" + text
    return None


def _get(url: str, **params) -> list[dict]:
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except requests.HTTPError as e:
        raise DirectoryError(f"the podcast directory answered {e.response.status_code}") from None
    except requests.RequestException as e:
        raise DirectoryError(f"couldn't reach the podcast directory ({type(e).__name__})") from None
    except ValueError:
        raise DirectoryError("the podcast directory sent back something we couldn't read") from None


def _show(r: dict) -> Show | None:
    if not r.get("feedUrl"):
        return None  # Apple lists some shows it hosts no feed for
    return Show(name=r.get("collectionName") or r.get("trackName") or "Untitled", author=r.get("artistName") or "",
                feed_url=r["feedUrl"], image_url=r.get("artworkUrl600") or r.get("artworkUrl100"),
                genre=r.get("primaryGenreName"), episodes=r.get("trackCount"))


def search(term: str, limit: int = LIMIT) -> list[Show]:
    results = _get(SEARCH_URL, term=term, media="podcast", entity="podcast", limit=limit)
    return [s for s in map(_show, results) if s]


def apple_feed(url: str) -> str | None:
    """The feed behind an Apple Podcasts page, or None when `url` isn't one."""
    m = _APPLE_PAGE.match(url)
    if not m:
        return None
    shows = [s for s in map(_show, _get(LOOKUP_URL, id=m.group(1), entity="podcast")) if s]
    if not shows:
        raise DirectoryError("Apple Podcasts has no feed for that show")
    return shows[0].feed_url
