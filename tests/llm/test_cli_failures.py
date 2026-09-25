from __future__ import annotations

import json
import subprocess
import traceback

import pytest

from venator.llm.lanes import ClaudeLane, CodexLane
from venator.llm.runtime import Invocation, LaneFailed, LaneUnavailable, Request, Result


def test_codex_argv_keeps_the_safety_flags_and_uses_the_named_model() -> None:
    lane = CodexLane()
    environ = {"VENATOR_LLM_CODEX_MODEL": "chosen-model"}
    expected = (
        "codex", "-s", "read-only", "-a", "never", "exec", "--json",
        "--color", "never", "--skip-git-repo-check", "--ephemeral",
        "--model", "chosen-model", "-",
    )
    assert lane.argv(environ) == expected


@pytest.mark.parametrize("lane", [ClaudeLane(), CodexLane()])
@pytest.mark.parametrize("failure", ["exit", "signed_out", "spawn", "io", "timeout", "shape", "failed_reply"])
def test_cli_failures_never_quote_provider_text(lane: ClaudeLane | CodexLane, failure: str) -> None:
    sentinel = "PRIVATE-PROVIDER-SENTINEL"

    def runner(invocation: Invocation) -> Result:
        if failure == "spawn":
            raise FileNotFoundError(sentinel)
        if failure == "io":
            raise OSError(sentinel)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(sentinel, 5, output=sentinel, stderr=sentinel)
        if failure == "exit":
            return Result(1, sentinel, sentinel)
        if failure == "signed_out":
            return Result(1, sentinel, "not logged in " + sentinel)
        if failure == "shape":
            return Result(0, json.dumps({"unexpected": sentinel}), sentinel)
        if lane.name == "claude":
            return Result(0, json.dumps({"is_error": True, "result": sentinel}), "")
        return Result(0, "\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": sentinel}}),
            json.dumps({"type": "turn.failed", "error": sentinel}),
        ]), "")

    with pytest.raises((LaneFailed, LaneUnavailable, subprocess.TimeoutExpired)) as error:
        lane.complete(Request("fixed prompt"), runner, {})
    assert sentinel not in str(error.value)
    assert sentinel not in "".join(traceback.format_exception(error.value))


def test_malformed_json_does_not_expose_decoder_context() -> None:
    sentinel = "PRIVATE-SENTINEL"
    with pytest.raises(LaneFailed) as error:
        ClaudeLane().complete(Request("hi"), lambda _: Result(0, sentinel, ""), {})
    assert sentinel not in "".join(traceback.format_exception(error.value))
