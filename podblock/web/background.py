"""Work that runs apart from any request: feed refreshes, and the job queue.

One thread refreshes feeds when they are due. `workers` threads take jobs
from the queue; pages call `wake()` after adding one so it starts at once.
"""

from __future__ import annotations

import logging
import threading

from . import feeds, jobs, processing
from .config import Settings
from .db import connect

log = logging.getLogger(__name__)

TICK_SECONDS = 60  # between checks for feeds due a refresh
IDLE_SECONDS = 10  # between looks at an empty queue, in case a wake was missed

RUNNERS = {"process": processing.run}


def run_job(settings: Settings, db, job) -> str:
    """Run one claimed job to the end. Returns the state it finished in."""
    ctx = jobs.Context(db, job)
    try:
        RUNNERS[job["kind"]](ctx, settings)
    except jobs.Cancelled:
        jobs.finish(db, job["id"], "cancelled")
        return "cancelled"
    except Exception as e:
        if not isinstance(e, processing.ProcessError):
            log.exception("job %d (%s) failed", job["id"], job["kind"])
        jobs.finish(db, job["id"], "failed", str(e)[:500] or type(e).__name__)
        return "failed"
    jobs.finish(db, job["id"], "done")
    return "done"


def run_queue(settings: Settings) -> int:
    """Run queued jobs until none are left, in this thread. Returns how many ran."""
    db = connect(settings.db_path)
    ran = 0
    try:
        while (job := jobs.claim(db)) is not None:
            run_job(settings, db, job)
            ran += 1
    finally:
        db.close()
    return ran


class Background:
    def __init__(self, settings: Settings, workers: int = 1, tick: float = TICK_SECONDS):
        self.settings = settings
        self.tick = tick
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads = [threading.Thread(target=self._refresh, name="podblock-refresh", daemon=True)]
        self._threads += [threading.Thread(target=self._work, name=f"podblock-worker-{n}", daemon=True)
                          for n in range(max(0, workers))]

    def start(self) -> None:
        db = connect(self.settings.db_path)
        try:
            if requeued := jobs.recover(db):
                log.warning("put %d interrupted job(s) back in the queue", requeued)
        finally:
            db.close()
        for t in self._threads:
            t.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=5)

    def _refresh(self) -> None:
        while not self._stop.is_set():
            try:
                db = connect(self.settings.db_path)
                try:
                    feeds.refresh_due(db)
                finally:
                    db.close()
            except Exception:  # keep going; the next tick may well work
                log.exception("refreshing feeds failed")
            self._stop.wait(self.tick)

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                db = connect(self.settings.db_path)
                try:
                    while not self._stop.is_set() and (job := jobs.claim(db)) is not None:
                        run_job(self.settings, db, job)
                finally:
                    db.close()
            except Exception:
                log.exception("the job worker hit an error")
            self._wake.wait(IDLE_SECONDS)
            self._wake.clear()
