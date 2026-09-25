from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from venator.llm.lanes import (
    CODEX_SAFETY_FLAGS,
    DEFAULT_RUNTIME,
    JSON_SCHEMA_FLAG,
    LANES,
    SCHEMA_RETRIES_SUBTYPE,
    ApiKeyLane,
    ClaudeLane,
    CodexLane,
)
from venator.llm.runtime import (
    Invocation,
    LaneFailed,
    LaneUnavailable,
    Request,
    Result,
)

REQUEST = Request(prompt="score these postings", model="sonnet", timeout=12.0)
CODEX_SUCCESS_FIXTURE = (
    Path(__file__).with_name("fixtures") / "codex-cli-0.151.0-success.jsonl"
)

PARENT = {
    "PATH": "/usr/bin",
    "HOME": "/home/owner",
    "ANTHROPIC_API_KEY": "planted-anthropic-key",
    "OPENAI_API_KEY": "planted-openai-key",
    "ANTHROPIC_BASE_URL": "https://someone-elses-gateway.example",
}


class Recorder:
    """Stands in for the process boundary: records the invocation, replies."""

    def __init__(self, result: Result | BaseException) -> None:
        self.result = result
        self.invocations: list[Invocation] = []

    def __call__(self, invocation: Invocation) -> Result:
        self.invocations.append(invocation)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    @property
    def only(self) -> Invocation:
        assert len(self.invocations) == 1
        return self.invocations[0]


def envelope(text: str) -> Result:
    return Result(returncode=0, stdout=json.dumps({"result": text}), stderr="")


def test_the_claude_lane_spawns_exactly_what_the_judge_always_spawned() -> None:
    runner = Recorder(envelope("[]"))

    ClaudeLane().complete(REQUEST, runner, PARENT)

    assert runner.only.argv == (
        "claude",
        "-p",
        "--model",
        "sonnet",
        "--output-format",
        "json",
    )
    assert runner.only.stdin == "score these postings"
    assert runner.only.timeout == 12.0


def test_no_lane_ever_passes_bare() -> None:
    # --bare does not read the subscription login at all, so it must never
    # appear in an argv this adapter builds.
    claude = ClaudeLane().argv(REQUEST)
    codex = CodexLane().argv({"VENATOR_LLM_CODEX_MODEL": "some-model"})

    assert "--bare" not in claude
    assert "--bare" not in codex


def test_the_codex_lane_never_passes_dash_p() -> None:
    # On codex, -p is --profile, not "print". A run with `-p sonnet` was
    # accepted against codex-cli 0.149.1, did not complain about the
    # nonexistent profile, and quietly used the default model — wrong silently
    # rather than loudly. It also collides with venator's Profile vocabulary.
    argv = CodexLane().argv({"VENATOR_LLM_CODEX_MODEL": "some-model"})

    assert "-p" not in argv
    assert "--model" in argv


def test_the_codex_lane_never_passes_output_format() -> None:
    # --output-format does not exist on codex; it exits 2 and suggests
    # --output-schema. Only the Claude lane uses that flag.
    assert "--output-format" not in CodexLane().argv({})


def test_the_claude_lane_child_cannot_see_a_planted_key() -> None:
    runner = Recorder(envelope("[]"))

    ClaudeLane().complete(REQUEST, runner, PARENT)

    environment = runner.only.environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_BASE_URL" not in environment
    assert environment["HOME"] == "/home/owner"


def test_the_codex_lane_child_cannot_see_a_planted_key() -> None:
    runner = Recorder(Result(returncode=0, stdout="an answer", stderr=""))

    CodexLane().complete(REQUEST, runner, PARENT)

    assert "OPENAI_API_KEY" not in runner.only.environment
    assert "ANTHROPIC_API_KEY" not in runner.only.environment


def test_the_claude_lane_unwraps_the_json_envelope() -> None:
    runner = Recorder(envelope('[{"posting_key": "a", "score": 80}]'))

    answer = ClaudeLane().complete(REQUEST, runner, PARENT)

    assert answer.text == '[{"posting_key": "a", "score": 80}]'
    # Nothing was asked for, so nothing is claimed: no note, not enforced.
    assert (answer.schema_enforced, answer.schema_note) == (False, "")


def test_a_missing_command_makes_the_lane_unavailable_not_failed() -> None:
    runner = Recorder(FileNotFoundError("claude"))

    with pytest.raises(LaneUnavailable) as unavailable:
        ClaudeLane().complete(REQUEST, runner, PARENT)

    assert unavailable.value.lane == "claude"
    assert "could not be run" in unavailable.value.reason
    assert "sign in" in unavailable.value.remedy


def test_a_runtime_that_says_it_has_no_login_makes_the_lane_unavailable() -> None:
    # Behavioural detection: the lane is run, and its own words are read. No
    # credential store is opened to decide this.
    runner = Recorder(Result(returncode=1, stdout="", stderr="Error: not logged in"))

    with pytest.raises(LaneUnavailable) as unavailable:
        ClaudeLane().complete(REQUEST, runner, PARENT)

    assert "installed but reported no usable login" in unavailable.value.reason
    assert "/login" in unavailable.value.remedy


def test_not_installed_and_not_signed_in_carry_different_remedies() -> None:
    # These are different errands for the person reading the message, which is
    # the only reason the two failure kinds still exist now that neither one
    # reaches for another account.
    absent = Recorder(FileNotFoundError("claude"))
    signed_out = Recorder(Result(returncode=1, stdout="", stderr="Please log in"))

    with pytest.raises(LaneUnavailable) as missing:
        ClaudeLane().complete(REQUEST, absent, PARENT)
    with pytest.raises(LaneUnavailable) as unauthenticated:
        ClaudeLane().complete(REQUEST, signed_out, PARENT)

    assert "install Claude Code" in missing.value.remedy
    assert "install" not in unauthenticated.value.remedy
    assert "sign in" in unauthenticated.value.remedy


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: 401 unauthorized",
        "authentication_error while calling a tool",
        "invalid api key",
        "your credit balance is too low",
        "sh: codex: command not found",
    ],
)
def test_an_ambiguous_phrase_is_a_failure_not_a_confident_wrong_remedy(
    stderr: str,
) -> None:
    # These used to be read as "not signed in", which under the old ladder
    # switched accounts. They are gone from SETUP_MARKERS: each has a different
    # fix, so a constructed failure preserves the failure classification.
    runner = Recorder(Result(returncode=1, stdout="", stderr=stderr))

    with pytest.raises(LaneFailed) as failed:
        ClaudeLane().complete(REQUEST, runner, PARENT)

    assert stderr not in str(failed.value)
    assert "exited 1" in str(failed.value)


def test_a_runtime_that_ran_and_broke_is_a_failure_not_an_absence() -> None:
    runner = Recorder(Result(returncode=1, stdout="", stderr="prompt too long"))

    with pytest.raises(LaneFailed, match="exited 1"):
        ClaudeLane().complete(REQUEST, runner, PARENT)


def test_output_that_is_not_the_expected_envelope_fails_the_lane() -> None:
    runner = Recorder(Result(returncode=0, stdout="not json at all", stderr=""))

    with pytest.raises(LaneFailed, match="not JSON"):
        ClaudeLane().complete(REQUEST, runner, PARENT)


def test_the_codex_lane_takes_the_last_assistant_message() -> None:
    stream = "\n".join(
        [
            json.dumps({"type": "thread.started"}),
            json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}
            ),
            json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "last"}}
            ),
        ]
    )
    runner = Recorder(Result(returncode=0, stdout=stream, stderr=""))

    assert CodexLane().complete(REQUEST, runner, PARENT).text == "last"


def test_the_codex_lane_parses_the_recorded_success_fixture() -> None:
    runner = Recorder(
        Result(
            returncode=0,
            stdout=CODEX_SUCCESS_FIXTURE.read_text(encoding="utf-8"),
            stderr="",
        )
    )

    answer = CodexLane().complete(REQUEST, runner, PARENT)

    assert answer.text == "CODEX_LANE_OK"


def test_the_codex_lane_falls_back_to_plain_output() -> None:
    runner = Recorder(Result(returncode=0, stdout="  a plain answer  ", stderr=""))

    assert CodexLane().complete(REQUEST, runner, PARENT).text == "a plain answer"


def test_the_codex_lane_ignores_the_callers_model_hint() -> None:
    # A model name meaningful to one runtime means nothing to another.
    runner = Recorder(Result(returncode=0, stdout="answer", stderr=""))

    CodexLane().complete(REQUEST, runner, PARENT)

    assert "sonnet" not in runner.only.argv
    assert "--model" not in runner.only.argv


def test_the_codex_lane_uses_its_own_model_variable_when_set() -> None:
    argv = CodexLane().argv({**PARENT, "VENATOR_LLM_CODEX_MODEL": "some-model"})

    assert argv[-3:] == ("--model", "some-model", "-")
    assert argv[:7] == (
        "codex",
        "-s",
        "read-only",
        "-a",
        "never",
        "exec",
        "--json",
    )


def test_the_codex_invocation_is_pinned_read_only_and_never_asks() -> None:
    """The one that must fail hard.

    Codex is an agent with file-writing tools, and the directory it would run
    in holds `data/` — append-only, and the transport for the whole corpus. A
    lane in this codebase must not be able to write files. These two flags are
    not configurable and have no debugging escape hatch; if this test ever needs
    changing, that is the owner's call and not a refactor's.
    """
    for environ in ({}, {"VENATOR_LLM_CODEX_MODEL": "some-model"}):
        argv = CodexLane().argv(environ)
        joined = " ".join(argv)

        assert "-s read-only" in joined, "the Codex sandbox must be read-only"
        assert "-a never" in joined, "the Codex lane must never ask to escalate"
        for flag in CODEX_SAFETY_FLAGS:
            assert flag in argv


def test_the_codex_safety_flags_are_global_options_before_exec() -> None:
    # codex-cli 0.150.1 and 0.151.0 reject `-a` after `exec` with exit 2.
    # Keep the safety pair intact and put it where both commands parse it.
    argv = CodexLane().argv({})

    assert argv[:6] == ("codex", *CODEX_SAFETY_FLAGS, "exec")
    assert "-a" not in argv[argv.index("exec") + 1 :]


def test_nothing_spawns_codex_except_the_one_construction_site() -> None:
    # If a second path ever builds a Codex invocation, the safety flags can be
    # dropped from it silently. So what `complete` spawns must be, exactly and
    # only, what `argv` returned.
    environ = {**PARENT, "VENATOR_LLM_CODEX_MODEL": "some-model"}
    runner = Recorder(Result(returncode=0, stdout="an answer", stderr=""))

    CodexLane().complete(REQUEST, runner, environ)

    assert runner.only.argv == CodexLane().argv(environ)


def test_the_codex_invocation_keeps_its_hygiene_flags() -> None:
    # Load-bearing, each for a stated reason: escapes would land in a committed
    # file, an untrusted directory would refuse to run, and session state from a
    # judging run would otherwise pile up in the person's own Codex history.
    argv = CodexLane().argv({})

    assert ("--color", "never") == argv[argv.index("--color") : argv.index("--color") + 2]
    assert "--skip-git-repo-check" in argv
    assert "--ephemeral" in argv


def test_the_codex_lane_reads_jsonl_and_never_a_result_envelope() -> None:
    # codex --json emits one event per line with no envelope. A result-shaped
    # reply is not a Codex reply, and must not be read as one.
    envelope_shaped = json.dumps({"result": "an answer that is not codex's"})
    runner = Recorder(Result(returncode=0, stdout=envelope_shaped, stderr=""))

    with pytest.raises(LaneFailed, match="no assistant message"):
        CodexLane().complete(REQUEST, runner, PARENT)


def test_an_event_stream_with_no_answer_is_a_failure_not_a_wall_of_events() -> None:
    stream = "\n".join(
        [
            json.dumps({"type": "thread.started"}),
            json.dumps({"type": "turn.failed", "error": {"message": "no"}}),
        ]
    )
    runner = Recorder(Result(returncode=0, stdout=stream, stderr=""))

    with pytest.raises(LaneFailed) as failed:
        CodexLane().complete(REQUEST, runner, PARENT)

    assert "unsuccessful completion" in str(failed.value)


def test_the_verified_signed_out_message_is_read_as_a_login_problem() -> None:
    # Observed against codex-cli 0.149.1 with no credentials: every run ends
    # here, exit 1, plain text on stderr.
    runner = Recorder(
        Result(
            returncode=1,
            stdout="",
            stderr="401 Unauthorized: Missing bearer or basic authentication in header",
        )
    )

    with pytest.raises(LaneUnavailable) as unavailable:
        CodexLane().complete(REQUEST, runner, PARENT)

    assert unavailable.value.kind == "not_signed_in"
    assert "sign in to the Codex CLI" in unavailable.value.remedy


def test_a_bare_401_is_not_confidently_called_a_login_problem() -> None:
    # codex exits 1 for auth, an untrusted directory and a failed turn alike, so
    # only the specific observed phrase is trusted. A proxy's 401 is not it.
    runner = Recorder(
        Result(returncode=1, stdout="", stderr="proxy said 401 unauthorized")
    )

    with pytest.raises(LaneFailed):
        CodexLane().complete(REQUEST, runner, PARENT)


def test_escape_sequences_never_reach_a_recorded_failure() -> None:
    # The failure is shown on screen and written to logs. Raw ANSI there
    # is a mess.
    runner = Recorder(
        Result(returncode=1, stdout="", stderr="\x1b[31mturn failed\x1b[0m")
    )

    with pytest.raises(LaneFailed) as failed:
        CodexLane().complete(REQUEST, runner, PARENT)

    assert "\x1b" not in str(failed.value)
    assert "turn failed" not in str(failed.value)
    assert "exited 1" in str(failed.value)


class FakePoster:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        self.calls: list[tuple[str, Mapping[str, str], dict[str, Any], float]] = []

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> tuple[int, str]:
        self.calls.append((url, headers, payload, timeout))
        return self.status, self.body


def unusable(invocation: Invocation) -> Result:
    raise AssertionError("the key lane must not start a child process")


CHAT_BODY = json.dumps({"choices": [{"message": {"content": "an answer"}}]})

CONFIGURED = {
    **PARENT,
    "VENATOR_LLM_API_URL": "https://endpoint.example/v1/chat/completions",
    "VENATOR_LLM_API_MODEL": "a-model",
    "VENATOR_LLM_API_KEY_VAR": "MY_OWN_KEY",
    "MY_OWN_KEY": "the-key-the-owner-chose",
}


def test_a_planted_vendor_key_does_not_switch_the_key_lane_on() -> None:
    # PARENT has both vendor keys in it. Without the explicit opt-in the lane
    # declines: a key lying around is never taken as permission to bill it.
    with pytest.raises(LaneUnavailable) as unavailable:
        ApiKeyLane(poster=FakePoster(200, CHAT_BODY)).complete(REQUEST, unusable, PARENT)

    assert "not set up" in unavailable.value.reason
    assert "VENATOR_LLM_API_URL" in unavailable.value.reason
    assert "VENATOR_LLM_API_KEY_VAR" in unavailable.value.reason


def test_the_key_lane_sends_only_the_key_it_was_pointed_at() -> None:
    poster = FakePoster(200, CHAT_BODY)

    text = ApiKeyLane(poster=poster).complete(REQUEST, unusable, CONFIGURED).text

    url, headers, payload, timeout = poster.calls[0]
    assert text == "an answer"
    assert url == "https://endpoint.example/v1/chat/completions"
    assert headers["Authorization"] == "Bearer the-key-the-owner-chose"
    assert "planted-anthropic-key" not in json.dumps(dict(headers))
    assert "planted-openai-key" not in json.dumps(dict(headers))
    assert payload == {
        "model": "a-model",
        "messages": [{"role": "user", "content": "score these postings"}],
    }
    assert timeout == 12.0


def test_the_key_lane_declines_when_the_named_variable_is_empty() -> None:
    environ = {**CONFIGURED, "MY_OWN_KEY": ""}

    with pytest.raises(LaneUnavailable, match="The variable it names is empty"):
        ApiKeyLane(poster=FakePoster(200, CHAT_BODY)).complete(REQUEST, unusable, environ)


def test_the_key_lane_reports_a_refused_endpoint_as_a_failure() -> None:
    poster = FakePoster(401, '{"error": "no"}')

    with pytest.raises(LaneFailed) as failed:
        ApiKeyLane(poster=poster).complete(REQUEST, unusable, CONFIGURED)

    message = str(failed.value)
    assert "answered HTTP 401" in message
    assert "15 bytes" in message
    # Constructed, not quoted: the endpoint's own words are absent entirely.
    assert '{"error": "no"}' not in message
    assert "is not shown here" in message


def test_a_request_that_never_reached_the_endpoint_names_only_the_class() -> None:
    # An httpx error quotes the URL it was handed, and a URL is somewhere a
    # person can put a key. Only the exception class name survives.
    class Exploding:
        def __call__(
            self,
            url: str,
            headers: Mapping[str, str],
            payload: dict[str, Any],
            timeout: float,
        ) -> tuple[int, str]:
            raise ConnectionError(
                "failed connecting to https://endpoint.example/v1?key=the-key-the-owner-chose"
            )

    with pytest.raises(LaneFailed) as failed:
        ApiKeyLane(poster=Exploding()).complete(REQUEST, unusable, CONFIGURED)

    message = str(failed.value)
    assert "ConnectionError" in message
    assert "failed before any answer came back" in message
    assert "the-key-the-owner-chose" not in message
    assert "endpoint.example" not in message


PASTED_KEYS = [
    # Dashes, so it is not a variable name and takes the guarded branch.
    pytest.param("sk-ant-api03-the-key-itself", id="dashed"),
    # These are all valid POSIX identifiers, so `VARIABLE_NAME` matches them and
    # the guarded branch never fires. They reach the empty-value branch instead,
    # which is where this leaked: that branch named the variable it was handed,
    # and the variable it was handed was the key.
    pytest.param("gsk_venator0123456789abcdefghijklmn", id="groq"),
    pytest.param("hf_venator0123456789abcdefghijklmno", id="hugging-face"),
    pytest.param("ghp_venator0123456789abcdefghijklmn", id="github-classic"),
    pytest.param("github_pat_venator0123456789abcdefg", id="github-fine-grained"),
    pytest.param("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", id="hex-leading-letter"),
]


@pytest.mark.parametrize("pasted", PASTED_KEYS)
def test_a_key_pasted_into_the_name_variable_is_refused_without_echoing_it(
    pasted: str,
) -> None:
    # The likeliest way to get this wrong is to put the key where its name goes.
    # Saying so must not print the key into a message shown on screen and
    # written to logs.
    environ = {**PARENT, "VENATOR_LLM_API_URL": "https://endpoint.example/v1",
               "VENATOR_LLM_API_MODEL": "a-model",
               "VENATOR_LLM_API_KEY_VAR": pasted}

    with pytest.raises(LaneUnavailable) as unavailable:
        ApiKeyLane(poster=FakePoster(200, CHAT_BODY)).complete(REQUEST, unusable, environ)

    assert "the *name* of the variable" in unavailable.value.reason
    assert pasted not in unavailable.value.reason
    assert pasted not in unavailable.value.remedy
    assert pasted not in str(unavailable.value)


@pytest.mark.parametrize("pasted", PASTED_KEYS)
def test_a_pasted_key_never_reaches_the_probe_json(pasted: str) -> None:
    # The probe's detail and remedy are a published contract that a dashboard
    # renders (coordination/CONTRACTS.md), which makes them a third surface for
    # the same value, not just a second copy of the error.
    environ = {**PARENT, "VENATOR_LLM_API_URL": "https://endpoint.example/v1",
               "VENATOR_LLM_API_MODEL": "a-model",
               "VENATOR_LLM_API_KEY_VAR": pasted}

    status = ApiKeyLane(poster=FakePoster(200, CHAT_BODY)).probe(unusable, environ)

    assert status.state == "not_configured"
    assert pasted not in json.dumps(status.as_dict())


def test_the_default_runtime_is_the_subscription_lane() -> None:
    # LANES is a registry of what a person may name, not a ladder. What matters
    # is which one runs when nobody names anything.
    assert DEFAULT_RUNTIME == "claude"
    assert sorted(lane.name for lane in LANES) == ["api", "claude", "codex"]


def test_the_key_never_rides_along_in_an_error_message() -> None:
    # A failure message travels on to the person and to logs, so a body that
    # echoes the key back must not carry it there.
    echoing = FakePoster(401, '{"error": "bad key the-key-the-owner-chose"}')

    with pytest.raises(LaneFailed) as failed:
        ApiKeyLane(poster=echoing).complete(REQUEST, unusable, CONFIGURED)

    assert "the-key-the-owner-chose" not in str(failed.value)


def test_the_key_never_rides_along_in_an_unreadable_answer() -> None:
    echoing = FakePoster(200, "not json, and it says the-key-the-owner-chose")

    with pytest.raises(LaneFailed) as failed:
        ApiKeyLane(poster=echoing).complete(REQUEST, unusable, CONFIGURED)

    assert "the-key-the-owner-chose" not in str(failed.value)


def test_an_answer_that_echoes_the_key_is_redacted_before_it_is_returned() -> None:
    # The answer is the one piece of provider text that is carried through: it
    # becomes a drafted document or a parsed resume written to disk. The
    # constructed failure messages cannot leak; this path can, so it is scrubbed.
    echoing = FakePoster(
        200,
        json.dumps(
            {"choices": [{"message": {"content": "your key the-key-the-owner-chose is bad"}}]}
        ),
    )

    text = ApiKeyLane(poster=echoing).complete(REQUEST, unusable, CONFIGURED).text

    assert "the-key-the-owner-chose" not in text
    assert "<redacted>" in text


def test_the_default_lane_strips_ansi_before_anything_is_recorded() -> None:
    # _plain was applied on the Codex lane and not on this one — which is the
    # lane that actually runs. A failure here is shown on screen and
    # written to logs, and a raw escape there is a mess.
    coloured = "\x1b[31mError: the model is unhappy\x1b[0m"
    runner = Recorder(Result(returncode=2, stdout="", stderr=coloured))

    with pytest.raises(LaneFailed) as failed:
        ClaudeLane().complete(REQUEST, runner, PARENT)

    assert "\x1b" not in str(failed.value)
    assert "Error: the model is unhappy" not in str(failed.value)
    assert "exited 2" in str(failed.value)


def test_the_default_lane_strips_ansi_out_of_a_sign_in_message_too() -> None:
    runner = Recorder(
        Result(returncode=1, stdout="", stderr="\x1b[33mError: not logged in\x1b[0m")
    )

    with pytest.raises(LaneUnavailable) as unavailable:
        ClaudeLane().complete(REQUEST, runner, PARENT)

    assert "\x1b" not in unavailable.value.reason
    assert "no usable login" in unavailable.value.reason


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("\x1b[31mred\x1b[0m", "red", id="csi"),
        pytest.param("\x1b]0;title\x07after", "after", id="osc-bel-terminated"),
        pytest.param("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\after", "linkafter",
                     id="osc-st-terminated"),
        # The two the old pattern could not finish. Its OSC branch stopped its
        # payload at any ESC, so a payload containing one made the whole branch
        # fail and left the raw `ESC \\` terminator in the text; and a sequence
        # cut off mid-write — which is what a killed process leaves on stderr —
        # had no terminator to find at all.
        pytest.param("\x1b]0;a\x1bb\x1b\\after", "after", id="osc-with-inner-escape"),
        pytest.param("before\x1b]0;cut-off-here", "before", id="osc-truncated"),
        pytest.param("text\x1b\\more", "textmore", id="lone-string-terminator"),
    ],
)
def test_ansi_stripping_leaves_no_escape_behind(raw: str, expected: str) -> None:
    from venator.llm.lanes import _plain

    plain = _plain(raw)

    assert plain == expected
    assert "\x1b" not in plain


# ---------------------------------------------------------------------------
# Asking the runtime to enforce the reply's shape.
#
# The property under test throughout this section is that the guarantee is
# never *claimed* more strongly than it was obtained: a reply the runtime did
# not validate is either refused or reported as unenforced, and never both
# accepted and described as constrained.
# ---------------------------------------------------------------------------

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {"type": "string"}}},
    "required": ["scores"],
    "additionalProperties": False,
}

SCHEMA_REQUEST = Request(prompt="score these", model="sonnet", timeout=12.0, schema=SCHEMA)

# What the installed command's own --help says when it has the option, and
# when it is old enough not to.
HELP_WITH_FLAG = f"Options:\n  {JSON_SCHEMA_FLAG} <schema>   JSON Schema for output\n"
HELP_WITHOUT_FLAG = "Options:\n  --output-format <format>   Output format\n"


class TwoStep:
    """A process boundary that answers `--help` and the completion differently.

    The real one does too, and a stub that answered both alike would make the
    flag-detection question untestable — which is the question that decides
    whether this package tells the truth about enforcement.
    """

    def __init__(self, help_text: str, result: Result) -> None:
        self.help_text = help_text
        self.result = result
        self.invocations: list[Invocation] = []

    def __call__(self, invocation: Invocation) -> Result:
        self.invocations.append(invocation)
        if "--help" in invocation.argv:
            return Result(returncode=0, stdout=self.help_text, stderr="")
        return self.result

    @property
    def completion(self) -> Invocation:
        spawned = [i for i in self.invocations if "--help" not in i.argv]
        assert len(spawned) == 1
        return spawned[0]


def structured(payload: object, **envelope: object) -> Result:
    """An `--output-format json` envelope carrying a validated object."""
    body: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ignore me",
        "structured_output": payload,
    }
    body.update(envelope)
    return Result(returncode=0, stdout=json.dumps(body), stderr="")


def test_the_schema_is_on_the_argv_when_the_command_takes_one() -> None:
    runner = TwoStep(HELP_WITH_FLAG, structured({"scores": ["a"]}))

    answer = ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)

    argv = runner.completion.argv
    assert argv[: len(argv) - 2] == ("claude", "-p", "--model", "sonnet", "--output-format", "json")
    assert argv[-2] == JSON_SCHEMA_FLAG
    # Compact and key-ordered, so the same schema always makes the same argv.
    assert json.loads(argv[-1]) == SCHEMA
    assert " " not in argv[-1]
    assert answer.schema_enforced is True


def test_no_schema_asked_for_means_no_flag_and_no_extra_spawn() -> None:
    # A completion that asks for no shape must spawn exactly what it always
    # spawned, and must not pay for a --help question nobody needed.
    runner = Recorder(envelope("[]"))

    answer = ClaudeLane().complete(REQUEST, runner, PARENT)

    assert runner.only.argv == ("claude", "-p", "--model", "sonnet", "--output-format", "json")
    assert (answer.schema_enforced, answer.schema_note) == (False, "")


def test_the_validated_object_is_read_and_not_the_text_beside_it() -> None:
    runner = TwoStep(
        HELP_WITH_FLAG,
        structured({"scores": ["kept"]}, result='{"scores": ["the unvalidated text"]}'),
    )

    answer = ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)

    assert json.loads(answer.text) == {"scores": ["kept"]}
    assert "unvalidated" not in answer.text
    assert answer.schema_enforced is True


def test_success_with_no_structured_output_is_a_failure_not_the_text() -> None:
    # The documented case, and the one that would quietly undo the whole
    # guarantee: subtype says success, nothing was validated, and a plausible
    # reply is sitting on `result` waiting to be mistaken for one.
    runner = TwoStep(
        HELP_WITH_FLAG,
        Result(
            returncode=0,
            stdout=json.dumps(
                {"subtype": "success", "is_error": False, "result": '{"scores": []}'}
            ),
            stderr="",
        ),
    )

    with pytest.raises(LaneFailed) as failed:
        ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)

    assert "reported success without returning an object validated" in str(failed.value)
    assert "not a substitute" in str(failed.value)


def test_a_null_structured_output_is_the_same_failure() -> None:
    runner = TwoStep(HELP_WITH_FLAG, structured(None))

    with pytest.raises(LaneFailed, match="without returning an object validated"):
        ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)


def test_exhausted_schema_retries_is_a_failure_of_its_own() -> None:
    runner = TwoStep(
        HELP_WITH_FLAG,
        Result(
            returncode=0,
            stdout=json.dumps(
                {
                    "subtype": SCHEMA_RETRIES_SUBTYPE,
                    "is_error": True,
                    "result": '{"scores": []}',
                }
            ),
            stderr="",
        ),
    )

    with pytest.raises(LaneFailed) as failed:
        ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)

    assert "ran out of attempts" in str(failed.value)


def test_exhausted_schema_retries_is_named_even_when_the_command_exits_nonzero() -> None:
    # Observed shape: exit 1, nothing on stderr. Without this branch the person
    # reads "claude -p exited 1: " and learns nothing at all.
    runner = TwoStep(
        HELP_WITH_FLAG,
        Result(
            returncode=1,
            stdout=json.dumps({"subtype": SCHEMA_RETRIES_SUBTYPE, "is_error": True}),
            stderr="",
        ),
    )

    with pytest.raises(LaneFailed) as failed:
        ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)

    assert "ran out of attempts" in str(failed.value)


def test_a_real_crash_has_a_constructed_message() -> None:
    # The envelope branch above must not swallow an ordinary failure: when the
    # reply is not that envelope at all, a constructed exit status is retained.
    runner = TwoStep(
        HELP_WITH_FLAG, Result(returncode=1, stdout="", stderr="prompt too long")
    )

    with pytest.raises(LaneFailed, match="exited 1"):
        ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)


def test_no_envelope_at_all_under_a_schema_is_a_failure() -> None:
    runner = TwoStep(HELP_WITH_FLAG, Result(returncode=0, stdout="not json", stderr=""))

    with pytest.raises(LaneFailed, match="did not return the JSON envelope"):
        ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)


def test_a_failure_under_a_schema_quotes_nothing_the_command_said() -> None:
    # Same rule the key lane lives by: the failure is shown on screen
    # and written to logs.
    secret = "the-owners-private-string"
    runner = TwoStep(
        HELP_WITH_FLAG,
        Result(
            returncode=0,
            stdout=json.dumps({"subtype": "success", "result": secret}),
            stderr=secret,
        ),
    )

    with pytest.raises(LaneFailed) as failed:
        ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)

    assert secret not in str(failed.value)


def test_a_command_without_the_option_reports_rather_than_pretends() -> None:
    # The case that will actually happen: an older `claude`. The flag is not
    # passed, the run continues, and the answer says in so many words that the
    # shape was not enforced — never silence, which reads as enforcement.
    runner = TwoStep(HELP_WITHOUT_FLAG, envelope('{"scores": []}'))

    answer = ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)

    assert JSON_SCHEMA_FLAG not in runner.completion.argv
    assert answer.schema_enforced is False
    assert "NOT enforced at the call" in answer.schema_note
    assert JSON_SCHEMA_FLAG in answer.schema_note
    assert answer.text == '{"scores": []}'


def test_a_help_question_that_cannot_be_answered_reads_as_no_option() -> None:
    # `--help` failing proves nothing except that the flag cannot be justified.
    # The completion that follows hits the same problem and diagnoses it.
    class HelpBroken:
        def __init__(self) -> None:
            self.invocations: list[Invocation] = []

        def __call__(self, invocation: Invocation) -> Result:
            self.invocations.append(invocation)
            if "--help" in invocation.argv:
                raise OSError("no")
            return envelope('{"scores": []}')

    runner = HelpBroken()

    answer = ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)

    assert JSON_SCHEMA_FLAG not in runner.invocations[-1].argv
    assert answer.schema_enforced is False
    assert "NOT enforced at the call" in answer.schema_note


def test_the_help_question_costs_no_completion() -> None:
    # It is spawned with no prompt on stdin. A --help that were handed the
    # prompt would be a second bill nobody asked for.
    runner = TwoStep(HELP_WITH_FLAG, structured({"scores": []}))

    ClaudeLane().complete(SCHEMA_REQUEST, runner, PARENT)

    help_calls = [i for i in runner.invocations if "--help" in i.argv]
    assert [i.argv for i in help_calls] == [("claude", "--help")]
    assert [i.stdin for i in help_calls] == [""]


def test_a_lane_that_is_not_given_the_schema_says_so() -> None:
    stream = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "x"}})
    codex = CodexLane().complete(
        SCHEMA_REQUEST, Recorder(Result(returncode=0, stdout=stream, stderr="")), PARENT
    )
    key = ApiKeyLane(poster=FakePoster(200, CHAT_BODY)).complete(
        SCHEMA_REQUEST, unusable, CONFIGURED
    )

    for answer, lane in ((codex, "codex"), (key, "api")):
        assert answer.schema_enforced is False
        assert f"the {lane} runtime is not given the schema" in answer.schema_note
        assert "NOT enforced at the call" in answer.schema_note


def test_a_lane_not_asked_for_a_shape_claims_nothing_either_way() -> None:
    stream = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "x"}})

    answer = CodexLane().complete(
        REQUEST, Recorder(Result(returncode=0, stdout=stream, stderr="")), PARENT
    )

    assert (answer.schema_enforced, answer.schema_note) == (False, "")


def test_document_only_claude_probes_controls_then_sends_prompt_without_tools():
    from venator.llm import complete

    calls = []

    def runner(invocation):
        calls.append(invocation)
        if invocation.argv == ("claude", "--help"):
            return Result(0, "--safe-mode --tools --no-session-persistence", "")
        return envelope("draft")

    result = complete("untrusted job text", document_only=True, runner=runner,
                      environ={**PARENT, "VENATOR_LLM_RUNTIME": "claude"})
    assert result.text == "draft"
    assert len(calls) == 2
    assert calls[0].stdin == ""
    assert calls[1].stdin == "untrusted job text"
    assert calls[1].argv[-4:] == ("--safe-mode", "--tools", "", "--no-session-persistence")
    assert "--bare" not in calls[1].argv
    assert calls[1].cwd is None  # runner supplies its existing neutral temporary cwd


@pytest.mark.parametrize("help_result", [
    Result(0, "--tools --no-session-persistence", ""),
    Result(0, "--safe-mode --no-session-persistence", ""),
    Result(0, "--safe-mode --tools", ""),
    Result(1, "--safe-mode --tools --no-session-persistence", ""),
    FileNotFoundError("missing"),
])
def test_document_only_claude_missing_controls_never_sends_prompt(help_result):
    runner = Recorder(help_result)
    with pytest.raises(LaneUnavailable, match="no prompt was sent"):
        ClaudeLane().complete(Request("private resume", document_only=True), runner, PARENT)
    assert runner.only.argv == ("claude", "--help")
    assert runner.only.stdin == ""


def test_codex_document_only_runs_with_restrictions_and_preserves_defaults():
    from venator.llm import complete
    from venator.llm.lanes import CODEX_DOCUMENT_DISABLED_FEATURES

    runner = Recorder(Result(0, CODEX_SUCCESS_FIXTURE.read_text(), ""))
    result = complete("private resume", document_only=True, runner=runner,
                      environ={**PARENT, "VENATOR_LLM_RUNTIME": "codex"})
    assert result.lane == "codex"
    argv = runner.only.argv
    assert argv[1:5] == CODEX_SAFETY_FLAGS
    assert argv[5] == "exec"
    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert 'web_search="disabled"' in argv and "mcp_servers={}" in argv
    assert {argv[i + 1] for i, flag in enumerate(argv) if flag == "--disable"} == set(CODEX_DOCUMENT_DISABLED_FEATURES)
    assert runner.only.stdin == "private resume"
    assert runner.only.cwd is None
    ordinary = CodexLane().argv({})
    assert "--disable" not in ordinary and "--ignore-user-config" not in ordinary
    assert ordinary[1:5] == CODEX_SAFETY_FLAGS


def test_document_only_api_has_no_tools_and_never_spawns_cli():
    poster = FakePoster(200, CHAT_BODY)
    ApiKeyLane(poster=poster).complete(Request("private resume", document_only=True), unusable, CONFIGURED)
    assert len(poster.calls) == 1
    assert set(poster.calls[0][2]) == {"model", "messages"}
