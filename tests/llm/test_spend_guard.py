"""The guard that stops a test paying for a completion, tested.

A guard nobody exercises is a comment. These assert the three things that make
it worth having: it is installed without any test opting in, it refuses the
commands that cost money, and it refuses them *on the path that actually
leaked* — an adapter call with no runner stubbed, which is the shape that put
five real judging runs on the Owner's subscription.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from spend_guard import BILLED_COMMANDS, billed_runtime
from venator.llm import complete


def test_the_guard_is_installed_without_anyone_asking_for_it() -> None:
    # Autouse and session-scoped: this test declares no fixture at all, and the
    # boundary is already wrapped by the time it runs.
    with pytest.raises(AssertionError, match="spends the Owner's subscription"):
        subprocess.run(["claude", "-p"], capture_output=True)


@pytest.mark.parametrize("command", sorted(BILLED_COMMANDS))
def test_every_billed_command_is_refused(command: str) -> None:
    with pytest.raises(AssertionError):
        subprocess.run([command, "--help"], capture_output=True)


def test_an_absolute_path_to_the_same_binary_is_refused_too() -> None:
    # resolve_executable returns an absolute path on Windows, and a guard that
    # only matched the bare name would wave that straight through.
    assert billed_runtime([r"C:\Program Files\claude\claude.exe", "-p"]) == "claude.exe"
    assert billed_runtime(["/opt/node/bin/claude", "-p"]) == "claude"


def test_an_ordinary_spawn_is_left_alone() -> None:
    # The suite starts sys.executable constantly. A guard that stopped that
    # would be turned off within a week.
    result = subprocess.run(
        [sys.executable, "-c", "print('fine')"], capture_output=True, text=True
    )

    assert result.stdout.strip() == "fine"


def test_an_unstubbed_completion_is_refused_rather_than_paid_for() -> None:
    # The exact shape of the leak: complete() with no runner falls through to
    # the real spawn. Before this guard that was a real, billed judging run.
    with pytest.raises(AssertionError, match="was about to start `claude`"):
        complete("this must never reach a runtime", model="sonnet", timeout=5.0)
