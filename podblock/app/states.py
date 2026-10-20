"""The states an episode and a job are in, as `episodes.state` and `jobs.state` hold them.

An episode's state is what pages show of it:

- None: never asked for, or back at rest with nothing to show;
- QUEUED: a job is waiting to work on it, or a running job has taken it
  along as a comparison episode and will get to it;
- RUNNING: a job is working on it; `state_note` says on what;
- READY: it has a cut file, and is in the private feeds;
- FAILED: its job failed and it has no cut file; `state_note` says why
  (one with a cut file goes back to READY, the failure in its note);
- EXPIRED: clean-up deleted its files after its retention period; its
  report and timeline stay, so it can be downloaded and cut again.

Which job may work on an episode is decided by `episodes.held_by` and the
jobs table (jobs.py), never by this state.

A job is QUEUED, then RUNNING, then DONE, FAILED or CANCELLED. A failed or
cancelled one can be retried, as a new job; one running when the worker
stopped goes back to QUEUED when it starts again.

SQL names a single state as it is ('ready'); a group of them comes from
here, through `sql`, as which states belong together is the decision this
module records. tests/test_states.py checks that SQL and templates name
only states listed here.
"""

from __future__ import annotations

QUEUED, RUNNING = "queued", "running"
READY, FAILED, EXPIRED = "ready", "failed", "expired"  # episodes
DONE, CANCELLED = "done", "cancelled"  # jobs; FAILED too

EPISODE = (QUEUED, RUNNING, READY, FAILED, EXPIRED)
JOB = (QUEUED, RUNNING, DONE, FAILED, CANCELLED)

ACTIVE = (QUEUED, RUNNING)  # an episode or job being worked on, or waiting to be
DOWNLOADED = EPISODE  # every episode state but None: downloaded at some point, or about to be
IN_USE = (READY, QUEUED, RUNNING)  # episodes whose files are needed: clean-up leaves them be
FINISHED = (DONE, FAILED, CANCELLED)  # jobs
RETRYABLE = (FAILED, CANCELLED)  # jobs


def sql(states: tuple[str, ...]) -> str:
    """A group of states for an SQL `IN (...)`: `state IN ({sql(ACTIVE)})`."""
    return ", ".join(f"'{state}'" for state in states)
