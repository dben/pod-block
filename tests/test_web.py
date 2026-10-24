import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from fakes import HTML
from podblock.app import accounts
from podblock.app.config import Settings
from podblock.app.db import MIGRATIONS, connect, setting
from podblock.store import Store
from podblock.web.app import create_app
from podblock.web.routes.auth import safe_next


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


def as_user(c, username, password):
    other = TestClient(c.app, follow_redirects=False)
    other.post("/login", data={"username": username, "password": password, "next": "/"})
    return other


def test_admin_logs_in_with_the_key_as_password(tmp_path):
    c = client(tmp_path, api_key="test-key")
    assert c.post("/login", data={"username": "Admin ", "password": "test-key"}).status_code == 303
    assert "/users" in c.get("/", headers=HTML).text
    assert c.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401


def test_admin_adds_users_who_log_in_with_a_password(tmp_path):
    c = client(tmp_path, api_key="test-key")
    log_in(c)
    assert c.post("/users", data={"username": "Alice", "password": "password1"}).status_code == 303
    page = c.get("/users", headers=HTML).text
    assert "alice" in page and "Added alice." in page

    for name, password, why in [("alice", "password2", "already a user called alice"),
                                ("admin", "password2", "the admin"),
                                ("bob smith", "password2", "User names are"),
                                ("bob", "short", "at least 8")]:
        bad = c.post("/users", data={"username": name, "password": password})
        assert bad.status_code == 422 and why in bad.text

    alice = as_user(c, "alice", "password1")
    home = alice.get("/", headers=HTML)
    assert home.status_code == 200 and "alice" in home.text and 'href="/users"' not in home.text
    assert alice.get("/users").status_code == 403
    assert alice.post("/users", data={"username": "eve", "password": "password1"}).status_code == 403

    assert as_user(c, "alice", "wrong-one").get("/", headers=HTML).status_code == 303
    assert c.post("/login", data={"username": "nobody", "password": "password1"}).status_code == 401
    db = connect(settings(tmp_path).db_path)
    row = db.execute("SELECT password_hash, last_login_at FROM users WHERE username = 'alice'").fetchone()
    db.close()
    assert row["password_hash"].startswith("scrypt$") and "password1" not in row["password_hash"]
    assert row["last_login_at"]


def test_resetting_a_password_logs_the_user_out(tmp_path):
    c = client(tmp_path, api_key="test-key")
    log_in(c)
    c.post("/users", data={"username": "alice", "password": "password1"})
    alice = as_user(c, "alice", "password1")
    uid = accounts.log_in(connect(settings(tmp_path).db_path), "alice", "password1")["id"]

    assert c.post(f"/users/{uid}/password", data={"password": "password2"}).status_code == 303
    assert alice.get("/", headers=HTML).status_code == 303
    assert as_user(c, "alice", "password1").get("/", headers=HTML).status_code == 303
    assert as_user(c, "alice", "password2").get("/", headers=HTML).status_code == 200

    c.post(f"/users/{uid}/password", data={"password": "short"})
    assert "at least 8" in c.get("/users", headers=HTML).text


def test_changing_your_own_password(tmp_path):
    c = client(tmp_path, api_key="test-key")
    log_in(c)
    c.post("/users", data={"username": "alice", "password": "password1"})
    here, elsewhere = as_user(c, "alice", "password1"), as_user(c, "alice", "password1")

    def change(current, new, repeat=None):
        return here.post("/account/password", data={"current": current, "new": new, "repeat": repeat or new})

    assert "your current password" in change("wrong-one", "password2").text
    assert "passwords don&#39;t match" in change("password1", "password2", "password3").text
    assert "at least 8" in change("password1", "short").text
    assert change("password1", "password2").status_code == 303

    assert here.get("/account", headers=HTML).status_code == 200  # this session carries on
    assert elsewhere.get("/", headers=HTML).status_code == 303  # the others end
    assert as_user(c, "alice", "password2").get("/", headers=HTML).status_code == 200

    admin = c.post("/account/password", data={"current": "test-key", "new": "password2", "repeat": "password2"})
    assert admin.status_code == 422 and "podblock api-key" in admin.text


def test_removing_a_user(tmp_path):
    c = client(tmp_path, api_key="test-key")
    log_in(c)
    c.post("/users", data={"username": "alice", "password": "password1"})
    alice = as_user(c, "alice", "password1")
    db = connect(settings(tmp_path).db_path)
    uid = db.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()["id"]
    admin = accounts.admin_id(db)

    assert c.post(f"/users/{uid}/remove").status_code == 303
    assert alice.get("/", headers=HTML).status_code == 303
    assert c.post(f"/users/{admin}/remove").status_code == 303
    assert "admin can&#39;t be removed" in c.get("/users", headers=HTML).text
    assert [r["username"] for r in db.execute("SELECT username FROM users")] == ["admin"]
    db.close()


def test_the_shared_feed_becomes_the_admins_on_upgrade(tmp_path):
    s = settings(tmp_path, api_key="test-key")
    create_app(s)
    db = connect(s.db_path)
    db.execute("INSERT INTO podcasts (name, public_token) VALUES ('Show A', 'old-shared-token')")
    db.executescript("""DROP TABLE user_feeds;
                        DROP INDEX users_omni_token; ALTER TABLE users DROP COLUMN omni_token;
                        ALTER TABLE podcasts DROP COLUMN sort_oldest;
                        DROP TABLE grabs; ALTER TABLE episodes DROP COLUMN source_chapters;
                        ALTER TABLE episodes DROP COLUMN transcripts;
                        ALTER TABLE episodes DROP COLUMN cut_chapters;
                        ALTER TABLE episodes DROP COLUMN held_by;
                        ALTER TABLE podcasts DROP COLUMN intro_phrases;
                        ALTER TABLE podcasts DROP COLUMN outro_phrases;
                        ALTER TABLE episodes DROP COLUMN phrase_cuts;
                        DROP TABLE timings;
                        ALTER TABLE podcasts DROP COLUMN max_break;""")  # undo every step from per-user feeds on
    before = next(n for n, m in enumerate(MIGRATIONS) if isinstance(m, str) and "CREATE TABLE user_feeds" in m)
    db.execute("UPDATE settings SET value = ? WHERE key = 'app_schema'", (str(before),))
    db.commit()
    create_app(s)
    assert [tuple(r) for r in db.execute("SELECT user_id, token FROM user_feeds")] == \
        [(accounts.admin_id(db), "old-shared-token")]
    db.close()
