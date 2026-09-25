"""Where a runtime is started, and why it is nowhere in particular.

A coding agent started with no working directory of its own inherits its
parent's. Both runtimes this package spawns then read project instructions out
of the directory they wake up in — Claude Code walks up from it collecting
``CLAUDE.md``, Codex does the same for its own file — so a completion started
from a checkout would have that checkout's instructions prepended to its prompt,
billed to the Owner's subscription, from a file that changes on most commits and
does not exist at all on an Install with no clone.
Two Owners running the same Profile would get different answers according to
where they happened to be standing.

So the directory is decided at the spawn, once, for every lane. These tests are
in two halves and both are needed:

* the *observed* half spawns a real child and reads back the directory it
  actually ran in, which is the only evidence that the argument does what the
  name suggests;
* the *staged* half patches ``subprocess.run`` — the convention already used by
  ``tests/llm/test_windows_spawn.py`` — to see the directory at the moment of
  the call, while it still exists and while the lanes are in the picture.

Nothing here costs a completion: every real child is ``sys.executable``, which
``tests/spend_guard.py`` allows precisely because the suite starts it all over
the place.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from venator.llm import lanes, runtime
from venator.llm.runtime import (
    SCRATCH_PREFIX,
    Invocation,
    Request,
    Result,
    Runner,
    run_process,
)

#: What a child prints to say where it is.
REPORT_CWD = "import os; print(os.getcwd())"

#: The instruction files an agent picks up from the directory it starts in, or
#: from any directory above it. The scratch directory must sit under none of
#: them — an empty directory inside a checkout would still inherit the
#: checkout's.
AGENT_MEMORY_FILENAMES = ("CLAUDE.md", "AGENTS.md")


class Completed:
    """The shape ``subprocess.run`` returns, for a staged spawn."""

    returncode = 0
    stdout = ""
    stderr = ""


def spy_on_the_spawn(
    monkeypatch: pytest.MonkeyPatch,
    seen: dict[str, Any],
    *,
    raising: BaseException | None = None,
) -> None:
    """Record the arguments of the one spawn, and optionally fail it."""

    def spy(argv: list[str], **kwargs: Any) -> Completed:
        seen["argv"] = argv
        seen.update(kwargs)
        cwd = kwargs.get("cwd")
        seen["existed"] = isinstance(cwd, str) and Path(cwd).is_dir()
        seen["contents"] = sorted(os.listdir(cwd)) if seen["existed"] else None
        if raising is not None:
            raise raising
        return Completed()

    monkeypatch.setattr(runtime.subprocess, "run", spy)


# ---------------------------------------------------------------- observed --


def test_a_real_child_does_not_start_in_the_directory_its_parent_stands_in() -> None:
    # The whole finding in one assertion, made by asking the child rather than
    # by reading the argument that was passed to it.
    result = run_process(
        Invocation(argv=(sys.executable, "-c", REPORT_CWD), stdin="", environment={})
    )

    where = Path(result.stdout.strip())

    assert where != Path.cwd()
    assert where.name.startswith(SCRATCH_PREFIX)


def test_a_real_child_starts_where_no_agent_instructions_can_be_found() -> None:
    # Not merely "somewhere else": somewhere with nothing above it to read. An
    # empty directory made *inside* a checkout would satisfy the test above and
    # leak exactly as much as before, because the walk goes upwards.
    result = run_process(
        Invocation(argv=(sys.executable, "-c", REPORT_CWD), stdin="", environment={})
    )

    where = Path(result.stdout.strip())
    found = [
        str(directory / name)
        for directory in (where, *where.parents)
        for name in AGENT_MEMORY_FILENAMES
        if (directory / name).exists()
    ]

    assert found == []


def test_the_directory_a_real_child_ran_in_is_gone_afterwards() -> None:
    result = run_process(
        Invocation(argv=(sys.executable, "-c", REPORT_CWD), stdin="", environment={})
    )

    assert not Path(result.stdout.strip()).exists()


def test_two_children_never_share_a_directory() -> None:
    # Made per call, not once per process: two completions must not be
    # able to leave anything for each other, and a directory that outlived one
    # call is a directory something could write into.
    first = run_process(
        Invocation(argv=(sys.executable, "-c", REPORT_CWD), stdin="", environment={})
    )
    second = run_process(
        Invocation(argv=(sys.executable, "-c", REPORT_CWD), stdin="", environment={})
    )

    assert first.stdout.strip() != second.stdout.strip()


# ------------------------------------------------------------------ staged --


def test_the_directory_exists_and_is_empty_at_the_moment_of_the_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    spy_on_the_spawn(monkeypatch, seen)

    run_process(Invocation(argv=("claude", "-p"), stdin="hello", environment={}))

    assert seen["existed"] is True
    assert seen["contents"] == []


def test_the_directory_is_removed_when_the_child_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The removal belongs to the context manager rather than to a line at the
    # bottom of the happy path, so the failing routes are covered by the same
    # code. A judging chunk that hangs is the ordinary way this happens.
    seen: dict[str, Any] = {}
    spy_on_the_spawn(
        monkeypatch, seen, raising=subprocess.TimeoutExpired(cmd="claude", timeout=1.0)
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_process(Invocation(argv=("claude", "-p"), stdin="hello", environment={}))

    assert not Path(seen["cwd"]).exists()


def test_the_directory_is_removed_when_the_command_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FileNotFoundError is left to propagate so a lane can turn it into
    # LaneUnavailable; it must not also leave a directory behind on a machine
    # where every run fails that way.
    seen: dict[str, Any] = {}
    spy_on_the_spawn(monkeypatch, seen, raising=FileNotFoundError("claude"))

    with pytest.raises(FileNotFoundError):
        run_process(Invocation(argv=("claude", "-p"), stdin="hello", environment={}))

    assert not Path(seen["cwd"]).exists()


def test_a_caller_that_names_a_directory_gets_it_and_keeps_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The field is an escape hatch for a caller with a reason, not a default.
    # Nothing in this package uses it; the test that nothing does is below.
    seen: dict[str, Any] = {}
    spy_on_the_spawn(monkeypatch, seen)

    run_process(
        Invocation(
            argv=("claude", "-p"), stdin="", environment={}, cwd=str(tmp_path)
        )
    )

    assert seen["cwd"] == str(tmp_path)
    assert tmp_path.is_dir()


# ------------------------------------------------------------------- lanes --


#: A reply each lane accepts: the Claude lane reads `--output-format json`'s
#: envelope, the Codex lane reads one `--json` event line. Both are here so a
#: single runner can serve both and neither lane fails for a reason that has
#: nothing to do with what is being asserted.
CLAUDE_REPLY = json.dumps({"result": "ok"})
CODEX_REPLY = json.dumps(
    {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}
)


def recording_runner(recorded: list[Invocation], reply: str) -> Runner:
    """A runner that files the Invocation away and answers plausibly."""

    def runner(invocation: Invocation) -> Result:
        recorded.append(invocation)
        if "--help" in invocation.argv:
            return Result(returncode=0, stdout="", stderr="")
        return Result(returncode=0, stdout=reply, stderr="")

    return runner


def test_no_lane_names_a_working_directory_of_its_own() -> None:
    # The point of deciding it at the spawn. A lane that leaves the field alone
    # cannot thereby inherit the parent's directory, because there is no value
    # of the field that means "inherit" — and a lane added later inherits the
    # decision rather than having to remember it.
    recorded: list[Invocation] = []
    request = Request(prompt="hello", model="haiku")
    environ = {"PATH": os.environ.get("PATH", "")}

    lanes.ClaudeLane().complete(request, recording_runner(recorded, CLAUDE_REPLY), environ)
    lanes.CodexLane().complete(request, recording_runner(recorded, CODEX_REPLY), environ)

    assert recorded, "no lane was exercised"
    assert [invocation.cwd for invocation in recorded] == [None] * len(recorded)


def test_the_default_is_a_fresh_directory_rather_than_the_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # What that `None` then means at the boundary, asserted here so the two
    # halves of the guarantee are not two separate hopes.
    seen: dict[str, Any] = {}
    spy_on_the_spawn(monkeypatch, seen)

    run_process(Invocation(argv=("claude",), stdin="", environment={}))

    assert seen["cwd"] != os.getcwd()
    assert Path(seen["cwd"]).name.startswith(SCRATCH_PREFIX)
