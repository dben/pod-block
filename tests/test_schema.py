"""The detector and the app share one database, and each changes its schema on its own.

The detector's `Store` makes its tables, and upgrades them, when it opens a
database; the app's numbered MIGRATIONS (podblock/app/db.py) then add the
app's own tables, and its own columns on the detector's `podcasts` and
`episodes`. Either side adding a name the other already uses would only
break on a database the other side has touched, so these check that they
never share a table, a column or a settings key, that a database
upgraded one step at a time ends up as a new one does, and that a step
never changes once it has shipped.
"""

import ast
import hashlib
import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from podblock.app import db as app_db
from podblock.app.db import MIGRATIONS, prepare
from podblock.store import SCHEMA_VERSION, Store

ROOT = Path(__file__).parent.parent / "podblock"

# Each migration step as it shipped (see step_hash). A database that ran a step never runs it again, so
# changing one leaves those databases as they were: change what a step does with a new step instead.
SHIPPED = ["9e41bd10322d1b18", "25b49003da210e9e", "a3b543510a6e79fe", "52c4ef83a590799a", "95f4530505c45b89",
           "da86379def3017da", "579499483c73fce2", "8584ad1da8ec8f0d", "0ecaa28be5177004", "3d3d0fb58e988193",
           "894c592d023cff02", "2279df03ba58202e", "6746b5025296d568", "03af274d2e0e3f79", "a4894b75bb3c8de0",
           "5dc3b8c724b904c2"]


def tables(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def schema(path: Path) -> dict[str, list]:
    """Every table's columns (name, type, not null, default) in order, and every index, by name."""
    db = sqlite3.connect(path)
    try:
        found = {f"table {t}": [r[1:5] for r in db.execute(f"PRAGMA table_info({t})")] for t in tables(db)}
        found |= {f"index {name}": sql for name, sql in db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")}
        return found
    finally:
        db.close()


@pytest.fixture
def detector(tmp_path):
    """A database as the detector alone makes it."""
    path = tmp_path / "detector.sqlite"
    Store(path).close()
    db = sqlite3.connect(path)
    yield db
    db.close()


def app_sql() -> str:
    return "\n".join(step for step in MIGRATIONS if isinstance(step, str))


def test_the_app_makes_no_table_the_detector_has(detector):
    made = set(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", app_sql(), re.I))
    assert made and not made & tables(detector)


def test_the_app_adds_no_column_the_detector_has(detector):
    added = re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", app_sql(), re.I)
    assert added
    for table, column in added:
        if table in tables(detector):
            theirs = {r[1] for r in detector.execute(f"PRAGMA table_info({table})")}
            assert column not in theirs, f"the detector's {table} already has {column}"


def test_the_app_keeps_its_own_settings_keys(detector):
    """The app's keys in `settings`: every literal one its code reads or writes, and the migration step."""
    keys = set(re.findall(r"INTO settings VALUES \('(\w+)'", (ROOT / "app" / "db.py").read_text()))
    for path in [*ROOT.glob("app/*.py"), *ROOT.glob("web/**/*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            name = getattr(node, "func", None)
            name = getattr(name, "attr", None) or getattr(name, "id", None)
            if name in {"setting", "set_setting"} and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                keys.add(node.args[1].value)
    assert {"app_schema", "api_key_hash", "secret_key"} <= keys  # the scan sees them
    assert not keys & {r[0] for r in detector.execute("SELECT key FROM settings")}


def test_the_detector_still_knows_a_database_the_app_has_upgraded(tmp_path):
    path = tmp_path / "podblock.sqlite"
    prepare(path)
    db = sqlite3.connect(path)
    assert dict(db.execute("SELECT key, value FROM settings WHERE key = 'schema'")) == {"schema": SCHEMA_VERSION}
    db.close()
    assert Store(path).config is not None  # opens, with its fingerprint settings as they were


@pytest.mark.parametrize("done", range(len(MIGRATIONS)))
def test_upgrading_from_any_step_ends_as_a_new_database_does(tmp_path, monkeypatch, done):
    new = tmp_path / "new.sqlite"
    prepare(new)

    old = tmp_path / "old.sqlite"
    monkeypatch.setattr(app_db, "MIGRATIONS", MIGRATIONS[:done])  # as an older app left it
    prepare(old)
    monkeypatch.undo()
    prepare(old)  # this app, upgrading it

    assert schema(old) == schema(new)
    db = sqlite3.connect(old)
    assert db.execute("SELECT value FROM settings WHERE key = 'app_schema'").fetchone()[0] == str(len(MIGRATIONS))
    db.close()


def step_hash(step) -> str:
    """What a migration step does: its SQL, or a function's source, without comments or layout."""
    text = step if isinstance(step, str) else inspect.getsource(step)
    text = " ".join(re.sub(r"(--|#)[^\n]*", "", text).split())
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def test_migration_steps_never_change_once_shipped():
    now = [step_hash(step) for step in MIGRATIONS]
    for n, (shipped, current) in enumerate(zip(SHIPPED, now, strict=False), start=1):
        assert current == shipped, f"migration step {n} changed after it shipped; add a new step instead"
    assert len(now) == len(SHIPPED), f"add the new steps' hashes to SHIPPED: {now[len(SHIPPED):]}"
