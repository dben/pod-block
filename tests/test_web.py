import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from podblock.store import Store
from podblock.web import accounts
from podblock.web.app import create_app, safe_next
from podblock.web.config import Settings
from podblock.web.db import MIGRATIONS, connect, setting

HTML = {"accept": "text/html"}


def settings(tmp_path, **kw):
    return Settings(data_dir=tmp_path / "data", media_dir=tmp_path / "media", **kw)


def client(tmp_path, **kw):
    return TestClient(create_app(settings(tmp_path, **kw)), follow_redirects=False)


def log_in(c, key="test-key"):
    return c.post("/login", data={"key": key, "next": "/"})


def test_pages_need_a_login(tmp_path):
    c = client(tmp_path, api_key="test-key")
    page = c.get("/", headers=HTML)
    assert page.status_code == 303 and page.headers["location"] == "/login?next=/"
    assert c.get("/").status_code == 401  # not a browser: no redirect
    assert c.get("/", headers={"hx-request": "true"}).headers["hx-redirect"] == "/login?next=/"
    assert c.get("/healthz").json() == {"ok": True}


def test_logging_in_with_the_key(tmp_path):
    c = client(tmp_path, api_key="test-key")
    bad = log_in(c, "wrong")
    assert bad.status_code == 401 and "didn" in bad.text

    ok = log_in(c)
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert c.get("/", headers=HTML).status_code == 200

    c.post("/logout")
    assert c.get("/", headers=HTML).status_code == 303


def test_bearer_key_works_without_a_session(tmp_path):
    c = client(tmp_path, api_key="test-key")
    assert c.get("/", headers={"authorization": "Bearer test-key"}).status_code == 200
    assert c.get("/", headers={"authorization": "Bearer nope"}).status_code == 401


def test_new_key_logs_everyone_out(tmp_path):
    c = client(tmp_path, api_key="test-key")
    log_in(c)
    db = connect(settings(tmp_path).db_path)
    new = accounts.new_api_key(db)
    db.close()

    assert c.get("/", headers=HTML).status_code == 303
    assert log_in(c, "test-key").status_code == 401
    assert log_in(c, new).status_code == 303


def test_first_start_makes_and_prints_a_key(tmp_path, capsys):
    create_app(settings(tmp_path))
    key = capsys.readouterr().err.split("\n\n")[1].strip()
    create_app(settings(tmp_path))  # a restart keeps it and prints nothing
    assert capsys.readouterr().err == ""

    c = client(tmp_path)
    assert log_in(c, key).status_code == 303


def test_app_tables_live_beside_the_detectors(tmp_path):
    s = settings(tmp_path, api_key="test-key")
    s.data_dir.mkdir(parents=True)
    store = Store(s.db_path)
    store.podcast_id("Show A", "http://a/feed")
    store.close()

    c = client(tmp_path, api_key="test-key")
    create_app(s)  # migrating again changes nothing
    log_in(c)
    assert "Show A" in c.get("/", headers=HTML).text

    db = connect(s.db_path)
    assert setting(db, "app_schema") == str(len(MIGRATIONS))
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert [r["username"] for r in db.execute("SELECT username FROM users")] == ["admin"]
    db.close()
    Store(s.db_path).close()  # the detector still opens it


def test_login_only_redirects_within_the_site():
    assert safe_next("/podcasts/3?x=1") == "/podcasts/3?x=1"
    for bad in ("//evil.example", "https://evil.example", "/\\evil.example", "", None):
        assert safe_next(bad) == "/"


def test_static_files_carry_a_version(tmp_path):
    c = client(tmp_path, api_key="test-key")
    page = c.get("/login").text
    assert "/static/app.css?v=" in page
    assert c.get("/static/app.css").status_code == 200


def test_backup_copies_the_database(tmp_path):
    import sqlite3
    from podblock.cli import main
    data = tmp_path / "data"
    store = Store(data / "podblock.sqlite")
    store.podcast_id("Show A", "http://a/feed")
    store.close()
    main(["--data-dir", str(data), "backup", str(tmp_path / "copy.sqlite")])
    copy = sqlite3.connect(tmp_path / "copy.sqlite")
    assert copy.execute("SELECT name FROM podcasts").fetchall() == [("Show A",)]
    copy.close()
    with pytest.raises(SystemExit):
        main(["--data-dir", str(data), "backup", str(tmp_path / "copy.sqlite")])
