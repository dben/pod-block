"""Put the made-up podcasts through a running pod-block, so every screenshot has something to show.

usage: seed.py DATA_DIR   (the app's PODBLOCK_DATA_DIR; the app at :8000, the feeds at :8765)
"""

import json
import sqlite3
import sys
import time

import requests

APP, SITE = "http://127.0.0.1:8000", "http://127.0.0.1:8765"
KEY = "demo-key-for-screenshots"
web = requests.Session()
web.headers["Authorization"] = f"Bearer {KEY}"
web.trust_env = False  # everything here is on localhost
db = sqlite3.connect(f"{sys.argv[1]}/podblock.sqlite", isolation_level=None)


def post(path, **kw):
    r = web.post(APP + path, allow_redirects=False, **kw)
    assert r.status_code in (200, 303), (path, r.status_code, r.text[:200])
    return r


def ids(*titles):
    return [db.execute("SELECT id FROM episodes WHERE title = ?", (t,)).fetchone()[0] for t in titles]


def download(podcast, *titles):
    post(f"/podcasts/{podcast}/episodes", data={"action": "download", "ids": ids(*titles)})


def wait():
    while db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued', 'running')").fetchone()[0]:
        time.sleep(3)
    failed = db.execute("SELECT kind, episode_id, error FROM jobs WHERE state = 'failed'").fetchall()
    assert not failed, failed


def mark(title, outro=True):
    """As the timeline does: the first section is the show's intro, and the last its outro."""
    (episode,) = ids(title)
    sections = json.loads(db.execute("SELECT report FROM reports WHERE episode_id = ?", (episode,)).fetchone()[0])[
        "sections"]
    marks = [("intro", sections[0])] + ([("outro", sections[-1])] if outro else [])
    types = [{"type": kind, "blocks": s["blocks"], "bumpers": [], "start": s["start"], "end": s["end"]}
             for kind, s in marks]
    post(f"/episodes/{episode}/edits", json={"types": types, "add": [], "remove": []})


for slug in ("placeholder-fm", "test-pattern", "the-sample-show"):  # podcasts 1, 2 and 3
    post("/podcasts", data={"url": f"{SITE}/{slug}/feed.xml"})
post("/podcasts/1/filters", data={"pattern": "rewind|best of", "scope": "all", "processing": "true"})
post("/podcasts/3/filters", data={"pattern": "^bonus", "scope": "podcast"})
post("/podcasts/1/phrases", data={"intro": "", "outro": "this was a production of podblock studios"})

# Two shows first, then the third, so the shared network ads are already known when it comes.
download(1, "Why is my laptop warm", "The cable drawer, sorted", "Printers, again", "Batteries don't lie",
         "Five apps we deleted", "Listener mail: chargers")
download(3, "We tried to bake bread", "The group chat incident", "Live from the sofa", "Is a hot dog a sandwich",
         "The worst haircut", "Our landlord's cat")
wait()
download(2, "Paper, folded", "The loudest sound", "Static, explained", "Where lost socks go",
         "The bus timetable problem", "Why toast lands butter side down")
wait()
mark("Why is my laptop warm", outro=False)  # its outro is cut by the phrase
mark("Why toast lands butter side down")
mark("Our landlord's cat")
wait()

# A section cut by hand, two episodes cleaned up, one kept.
(printers,) = ids("Printers, again")
post(f"/episodes/{printers}/edits", json={"types": [], "add": [{"start": 100, "end": 122, "as_block": False}],
                                          "remove": []})
post("/downloads/delete", data={"ids": ids("Paper, folded", "The loudest sound")})
post(f"/episodes/{ids('The bus timetable problem')[0]}/keep", data={"keep": "true"})
wait()

# Grab a few episodes through the private feeds, as a podcast app would.
for podcast in (1, 2):
    web.get(f"{APP}/podcasts/{podcast}?tab=feed")  # makes the feed's address
    token = db.execute("SELECT token FROM user_feeds WHERE podcast_id = ?", (podcast,)).fetchone()[0]
    feed = web.get(f"{APP}/p/{token}/feed.xml").text
    for url in [part.split('"')[0] for part in feed.split('<enclosure url="')[1:4]]:
        web.get(APP + url[url.index("/p/"):])
print("seeded")
