"""The CI guard's one judgement call: what counts as a behaviour change.

The guard in ``.github/scripts/filters_version_guard.py`` fails a pull request
that changes filter-deciding code without moving ``filters_version``. Everything
else it does is mechanical — read two revisions, run each side's own hashing
code, compare — but this one function decides whether a change is worth failing
over, and it is the part that decides whether the check is a guard or a nuisance.

Both directions are load-bearing and both are pinned here. A comment expansion
must not fail the check: one of the two pull requests that prompted the guard
was exactly that and nothing more, and a check that fires on prose is a check
people learn to route around. A changed literal must fail it: a threshold, a
reason string and a regex all decide verdicts, and the conservative direction
for anything else is to call it a change.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

GUARD_PATH = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "filters_version_guard.py"


def load_guard() -> ModuleType:
    """Import the guard by path — it is a CI script, not part of the package."""
    spec = importlib.util.spec_from_file_location("filters_version_guard", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = load_guard()


def test_the_guard_script_is_where_the_workflow_looks_for_it() -> None:
    assert GUARD_PATH.exists()
    workflow = (GUARD_PATH.parents[1] / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert ".github/scripts/filters_version_guard.py" in workflow


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param("x = 1\n", "# a new comment\nx = 1\n", id="added comment"),
        pytest.param("x = 1  # old\n", "x = 1  # new\n", id="reworded comment"),
        pytest.param(
            '"""One sentence."""\nx = 1\n',
            '"""A much longer sentence, rewritten at length."""\nx = 1\n',
            id="module docstring",
        ),
        pytest.param(
            'def f() -> int:\n    """Old."""\n    return 1\n',
            'def f() -> int:\n    """New, and longer."""\n    return 1\n',
            id="function docstring",
        ),
        pytest.param(
            'class C:\n    """Old."""\n    x = 1\n',
            'class C:\n    """New."""\n    x = 1\n',
            id="class docstring",
        ),
        pytest.param("x = 1\n", "\n\nx = 1\n\n\n", id="blank lines"),
        pytest.param("f(1, 2)\n", "f(\n    1,\n    2,\n)\n", id="rewrapped call"),
    ],
)
def test_prose_and_layout_are_not_a_behaviour_change(before: str, after: str) -> None:
    assert guard.behaviour(before) == guard.behaviour(after)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param("THRESHOLD = 75\n", "THRESHOLD = 70\n", id="threshold"),
        pytest.param('REASON = "kill"\n', 'REASON = "killed"\n', id="reason string"),
        pytest.param(
            'PATTERN = re.compile(r"\\bphd\\b")\n',
            'PATTERN = re.compile(r"\\bphd\\b|\\bdphil\\b")\n',
            id="regex",
        ),
        pytest.param(
            "def f(a: int) -> int:\n    return a\n",
            "def f(a: int) -> int:\n    return a + 1\n",
            id="expression",
        ),
        pytest.param(
            'def f() -> str:\n    """Doc."""\n    return "a"\n',
            'def f() -> str:\n    """Doc."""\n    return "b"\n',
            id="return value under an unchanged docstring",
        ),
        pytest.param(
            "x = 1\n",
            'x = 1\n\n\ndef g() -> None:\n    """Doc."""\n',
            id="new function",
        ),
    ],
)
def test_a_changed_literal_or_statement_is_a_behaviour_change(before: str, after: str) -> None:
    assert guard.behaviour(before) != guard.behaviour(after)


def test_a_string_that_is_not_a_docstring_still_counts() -> None:
    """Only the *first* statement is prose; a string anywhere else is data."""
    before = 'def f() -> None:\n    """Doc."""\n    x = "before"\n'
    after = 'def f() -> None:\n    """Doc."""\n    x = "after"\n'
    assert guard.behaviour(before) != guard.behaviour(after)


def test_the_two_tiers_are_disjoint_and_are_the_whole_watched_set() -> None:
    """A file in both tiers would be judged by whichever list was read first."""
    assert not set(guard.RULE_FILES) & set(guard.PLUMBING_FILES)
    assert set(guard.WATCHED) == set(guard.RULE_FILES) | set(guard.PLUMBING_FILES)


def test_the_hard_filters_themselves_are_a_rule_file() -> None:
    """The one entry the whole check is pointless without."""
    assert "src/venator/match/filters.py" in guard.RULE_FILES


def test_every_watched_file_exists() -> None:
    """A watched path that has been renamed away silently stops being watched."""
    root = GUARD_PATH.parents[2]
    missing = [path for path in guard.WATCHED if not (root / path).exists()]
    assert not missing, f"the guard watches paths that no longer exist: {missing}"


def test_the_files_filters_version_hashes_directly_are_not_also_watched() -> None:
    """They cannot fail the check: editing one moves the version by construction."""
    hashed = ("profiles/sample/constraints.yaml", "profiles/sample/targeting.yaml")
    assert not set(guard.WATCHED) & set(hashed)
