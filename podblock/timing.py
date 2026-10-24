"""Timing the steps of a piece of work, to see where the time goes.

A `Timer` collects one row per step: its name, how long it took, and what it
worked on (an episode, seconds of audio, a count of things). The detector's
steps take one (`pipeline.find_common_blocks`), and the app's jobs keep what
it collects (`podblock/app/timings.py`). Only steps that finish are kept, so
a step that fails or is cancelled leaves no row.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager


class Timer:
    def __init__(self):
        self.rows: list[dict] = []

    @contextmanager
    def __call__(self, step: str, episode_id: int | None = None, audio: float | None = None,
                 count: int | None = None) -> Iterator[dict]:
        """Time the block as `step`. It is given the row, to fill in `audio`, `count` or `detail` as it learns
        them."""
        row = {"step": step, "episode_id": episode_id, "audio": audio, "count": count, "detail": None}
        start = time.perf_counter()
        yield row
        self.add(**{**row, "seconds": time.perf_counter() - start})

    def add(self, step: str, seconds: float, episode_id: int | None = None, audio: float | None = None,
            count: int | None = None, detail: dict | None = None) -> None:
        """A step timed some other way."""
        self.rows.append({"step": step, "seconds": seconds, "episode_id": episode_id, "audio": audio,
                          "count": count, "detail": detail})
