"""Fixtures the test modules share. The helpers behind them are in fakes.py."""

import shutil

import pytest

from fakes import FakeWeb, make_episodes
from podblock.app.db import connect, prepare


@pytest.fixture
def web(monkeypatch):
    """The network, as far as fetching feeds goes: put a feed's XML in `web.pages[url]`."""
    return FakeWeb(monkeypatch)


@pytest.fixture
def db(tmp_path):
    """A database made ready for the app, with its admin."""
    path = tmp_path / "podblock.sqlite"
    prepare(path)
    conn = connect(path)
    conn.execute("INSERT INTO users (username, is_admin, created_at) VALUES ('admin', 1, 0)")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def audio_files(tmp_path_factory):
    """make_episodes, made once for the whole run."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    return make_episodes(tmp_path_factory.mktemp("cdn"))


@pytest.fixture(autouse=True)
def quick_retries(monkeypatch):
    """The captions job's waits before asking the transcriber again, made short."""
    from podblock.app import captions
    monkeypatch.setattr(captions, "RETRY_AFTER", 0.0)
    monkeypatch.setattr(captions, "BUSY_WAIT", (0.05, 0.1))
