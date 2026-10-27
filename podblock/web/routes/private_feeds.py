"""The private feeds and their files, at addresses that hold a token in place of a login (see
`podblock.app.private_feeds`)."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response

from ...app import chapters, grabs, private_feeds
from ...app.private_feeds import build_feed, build_omni, feed_by_token, omni_by_token, podcast_by_token, ready_episode
from ..deps import base_url, get_db, wake_worker

NOT_FOUND = PlainTextResponse("Not found", status_code=404)
RETRY_AFTER = 60  # seconds, for a fetch of an episode still being cut on demand
FILE_NAME = re.compile(r"([0-9a-f]{16})\.(chapters\.json|transcript\.(?:vtt|srt|json)|[a-z0-9]{1,5})")

router = APIRouter()


@router.api_route("/p/{token}/feed.xml", methods=["GET", "HEAD"], include_in_schema=False)
def private_feed(request: Request, token: str, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """A user's feed of one podcast, or their omni feed of all of them, newest released first."""
    return _serve_feed(request, token, db, by_download=False)


@router.api_route("/p/{token}/downloaded.xml", methods=["GET", "HEAD"], include_in_schema=False)
def private_feed_by_download(request: Request, token: str, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """The same feed, newest downloaded first."""
    return _serve_feed(request, token, db, by_download=True)


def _serve_feed(request: Request, token: str, db: sqlite3.Connection, by_download: bool) -> Response:
    if (pod := feed_by_token(db, token)) is not None:
        body = build_feed(db, pod, base_url(request), pod["user_id"], token, by_download)
    elif (user := omni_by_token(db, token)) is not None:
        body = build_omni(db, user["id"], base_url(request), token, by_download)
    else:
        return NOT_FOUND
    etag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=300", "X-Robots-Tag": "noindex"}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return Response(body if request.method == "GET" else b"", media_type="application/rss+xml; charset=utf-8",
                    headers=headers)


@router.api_route("/p/{token}/{name}", methods=["GET", "HEAD"], include_in_schema=False)
def private_file(request: Request, token: str, name: str, db: sqlite3.Connection = Depends(get_db)) -> Response:
    """An episode's cut audio, chapters or transcript, through a user's feed or omni feed. Each GET of the audio
    counts toward a grab by that user. Audio links from before feeds carried their own token still work. Audio
    of an episode offered on demand is cut again first (`private_feeds.fetch_on_demand`)."""
    m = FILE_NAME.fullmatch(name)
    if not m:
        return NOT_FOUND
    key, what = m[1], m[2]
    if (pod := feed_by_token(db, token)) is not None:
        podcast_id, user_id, via = pod["id"], pod["user_id"], "feed"
    elif (user := omni_by_token(db, token)) is not None:
        podcast_id, user_id, via = None, user["id"], "omni"
    elif (pod := podcast_by_token(db, token)) is not None and "." not in what:
        podcast_id, user_id, via = pod["id"], None, None
    else:
        return NOT_FOUND
    ep, on_demand = ready_episode(db, key, podcast_id), False
    if ep is None and "." not in what and (waiting := private_feeds.on_demand_episode(db, key, podcast_id)):
        wait = private_feeds.ON_DEMAND_WAIT if request.method == "GET" else 0
        ep, on_demand = private_feeds.fetch_on_demand(db, waiting, user_id, lambda: wake_worker(request), wait), True
        if ep is None:
            return PlainTextResponse("Being downloaded and cut. Try again in a minute.", status_code=503,
                                     headers={"Retry-After": str(RETRY_AFTER)})
    if ep is None:
        return NOT_FOUND
    headers = {"Cache-Control": "private, max-age=3600", "X-Robots-Tag": "noindex"}
    if what == "chapters.json":
        return Response(chapters.chapters_json(chapters.for_episode(db, ep)) if request.method == "GET" else b"",
                        media_type="application/json", headers=headers)
    if what.startswith("transcript."):
        fmt = what.split(".", 1)[1]
        text = chapters.transcript(ep, fmt)
        if text is None:
            return NOT_FOUND
        return Response(text.encode() if request.method == "GET" else b"",
                        media_type=f"{chapters.MIME[fmt]}; charset=utf-8", headers=headers)
    if Path(ep["cut_path"]).suffix != f".{what}" and not on_demand:  # the new cut may have another suffix
        return NOT_FOUND
    if user_id is not None and request.method == "GET":
        grabs.record(db, user_id, ep["id"], via, resumed=grabs.resumed(request.headers.get("range")))
    return FileResponse(ep["cut_path"], media_type=ep["cut_type"] or "audio/mpeg", headers=headers)
