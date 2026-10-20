"""Episode and job states are named in podblock/app/states.py, and only states named there are used.

SQL and templates write a single state as it is (`state = 'ready'`), so a
misspelt one would match nothing and fail quietly; these check every one.
"""

import ast
import re
from pathlib import Path

from podblock.app import states

ROOT = Path(__file__).parent.parent / "podblock"
KNOWN = set(states.EPISODE) | set(states.JOB)

ONE = re.compile(r"\bstate\s*(?:==|!=|=)\s*['\"](\w+)['\"]", re.I)  # state = 'ready', e.state == 'ready'
SOME = re.compile(r"\bstate(?:,\s*'')?\)?\s+(?:not\s+)?in\s*\(([^)]*)\)", re.I)  # state IN ('a', 'b'), COALESCE(..)


def named_states(text: str) -> set[str]:
    found = set(ONE.findall(text))
    for listed in SOME.findall(text):
        found |= set(re.findall(r"['\"](\w+)['\"]", listed))
    return found


def sources() -> list[Path]:
    return [*ROOT.rglob("*.py"), *ROOT.rglob("*.html")]


def test_only_known_states_are_named():
    used = {}
    for path in sources():
        for state in named_states(path.read_text()):
            used.setdefault(state, path.relative_to(ROOT))
    assert {"queued", "running", "ready", "failed", "expired"} <= used.keys()  # the scan sees them
    unknown = {state: str(where) for state, where in used.items() if state not in KNOWN}
    assert not unknown, f"states named nowhere in states.py: {unknown}"


def test_the_scan_catches_a_misspelt_state():
    assert named_states("UPDATE episodes SET state = 'redy' WHERE e.state NOT IN ('queued', 'runing')") == {
        "redy", "queued", "runing"}
    assert named_states("{% if e.state == 'expird' %}") == {"expird"}
    assert named_states("SET state_note = 'Finding repeats', step = 'Starting'") == set()


def test_templates_use_groups_that_exist():
    for path in ROOT.rglob("*.html"):
        for name in re.findall(r"\bstates\.(\w+)", path.read_text()):
            assert hasattr(states, name), f"{path.name} uses states.{name}, which states.py doesn't have"


def test_groups_hold_known_states():
    for name in ("ACTIVE", "DOWNLOADED", "IN_USE", "FINISHED", "RETRYABLE"):
        assert set(getattr(states, name)) <= KNOWN, name
    assert states.sql(states.ACTIVE) == "'queued', 'running'"


def test_sql_groups_are_only_put_in_f_strings():
    """`{sql(...)}` in a plain string would reach SQLite as it is, and match nothing."""
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text())
        docstrings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                assert "{sql(" not in node.value, f"{path.name}:{node.lineno} isn't an f-string"
