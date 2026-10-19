"""Work that runs on its own, apart from any request: for now, refreshing feeds."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import feeds
from .db import connect

log = logging.getLogger(__name__)

TICK_SECONDS = 60


class Background:
    def __init__(self, db_path: Path, tick: float = TICK_SECONDS):
        self.db_path = db_path
        self.tick = tick
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="podblock-background", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                db = connect(self.db_path)
                try:
                    feeds.refresh_due(db)
                finally:
                    db.close()
            except Exception:  # keep going; the next tick may well work
                log.exception("background work failed")
            self._stop.wait(self.tick)
