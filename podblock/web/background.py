"""Work that runs apart from any request: feed refreshes, clean-up, and the job queue.

`Background` is that work: one thread refreshes feeds when they are due and
cleans up old downloads, and `workers` threads take jobs from the queue.

It runs in a process of its own, `podblock worker` (`run_worker`), which the
web app starts as its child and starts again if it dies (`JobProcess`). So a
crash or a memory spike while fingerprinting can't take the pages down, and
the pages stay quick while a job works. The two share only the database:
pages queue jobs there, then send the worker SIGUSR1 so it starts at once
(it also looks every IDLE_SECONDS). Only one worker process may run on a
database (`WORKER_LOCK`), since on starting it puts jobs left running back in
the queue. `PODBLOCK_JOBS=threads` runs it in the web app's own process
instead, and `none` leaves it to a `podblock worker` you run yourself.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time

from . import feeds, jobs, locks, processing, retention
from .config import Settings
from .db import connect, prepare

log = logging.getLogger(__name__)

TICK_SECONDS = 60  # between checks for feeds due a refresh
IDLE_SECONDS = 5  # between looks at an empty queue, in case a wake was missed
WORKER_LOCK = "worker.lock"  # held by the one process running jobs, beside the database
RESTART_MAX = 60  # seconds: the longest wait before starting a job process that keeps dying
PARENT_CHECK = 2  # seconds between checks that the web app that started a worker is still there

RUNNERS = {"process": processing.run, "edit": processing.run_edit, "unlearn": processing.run_unlearn}


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
        self.held: int | None = None  # the worker lock, when it runs in the web app's own process

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

    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=5)
        if self.held is not None:
            os.close(self.held)
            self.held = None

    def _refresh(self) -> None:
        while not self._stop.is_set():
            try:
                db = connect(self.settings.db_path)
                try:
                    feeds.refresh_due(db)
                    retention.expire_due(db)
                    retention.prune_store(db)
                finally:
                    db.close()
            except Exception:  # keep going; the next tick may well work
                log.exception("refreshing feeds or cleaning up failed")
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


def hold_worker(settings: Settings) -> int | None:
    """Become the one process running jobs on this database. Returns the descriptor holding the lock, or None
    if another process has it."""
    return locks.hold(settings.db_path.with_name(WORKER_LOCK))


def in_process(settings: Settings) -> Background | None:
    """Run the work in threads of this process (`PODBLOCK_JOBS=threads`), unless a worker process runs already."""
    held = hold_worker(settings)
    if held is None:
        log.error("another process runs the jobs for %s; this one won't", settings.data_dir)
        return None
    work = Background(settings, workers=settings.workers)
    work.held = held
    return work


def run_worker(settings: Settings, parent: int | None = None, ready_fd: int | None = None) -> int:
    """`podblock worker`: run jobs, feed refreshes and clean-up until SIGTERM or SIGINT, or until the web app
    that started it (`parent`) is gone. SIGUSR1 has it look at the queue now; a byte on `ready_fd` tells the
    web app it can send one (until then, one would kill it). Returns an exit code."""
    prepare(settings.db_path)
    held = hold_worker(settings)
    if held is None:
        log.error("another process runs the jobs for %s; only one may", settings.data_dir)
        return 1
    work = Background(settings, workers=settings.workers)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: work.wake())
    work.start()
    if ready_fd is not None:
        os.write(ready_fd, b"1")
        os.close(ready_fd)
    log.info("worker running jobs for %s with %d thread(s)", settings.data_dir, settings.workers)
    while not stop.wait(PARENT_CHECK):
        if parent and os.getppid() != parent:
            log.warning("the web app that started this worker is gone; stopping")
            break
    work.stop()  # a job cut short goes back in the queue when a worker next starts
    os.close(held)
    return 0


class JobProcess:
    """Runs the work in a child process, `podblock worker`, and starts it again when it dies, waiting longer
    each time it dies soon after starting. Pages call `wake()` after queuing a job."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._proc: subprocess.Popen | None = None
        self._ready = False  # it has its SIGUSR1 handler: until then a wake would kill it
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._supervise, name="podblock-jobs", daemon=True)

    def command(self, ready_fd: int) -> list[str]:
        verbose = ["-v"] if logging.getLogger().isEnabledFor(logging.INFO) else []
        return [sys.executable, "-m", "podblock", "--data-dir", str(self.settings.data_dir), *verbose, "worker",
                "--media-dir", str(self.settings.media_dir), "--workers", str(self.settings.workers),
                "--parent", str(os.getpid()), "--ready-fd", str(ready_fd)]

    def start(self) -> None:
        self._thread.start()

    def running(self) -> bool:
        """False while it is down and waiting to be started again."""
        proc = self._proc
        return proc is None or proc.poll() is None

    def wake(self) -> None:
        proc = self._proc
        if self._ready and proc is not None and proc.poll() is None and hasattr(signal, "SIGUSR1"):
            try:
                proc.send_signal(signal.SIGUSR1)
            except OSError:
                pass

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._end(timeout)
        self._thread.join(timeout=timeout)

    def _end(self, timeout: float) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _supervise(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            self._ready = False
            ready, tell = os.pipe()
            try:
                # A session of its own, so a Ctrl-C at a terminal reaches the web app, which then stops it.
                self._proc = subprocess.Popen(self.command(tell), start_new_session=True, pass_fds=(tell,))
            except OSError:
                log.exception("couldn't start the job process")
                code = None
            else:
                os.close(tell)
                tell = None
                if self._stop.is_set():  # stopped while it started
                    self._end(10)
                    return
                self._ready = os.read(ready, 1) == b"1"  # nothing, if it dies first
                code = self._proc.wait()
            finally:
                os.close(ready)
                if tell is not None:
                    os.close(tell)
            if self._stop.is_set():
                return
            if time.monotonic() - started > RESTART_MAX:
                delay = 1.0
            log.error("the job process stopped (exit code %s); starting it again in %.0f s", code, delay)
            self._stop.wait(delay)
            delay = min(delay * 2, RESTART_MAX)
