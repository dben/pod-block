"""Locks: the ones that hold across processes as well as threads, and `detecting`.

The web app and its job process (`podblock worker`) share the database, and
a few things in it must happen one at a time across both: refreshing a feed
(two refreshes could add the same episode), upgrading the database, and
being the one worker process. Each lock is a file beside the database, held
with `flock`, which the system lets go of when the process ends, however it
ends. Where there is no `flock` (Windows), they only hold within a process.

`detecting` holds only within a process. Finding repeats, and anything else
that changes the detector's blocks, runs only in the worker process, one at
a time, under it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

detecting = threading.Lock()  # the store's blocks change while repeats are found; one change at a time

_threads: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    with _guard:
        return _threads.setdefault(str(path), threading.Lock())


def beside(db: sqlite3.Connection, name: str) -> Path | None:
    """The lock file `name` beside a connection's database; None for a database in memory."""
    file = db.execute("PRAGMA database_list").fetchone()[2]  # the main database's file; "" in memory
    return Path(file).with_name(f"{name}.lock") if file else None


@contextmanager
def exclusive(path: Path | None) -> Iterator[None]:
    """Hold the lock at `path` for the block, waiting for it if need be. None locks nothing."""
    if path is None:
        yield
        return
    with _thread_lock(path):
        if fcntl is None:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # lets go of the lock too


def hold(path: Path) -> int | None:
    """Take the lock at `path` for as long as this process runs, without waiting. Returns the file
    descriptor that holds it (close it to let go), or None if someone else has it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    if fcntl is None:
        return fd
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd
