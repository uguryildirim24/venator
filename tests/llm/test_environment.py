from __future__ import annotations

import json
import sys

import pytest

from venator.llm import environment
from venator.llm.environment import child_environment
from venator.llm.runtime import Invocation, run_process

PARENT = {
    "PATH": "/usr/bin",
    "HOME": "/home/owner",
    "LANG": "en_US.UTF-8",
    "ANTHROPIC_API_KEY": "planted-anthropic-key",
    "ANTHROPIC_AUTH_TOKEN": "planted-anthropic-token",
    "ANTHROPIC_BASE_URL": "https://someone-elses-gateway.example",
    "OPENAI_API_KEY": "planted-openai-key",
    "OPENAI_BASE_URL": "https://someone-elses-gateway.example",
    "AWS_SECRET_ACCESS_KEY": "planted-aws-secret",
    "SOME_UNRELATED_VARIABLE": "kept out on principle",
}


def test_planted_keys_never_reach_the_child() -> None:
    child = child_environment(PARENT)

    assert "ANTHROPIC_API_KEY" not in child
    assert "ANTHROPIC_AUTH_TOKEN" not in child
    assert "ANTHROPIC_BASE_URL" not in child
    assert "OPENAI_API_KEY" not in child
    assert "OPENAI_BASE_URL" not in child
    assert "planted-anthropic-key" not in "".join(child.values())
    assert "planted-openai-key" not in "".join(child.values())


def test_only_allowlisted_names_cross_the_boundary() -> None:
    child = child_environment(PARENT)

    assert child == {"PATH": "/usr/bin", "HOME": "/home/owner", "LANG": "en_US.UTF-8"}
    assert "SOME_UNRELATED_VARIABLE" not in child
    assert "AWS_SECRET_ACCESS_KEY" not in child


def test_the_login_a_runtime_reads_for_itself_still_resolves() -> None:
    # HOME is the one variable the subscription lanes genuinely need: the
    # unmodified binary opens its own login under it. This adapter never does.
    child = child_environment(PARENT)

    assert child["HOME"] == "/home/owner"
    assert child["PATH"] == "/usr/bin"


def test_a_careless_addition_to_the_allowlist_still_does_not_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        environment, "ALLOWED", (*environment.ALLOWED, "ANTHROPIC_API_KEY")
    )

    assert "ANTHROPIC_API_KEY" not in child_environment(PARENT)


def test_a_credential_handed_in_as_an_extra_is_refused() -> None:
    with pytest.raises(ValueError, match="never carries vendor credentials"):
        child_environment(PARENT, extra={"OPENAI_API_KEY": "handed in by hand"})


def test_a_non_secret_extra_is_carried() -> None:
    child = child_environment(PARENT, extra={"VENATOR_LLM_CODEX_MODEL": "some-model"})

    assert child["VENATOR_LLM_CODEX_MODEL"] == "some-model"


def test_an_empty_parent_yields_an_empty_child() -> None:
    assert child_environment({}) == {}


# `CLAUDE_CONFIG_DIR` moves the whole configuration directory — "default:
# `~/.claude`. All settings, session history, and plugins are stored under this
# path, as are credentials on Linux and Windows"
# (https://code.claude.com/docs/en/env-vars). A child that cannot see it reads
# `~/.claude` while the person signed in somewhere else, and the lane reports
# "not signed in" with nothing pointing at the cause.
RELOCATED = {**PARENT, "CLAUDE_CONFIG_DIR": "/home/owner/.claude-work"}


def test_a_relocated_configuration_directory_reaches_the_child() -> None:
    assert child_environment(RELOCATED)["CLAUDE_CONFIG_DIR"] == "/home/owner/.claude-work"


def test_the_relocated_directory_widens_nothing_else() -> None:
    # One name added is one name added. The planted keys are in this parent too,
    # and the child is still exactly the allowlisted four.
    child = child_environment(RELOCATED)

    assert child == {
        "PATH": "/usr/bin",
        "HOME": "/home/owner",
        "CLAUDE_CONFIG_DIR": "/home/owner/.claude-work",
        "LANG": "en_US.UTF-8",
    }
    assert "ANTHROPIC_API_KEY" not in child
    assert "OPENAI_API_KEY" not in child
    assert "planted-anthropic-key" not in "".join(child.values())
    assert "planted-openai-key" not in "".join(child.values())


def test_a_blank_configuration_directory_is_dropped_rather_than_passed_on() -> None:
    # The existing truthiness filter, doing the one piece of validation this
    # variable needs: an empty value resolves to nothing useful in the child, and
    # dropping it leaves the documented default (`~/.claude`) in force instead.
    assert "CLAUDE_CONFIG_DIR" not in child_environment(
        {**PARENT, "CLAUDE_CONFIG_DIR": ""}
    )
    assert "CLAUDE_CONFIG_DIR" not in child_environment(PARENT)


@pytest.mark.parametrize(
    "value",
    [
        ".claude-work",
        "~/.claude-work",
        "/nonexistent/never/created",
        r"\\fileserver\share\.claude",
        "/home/owner/.claude-work\nOPENAI_API_KEY=planted-openai-key",
        "/home/owner/.claude-work=still-one-value",
    ],
    ids=[
        "relative",
        "unexpanded tilde",
        "a directory that does not exist",
        "a UNC path",
        "a value carrying a newline",
        "a value carrying an equals sign",
    ],
)
def test_a_hostile_or_malformed_directory_is_carried_verbatim(value: str) -> None:
    """Deliberate: this adapter does not adjudicate the value, and says why.

    What the string means is the child's to decide — it is Anthropic's own
    binary reading its own configuration — and every path where Venator refused
    or rewrote one would put the child back to reading a directory the person
    did not choose, which is the defect this entry exists to fix. The value can
    also not become a second variable: an environment crosses as an array of
    NAME=value pairs, so a newline or an `=` stays inside the value it is part
    of (test_a_value_cannot_smuggle_a_second_variable_past_the_allowlist).
    Anyone able to set this can already set PATH, which decides which binary
    `claude` even is.
    """
    child = child_environment({**PARENT, "CLAUDE_CONFIG_DIR": value})

    assert child["CLAUDE_CONFIG_DIR"] == value
    assert set(child) == {"PATH", "HOME", "LANG", "CLAUDE_CONFIG_DIR"}


def test_a_value_cannot_smuggle_a_second_variable_past_the_allowlist() -> None:
    """Run against a real child rather than reasoned about.

    The worry an added name deserves is whether its *value* can carry another
    name through. It cannot: execve takes an array of NAME=value strings and the
    child splits each on its first `=`, so the planted key below arrives as part
    of one directory string and never as a variable of its own.
    """
    smuggled = "/home/owner/.claude-work\nOPENAI_API_KEY=planted-openai-key\nX=Y"
    emit = "import json, os, sys; sys.stdout.write(json.dumps(dict(os.environ)))"

    result = run_process(
        Invocation(
            argv=(sys.executable, "-c", emit),
            stdin="",
            environment=child_environment(
                {**PARENT, "CLAUDE_CONFIG_DIR": smuggled}, os_name="posix"
            ),
        )
    )
    seen = json.loads(result.stdout)

    assert seen["CLAUDE_CONFIG_DIR"] == smuggled
    assert "OPENAI_API_KEY" not in seen
    assert "X" not in seen
    assert "ANTHROPIC_API_KEY" not in seen


def test_a_null_byte_in_the_value_stops_the_run_loudly() -> None:
    # The one malformed value the platform itself refuses, and it refuses it
    # before a child exists. Worth pinning: a silent truncation here would mean
    # a child reading half a path, which is the failure mode this whole entry is
    # about.
    #
    # The wording is CPython's, not this repository's, and it is not the same on
    # both platforms: POSIX raises "embedded null byte" and Windows raises
    # "embedded null character". Matching on the half they share keeps the
    # assertion where it belongs — that the platform refuses, before a child
    # exists — rather than on a message nobody here writes. Nothing about what
    # may reach a child moves: `ALLOWED`, `DENIED` and the proxy entries are
    # untouched, and the value is still refused rather than truncated.
    with pytest.raises(ValueError, match="embedded null"):
        run_process(
            Invocation(
                argv=(sys.executable, "-c", ""),
                stdin="",
                environment=child_environment(
                    {**PARENT, "CLAUDE_CONFIG_DIR": "/home/owner/.claude\x00work"},
                    os_name="posix",
                ),
            )
        )
