"""Two workers must never work on the same episode at once."""

import pytest

from podblock.web import jobs
from podblock.web.db import connect, prepare


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "podblock.sqlite"
    prepare(path)
    db = connect(path)
    db.execute("INSERT INTO podcasts (id, name) VALUES (1, 'Show')")
    db.executemany("INSERT INTO episodes (id, guid, podcast_id) VALUES (?, ?, 1)", [(n, f"ep-{n}") for n in (1, 2, 3)])
    db.commit()
    db.close()
    return path


def test_a_recut_asked_for_while_a_job_holds_the_episode_runs_once_after_it(path):
    one, two = connect(path), connect(path)  # two workers
    jobs.enqueue(one, "process", [1], None)
    a = jobs.claim(one)  # takes episode 2 along for comparison, cuts it, then draws its waveform
    jobs.take_along(one, a["id"], [2], "Coming along")
    one.execute("UPDATE episodes SET state = 'ready' WHERE id = 2")  # cut: ready for a moment
    one.commit()
    assert jobs.enqueue(one, "process", [2], None, skip_ready=False) == [2]  # another job found new repeats
    one.execute("UPDATE episodes SET state = 'running' WHERE id = 2")  # drawing its waveform
    one.execute("UPDATE episodes SET state = 'ready' WHERE id = 2")
    one.commit()
    assert jobs.enqueue(one, "process", [2], None, skip_ready=False) == []  # already waiting: not twice
    assert jobs.claim(two) is None  # episode 2 is still job a's

    jobs.finish(one, a["id"], "done")
    assert one.execute("SELECT state FROM episodes WHERE id = 2").fetchone()[0] == "queued"
    b = jobs.claim(two)
    assert b["episode_id"] == 2 and jobs.claim(one) is None
    one.close()
    two.close()


def test_held_episodes_are_not_taken_along_or_downloaded_again(path):
    db = connect(path)
    jobs.enqueue(db, "process", [1], None)
    a = jobs.claim(db)
    assert jobs.take_along(db, a["id"], [2, 3], "Coming along") == [2, 3]
    jobs.enqueue(db, "process", [3], None)
    assert jobs.take_along(db, a["id"], [3], "Again") == []
    assert jobs.enqueue(db, "process", [2], None) == []  # a plain download of an episode being processed: no need
    jobs.finish(db, a["id"], "failed", "boom")
    assert db.execute("SELECT COUNT(*) FROM episodes WHERE held_by IS NOT NULL").fetchone()[0] == 0
    db.close()


def test_a_restart_lets_go_of_everything(path):
    db = connect(path)
    jobs.enqueue(db, "process", [1], None)
    a = jobs.claim(db)
    jobs.take_along(db, a["id"], [2], "Coming along")
    jobs.recover(db)
    assert db.execute("SELECT COUNT(*) FROM episodes WHERE held_by IS NOT NULL").fetchone()[0] == 0
    assert jobs.claim(db)["id"] == a["id"]
    db.close()


def test_two_jobs_for_one_episode_never_run_at_once(path):
    one, two = connect(path), connect(path)
    for _ in range(2):  # however two came to be queued
        one.execute("INSERT INTO jobs (kind, episode_id, created_at) VALUES ('process', 1, 0)")
    one.execute("INSERT INTO jobs (kind, episode_id, created_at) VALUES ('process', 3, 0)")
    one.commit()
    first = jobs.claim(one)
    assert jobs.claim(two)["episode_id"] == 3  # the other worker skips past it to the next free one
    assert jobs.claim(two) is None
    jobs.finish(one, first["id"], "done")
    assert jobs.claim(two)["episode_id"] == 1
    one.close()
    two.close()
