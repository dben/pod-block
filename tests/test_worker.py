"""Jobs run in a process of their own, `podblock worker`, which the web app starts and keeps going."""

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from test_feeds import rss
from test_processing import audio_files  # noqa: F401  (a fixture)

from podblock.web import background, locks
from podblock.web.app import create_app
from podblock.web.config import Settings
from podblock.web.db import connect

HOLD = """
import sys, time
from pathlib import Path
from podblock.web import locks
with locks.exclusive(Path(sys.argv[1])):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_a_lock_holds_across_processes(tmp_path):
    path = tmp_path / "x.lock"
    other = subprocess.Popen([sys.executable, "-c", HOLD, str(path), "1.5"], stdout=subprocess.PIPE, text=True)
    assert other.stdout.readline().strip() == "held"
    started = time.monotonic()
    with locks.exclusive(path):
        waited = time.monotonic() - started
    other.wait()
    assert waited > 0.8  # it waited for the other process to let go

    assert (held := locks.hold(path)) is not None
    taken = subprocess.run([sys.executable, "-c", "import sys; from pathlib import Path; from podblock.web import locks; "
                            "print(locks.hold(Path(sys.argv[1])))", str(path)], capture_output=True, text=True)
    assert taken.stdout.strip() == "None"
    os.close(held)


def settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path / "data", media_dir=tmp_path / "media", api_key="k", **kw)


def test_only_one_process_runs_the_jobs(tmp_path):
    s = settings(tmp_path)
    create_app(s)
    held = background.hold_worker(s)
    try:
        assert background.run_worker(s) == 1
        assert background.in_process(s) is None
    finally:
        os.close(held)
    assert subprocess.run([sys.executable, "-m", "podblock", "--data-dir", str(s.data_dir), "worker", "--parent",
                           "1"], timeout=30).returncode == 0  # runs, sees it has no such parent, and stops


def test_the_jobs_setting_is_checked(monkeypatch):
    monkeypatch.setenv("PODBLOCK_JOBS", "Threads")
    assert Settings.from_env().jobs == "threads"
    monkeypatch.setenv("PODBLOCK_JOBS", "sideways")
    with pytest.raises(ValueError, match="PODBLOCK_JOBS"):
        Settings.from_env()


@pytest.fixture
def site(tmp_path, audio_files):  # noqa: F811
    """A web server with the feed and its audio, for a worker process to download from."""
    root = tmp_path / "site"
    root.mkdir()
    for url, path in audio_files.items():
        shutil.copy(path, root / url.rsplit("/", 1)[1])
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    items = "".join(f"""<item><title>Episode {n}</title><guid>ep-{n}</guid>
        <pubDate>{n:02d} Jan 2026 10:00:00 +0000</pubDate>
        <enclosure url="{base}/{n}.mp3" type="audio/mpeg" length="1000"/></item>""" for n in range(1, 6))
    (root / "show.rss").write_bytes(rss(items))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield base
    server.shutdown()


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def wait_for(check, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if result := check():
            return result
        time.sleep(0.2)
    raise AssertionError("timed out")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_the_web_app_runs_jobs_in_a_worker_process(tmp_path, site, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    s = settings(tmp_path)
    app = create_app(s)
    with TestClient(app, follow_redirects=False) as client:
        jobs = app.state.background
        assert isinstance(jobs, background.JobProcess)
        first = wait_for(lambda: jobs._proc)
        assert first.pid != os.getpid()
        client.post("/login", data={"key": "k"})
        pod = int(client.post("/podcasts", data={"url": f"{site}/show.rss"}).headers["location"].rsplit("/", 1)[1])
        db = connect(s.db_path)
        two = db.execute("SELECT id FROM episodes WHERE guid = 'ep-2'").fetchone()[0]
        client.post(f"/podcasts/{pod}/episodes", data={"action": "download", "ids": two})
        state = lambda guid: db.execute("SELECT state FROM episodes WHERE guid = ?", (guid,)).fetchone()[0]  # noqa: E731
        idle = lambda: db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued', 'running')").fetchone()[0] == 0  # noqa: E731
        wait_for(idle)
        assert [state(f"ep-{n}") for n in (2, 3, 4, 5)] == ["ready"] * 4  # its comparison episodes came along
        assert db.execute("SELECT state FROM jobs").fetchone()[0] == "done"

        first.send_signal(signal.SIGKILL)  # the worker dies...
        first.wait()
        assert "The job process stopped" in client.get("/jobs").text
        client.post(f"/podcasts/{pod}/episodes", data={"action": "download", "ids": db.execute(
            "SELECT id FROM episodes WHERE guid = 'ep-1'").fetchone()[0]})
        second = wait_for(lambda: jobs._proc if jobs._proc is not first and jobs.running() else None)
        wait_for(lambda: idle() and state("ep-1") == "ready")  # ...and the next one takes the queue on
        assert "The job process stopped" not in client.get("/jobs").text
        db.close()
    assert second.poll() is not None  # the web app stopping stops it too
