"""Grabs: each time a user gets an episode, through their private feed or in the app.

Every audio link in a user's feed, or omni feed, carries that feed's token, so
a request for it says whose it is. Podcast apps fetch an episode in pieces
(byte ranges), some stream it while a second download runs alongside, and
every seek is another request; so a hit within WINDOW of the last one counts
as the same grab. A hit that starts partway into the file is a seek or a
resume, so it only opens a grab of its own when there was none before.

Playing an episode on its page counts too, once the player has played the
longer of APP_MIN_SEC and APP_MIN_SHARE of it (or most of it, for an episode
shorter than that); so does the page's Download button.
"""

from __future__ import annotations

import re
import sqlite3
import time

WINDOW = 10 * 60  # seconds: hits closer than this to the last one are the same grab
APP_MIN_SEC = 5 * 60
APP_MIN_SHARE = 0.05
APP_MAX_SHARE = 0.9  # an episode shorter than APP_MIN_SEC counts once this much of it has played


def record(db: sqlite3.Connection, user_id: int, episode_id: int, via: str, resumed: bool = False,
           now: float | None = None) -> bool:
    """Count a hit on an episode's audio by a user. Returns whether it started a new grab."""
    now = now or time.time()
    last = db.execute("SELECT id, last_at FROM grabs WHERE user_id = ? AND episode_id = ? ORDER BY last_at DESC LIMIT 1",
                      (user_id, episode_id)).fetchone()
    same = last is not None and (resumed or now - last["last_at"] <= WINDOW)
    if same:
        db.execute("UPDATE grabs SET last_at = ?, hits = hits + 1 WHERE id = ?", (now, last["id"]))
    else:
        db.execute("INSERT INTO grabs (user_id, episode_id, via, first_at, last_at) VALUES (?, ?, ?, ?, ?)",
                   (user_id, episode_id, via, now, now))
    db.commit()
    return not same


def resumed(range_header: str | None) -> bool:
    """Whether a request asks for audio partway into the file: a seek or a resume, not a new download."""
    m = re.match(r"\s*bytes\s*=\s*(\d*)\s*-", range_header or "")
    return bool(m) and (not m[1] or int(m[1]) > 0)


def play_needed(length: float | None) -> float:
    """Seconds an episode must play on its page to count as a grab."""
    need = max(APP_MIN_SEC, APP_MIN_SHARE * (length or 0))
    return min(need, APP_MAX_SHARE * length) if length else need
