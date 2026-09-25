"""The runtime probe: what this machine actually has, measured not guessed.

Every test here drives the injected-runner seam. Nothing shells out — a binary
that may not exist in this sandbox is exactly what the probe is for, and a test
that needs one would be untrustworthy in the only environment that matters.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from venator.llm import probe
from venator.llm.lanes import CLAUDE_SIGN_IN_REMEDY, ApiKeyLane, ClaudeLane, CodexLane
from venator.llm.probe import main, probe_all, probe_lane
from venator.llm.runtime import Invocation, Result

PARENT = {
    "PATH": "/usr/bin",
    "HOME": "/home/owner",
    "ANTHROPIC_API_KEY": "planted-anthropic-key",
    "OPENAI_API_KEY": "planted-openai-key",
}

CONFIGURED_KEY_LANE = {
    **PARENT,
    "VENATOR_LLM_API_URL": "https://endpoint.example/v1/chat/completions",
    "VENATOR_LLM_API_MODEL": "a-model",
    "VENATOR_LLM_API_KEY_VAR": "MY_OWN_KEY",
    "MY_OWN_KEY": "the-key-the-owner-chose",
}

LANES = [ClaudeLane(), CodexLane(), ApiKeyLane()]


class Recorder:
    """The process boundary, replaced: records the invocation, then replies."""

    def __init__(self, result: Result | BaseException) -> None:
        self.result = result
        self.invocations: list[Invocation] = []

    def __call__(self, invocation: Invocation) -> Result:
        self.invocations.append(invocation)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


ANSWERED = {
    "claude": Result(returncode=0, stdout=json.dumps({"result": "ok"}), stderr=""),
    "codex": Result(returncode=0, stdout="ok", stderr=""),
}
SIGNED_OUT = Result(returncode=1, stdout="", stderr="Error: not logged in")
BROKEN = Result(returncode=1, stdout="", stderr="context window exceeded")


@pytest.mark.parametrize("lane", ["claude", "codex"])
def test_a_lane_that_answers_is_available(lane: str) -> None:
    runner = Recorder(ANSWERED[lane])

    status = probe_lane(lane, lanes=LANES, runner=runner, environ=PARENT)

    assert (status.state, status.ready) == ("available", True)
    assert len(runner.invocations) == 1, "available must mean it actually ran"


@pytest.mark.parametrize("lane", ["claude", "codex"])
def test_a_lane_whose_command_is_absent_says_install(lane: str) -> None:
    status = probe_lane(
        lane, lanes=LANES, runner=Recorder(FileNotFoundError(lane)), environ=PARENT
    )

    assert (status.state, status.ready) == ("not_installed", False)
    assert "install" in status.remedy


@pytest.mark.parametrize("lane", ["claude", "codex"])
def test_a_lane_that_is_installed_but_signed_out_says_sign_in(lane: str) -> None:
    status = probe_lane(lane, lanes=LANES, runner=Recorder(SIGNED_OUT), environ=PARENT)

    assert (status.state, status.ready) == ("not_signed_in", False)
    assert "sign in" in status.remedy


def test_the_two_unavailable_states_never_give_the_same_instruction() -> None:
    # Collapsing these is the failure that would hurt most: "install Claude
    # Code" and "run claude and sign in" are different afternoons for someone
    # who is not a programmer.
    absent = probe_lane(
        "claude", lanes=LANES, runner=Recorder(FileNotFoundError("claude")), environ=PARENT
    )
    signed_out = probe_lane(
        "claude", lanes=LANES, runner=Recorder(SIGNED_OUT), environ=PARENT
    )

    assert absent.state != signed_out.state
    assert absent.remedy != signed_out.remedy
    assert "install" not in signed_out.remedy


@pytest.mark.parametrize("lane", ["claude", "codex"])
def test_a_lane_that_never_answers_is_timed_out_not_missing(lane: str) -> None:
    runner = Recorder(subprocess.TimeoutExpired(cmd=[lane], timeout=120.0))

    status = probe_lane(lane, lanes=LANES, runner=runner, environ=PARENT)

    assert (status.state, status.ready) == ("timed_out", False)
    assert "by hand" in status.remedy


@pytest.mark.parametrize("lane", ["claude", "codex"])
def test_a_lane_that_errors_for_another_reason_is_its_own_state(lane: str) -> None:
    status = probe_lane(lane, lanes=LANES, runner=Recorder(BROKEN), environ=PARENT)

    assert (status.state, status.ready) == ("failed", False)
    assert "context window exceeded" not in status.detail
    assert "exited 1" in status.detail


def test_the_probe_remedy_is_the_same_string_the_error_raises() -> None:
    # One copy of the copy. If the lane's wording changes, the screen changes
    # with it, because they are the same object.
    from venator.llm.lanes import CLAUDE_INSTALL_REMEDY, CLAUDE_SIGN_IN_REMEDY

    absent = probe_lane(
        "claude", lanes=LANES, runner=Recorder(FileNotFoundError("claude")), environ=PARENT
    )
    signed_out = probe_lane(
        "claude", lanes=LANES, runner=Recorder(SIGNED_OUT), environ=PARENT
    )

    assert absent.remedy == CLAUDE_INSTALL_REMEDY
    assert signed_out.remedy == CLAUDE_SIGN_IN_REMEDY


def test_the_probe_invokes_the_ordinary_command_and_signs_nobody_in() -> None:
    runner = Recorder(ANSWERED["claude"])

    probe_lane("claude", lanes=LANES, runner=runner, environ=PARENT)

    argv = runner.invocations[0].argv
    assert argv == ("claude", "-p", "--output-format", "json")
    for forbidden in ("login", "setup-token", "logout", "--bare"):
        assert forbidden not in argv


def test_the_probe_child_cannot_see_a_planted_key() -> None:
    # A key in the shell must not make a lane look available when it is not the
    # lane the person would actually get.
    runner = Recorder(ANSWERED["claude"])

    probe_lane("claude", lanes=LANES, runner=runner, environ=PARENT)

    environment = runner.invocations[0].environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["HOME"] == "/home/owner"


def test_the_verified_codex_result_carries_no_caveat() -> None:
    status = probe_lane("codex", lanes=LANES, runner=Recorder(ANSWERED["codex"]), environ=PARENT)

    assert status.state == "available"
    assert status.caveat == ""


def test_the_probe_never_builds_codex_without_its_safety_flags() -> None:
    from venator.llm.lanes import CODEX_SAFETY_FLAGS

    runner = Recorder(ANSWERED["codex"])

    probe_lane("codex", lanes=LANES, runner=runner, environ=PARENT)

    argv = runner.invocations[0].argv
    for flag in CODEX_SAFETY_FLAGS:
        assert flag in argv
    assert "-p" not in argv


def test_the_claude_result_carries_no_caveat() -> None:
    status = probe_lane("claude", lanes=LANES, runner=Recorder(ANSWERED["claude"]), environ=PARENT)

    assert status.caveat == ""


def unreachable(invocation: Invocation) -> Result:
    raise AssertionError("probing must not start a process for the key lane")


def test_an_unconfigured_key_lane_says_what_to_set() -> None:
    status = probe_lane("api", lanes=LANES, runner=unreachable, environ=PARENT)

    assert (status.state, status.ready) == ("not_configured", False)
    assert "VENATOR_LLM_API_URL" in status.remedy


def test_a_configured_key_lane_is_never_reported_available() -> None:
    # It was not contacted, so it cannot be called available — and contacting it
    # would spend the person's money to answer a question they did not ask.
    status = probe_lane("api", lanes=LANES, runner=unreachable, environ=CONFIGURED_KEY_LANE)

    assert (status.state, status.ready) == ("configured", False)
    assert "not contacted" in status.detail


def test_the_default_lane_is_marked_and_the_others_are_not() -> None:
    statuses = probe_all(lanes=LANES, runner=Recorder(ANSWERED["claude"]), environ=PARENT)

    marked = [status.lane for status in statuses if status.default]
    assert marked == ["claude"]


def test_probing_everything_covers_every_lane() -> None:
    statuses = probe_all(lanes=LANES, runner=Recorder(ANSWERED["claude"]), environ=PARENT)

    assert [status.lane for status in statuses] == ["claude", "codex", "api"]


def test_an_unknown_lane_name_is_refused() -> None:
    with pytest.raises(ValueError, match="names no runtime; available: claude, codex, api"):
        probe_lane("something-else", lanes=LANES, runner=unreachable, environ=PARENT)


# --- the command-line surface the dashboard shells out to --------------------


@pytest.fixture
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "VENATOR_LLM_RUNTIME",
        "VENATOR_LLM_API_URL",
        "VENATOR_LLM_API_MODEL",
        "VENATOR_LLM_API_KEY_VAR",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)


def emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list)
    return parsed


def test_the_cli_probes_the_default_lane_and_emits_one_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], clean_environment: None
) -> None:
    monkeypatch.setattr(probe, "run_process", Recorder(ANSWERED["claude"]))

    assert main(["--json"]) == 0

    rows = emitted(capsys)
    assert len(rows) == 1
    assert rows[0]["lane"] == "claude"
    assert rows[0]["default"] is True


def test_the_cli_probes_one_named_lane(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], clean_environment: None
) -> None:
    monkeypatch.setattr(probe, "run_process", Recorder(ANSWERED["codex"]))

    assert main(["--lane", "codex", "--json"]) == 0

    rows = emitted(capsys)
    assert [row["lane"] for row in rows] == ["codex"]
    assert rows[0]["state"] == "available"


def test_probing_every_lane_is_opt_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], clean_environment: None
) -> None:
    monkeypatch.setattr(probe, "run_process", Recorder(ANSWERED["claude"]))

    assert main(["--all", "--json"]) == 0

    assert [row["lane"] for row in emitted(capsys)] == ["claude", "codex", "api"]


def test_the_json_row_carries_exactly_the_agreed_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], clean_environment: None
) -> None:
    monkeypatch.setattr(probe, "run_process", Recorder(SIGNED_OUT))

    assert main(["--all", "--json"]) == 0

    for row in emitted(capsys):
        assert set(row) == {
            "lane",
            "state",
            "ready",
            "detail",
            "remedy",
            "caveat",
            "default",
        }
        assert isinstance(row["lane"], str)
        assert isinstance(row["state"], str)
        assert isinstance(row["ready"], bool)
        assert isinstance(row["remedy"], str)


def test_an_unavailable_runtime_is_data_not_a_failed_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], clean_environment: None
) -> None:
    # The caller must be able to tell "you have no runtime" from "the probe
    # broke" by exit code alone, without parsing stderr.
    monkeypatch.setattr(probe, "run_process", Recorder(FileNotFoundError("claude")))

    assert main(["--all", "--json"]) == 0

    captured = capsys.readouterr()
    rows = json.loads(captured.out)
    assert all(row["ready"] is False for row in rows)
    assert rows[0]["state"] == "not_installed"
    assert captured.err == "", "unavailability must not need stderr to be understood"


def test_the_probe_itself_failing_is_the_only_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], clean_environment: None
) -> None:
    monkeypatch.setattr(probe, "run_process", Recorder(ANSWERED["claude"]))

    assert main(["--lane", "something-else", "--json"]) == 2
    assert "names no runtime" in capsys.readouterr().err


def test_asking_for_one_lane_and_all_lanes_at_once_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], clean_environment: None
) -> None:
    monkeypatch.setattr(probe, "run_process", Recorder(ANSWERED["claude"]))

    assert main(["--lane", "codex", "--all"]) == 2
    assert "pick one" in capsys.readouterr().err


def test_the_human_form_names_the_state_and_the_fix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], clean_environment: None
) -> None:
    monkeypatch.setattr(probe, "run_process", Recorder(SIGNED_OUT))

    assert main([]) == 0

    out = capsys.readouterr().out
    assert "claude: not_signed_in" in out
    assert "(default)" in out
    assert f"fix: {CLAUDE_SIGN_IN_REMEDY}" in out


def test_the_runtime_variable_chooses_what_the_bare_cli_probes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], clean_environment: None
) -> None:
    # Someone who has already pinned a runtime should be told about that one.
    monkeypatch.setenv("VENATOR_LLM_RUNTIME", "codex")
    monkeypatch.setattr(probe, "run_process", Recorder(ANSWERED["codex"]))

    assert main(["--json"]) == 0

    assert [row["lane"] for row in emitted(capsys)] == ["codex"]
