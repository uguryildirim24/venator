"""A secret must never reach `data/`, from any path in the key lane.

`data/` is the transport layer (docs/adr/0001-git-as-transport.md): committed,
append-only, and never rewritten. A key that lands there is in the history for
good — it is not a bug anyone can fix forward, only one they can rotate around.

So this file does not test that one particular error body is scrubbed. It plants
one sentinel key, hands it to a provider that echoes it back through every path
that exists, and asserts the sentinel reaches neither the caller nor the store.
Adding a new call site to the key lane without thinking about this should make
something here go red.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from venator.llm import complete
from venator.llm.environment import child_environment
from venator.llm.lanes import ApiKeyLane, ClaudeLane, CodexLane
from venator.schedule.loop import append_heartbeat
from venator.secrets import scrub, scrub_record


SENTINEL = "sk-venator-sentinel-0123456789abcdefghij"

# The same mistake, made with a key whose shape is a valid POSIX variable name.
# The scan below never saw the pasted-key path because the sentinel was only
# ever planted in the variable that holds the key, never in the one that is
# supposed to hold its *name* — which is exactly where a person pastes it.
IDENTIFIER_SENTINEL = "gsk_venatorsentinel0123456789abcdefgh"

PASTED_INTO_THE_NAME = {
    "a dashed key pasted into the name variable": SENTINEL,
    "an identifier-shaped key pasted into the name variable": IDENTIFIER_SENTINEL,
}

CONFIGURED = {
    "PATH": "/usr/bin",
    "HOME": "/home/owner",
    "VENATOR_LLM_RUNTIME": "api",
    "VENATOR_LLM_API_URL": f"https://endpoint.example/v1/chat?token={SENTINEL}",
    "VENATOR_LLM_API_MODEL": "a-model",
    "VENATOR_LLM_API_KEY_VAR": "MY_OWN_KEY",
    "MY_OWN_KEY": SENTINEL,
}


class Echoing:
    """A provider doing the worst honest thing: quoting the key back at you."""

    def __init__(self, status: int, body: str, raises: Exception | None = None) -> None:
        self.status = status
        self.body = body
        self.raises = raises

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> tuple[int, str]:
        if self.raises is not None:
            raise self.raises
        return self.status, self.body


HOSTILE = {
    "refusal quoting the key": Echoing(401, f'{{"error": "bad key {SENTINEL}"}}'),
    "server error quoting the key": Echoing(500, f"upstream rejected {SENTINEL}"),
    "success with an unreadable body": Echoing(200, f"not json at all, {SENTINEL}"),
    "success with the key in the completion": Echoing(
        200, json.dumps({"choices": [{"message": {"content": f"your key {SENTINEL} is bad"}}]})
    ),
    "success with the wrong json shape": Echoing(200, json.dumps({"key": SENTINEL})),
    "transport error quoting the url": Echoing(
        0, "", raises=ConnectionError(f"cannot reach https://endpoint.example/v1?token={SENTINEL}")
    ),
    "transport error quoting a header": Echoing(
        0, "", raises=RuntimeError(f"Authorization: Bearer {SENTINEL}")
    ),
}


def lanes_with(poster: Echoing) -> list[object]:
    return [ClaudeLane(), CodexLane(), ApiKeyLane(poster=poster)]


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_nothing_the_key_lane_hands_back_carries_the_key(name: str) -> None:
    """Whatever leaves `complete` — an answer or an exception — is clean."""
    poster = HOSTILE[name]
    try:
        answer = complete(
            "prompt",
            lanes=lanes_with(poster),
            runner=_no_child_process,
            environ=CONFIGURED,
        )
    except Exception as error:  # noqa: BLE001 — every exception type is in scope
        leaked = f"{error}\n{error!r}\n{getattr(error, 'args', ())}"
    else:
        leaked = answer.text
    assert SENTINEL not in leaked, f"{name} leaked the key out of the lane"


def _no_child_process(invocation: object) -> object:
    raise AssertionError("the key lane must not start a child process")


def test_the_heartbeat_scan_is_not_vacuous(tmp_path: Path) -> None:
    """Prove the searches below are not vacuous."""
    heartbeat = tmp_path / "runs.jsonl"
    append_heartbeat(heartbeat, "filters", "error", error="a plain sentence, no key in it")
    assert "a plain sentence, no key in it" in heartbeat.read_text()


@pytest.mark.parametrize("name", sorted(PASTED_INTO_THE_NAME))
def test_a_key_pasted_into_the_name_variable_never_leaves_complete(name: str) -> None:
    """The route this scan used to miss: the key is the *name* of the variable."""
    pasted = PASTED_INTO_THE_NAME[name]
    try:
        answer = complete(
            "prompt",
            lanes=lanes_with(HOSTILE["refusal quoting the key"]),
            runner=_no_child_process,
            environ={**CONFIGURED, "VENATOR_LLM_API_KEY_VAR": pasted},
        )
    except Exception as error:  # noqa: BLE001 — every exception type is in scope
        leaked = f"{error}\n{error!r}\n{getattr(error, 'args', ())}"
    else:
        leaked = answer.text
    assert pasted not in leaked, f"{name} leaked the key out of the lane"


KEY_SHAPES = [
    pytest.param(SENTINEL, id="anthropic-style"),
    pytest.param(IDENTIFIER_SENTINEL, id="groq-style"),
    pytest.param("hf_venatorsentinel0123456789abcdefgh", id="hugging-face"),
    pytest.param("ghp_venatorsentinel0123456789abcdefgh", id="github-classic"),
    pytest.param("github_pat_venatorsentinel0123456789", id="github-fine-grained"),
    pytest.param("a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", id="bare-hex"),
    pytest.param("AKIAVENATORSENTINEL0", id="aws-access-key-id"),
]


@pytest.mark.parametrize("key", KEY_SHAPES)
def test_the_write_boundary_strips_a_key_from_a_heartbeat(key: str, tmp_path: Path) -> None:
    heartbeat = tmp_path / "runs.jsonl"
    append_heartbeat(heartbeat, "filters", "error", error=f"something went wrong with {key}")
    written = heartbeat.read_text()
    assert key not in written
    assert "<redacted>" in written
    assert json.loads(written)["status"] == "error"


@pytest.mark.parametrize("key", KEY_SHAPES)
def test_the_scheduled_loop_writes_through_the_same_boundary(key: str, tmp_path: Path) -> None:
    heartbeat = tmp_path / "runs.jsonl"
    append_heartbeat(heartbeat, "filters", "error", error=f"stage blew up: {key}")
    written = heartbeat.read_text()
    assert key not in written
    assert json.loads(written)["stage"] == "filters"


def test_a_proxy_password_does_not_survive_the_boundary(tmp_path: Path) -> None:
    heartbeat = tmp_path / "runs.jsonl"
    append_heartbeat(
        heartbeat,
        "filters",
        "error",
        error="cannot reach http://someone:hunter2@proxy.corp:8080",
    )
    written = heartbeat.read_text()
    assert "hunter2" not in written
    assert "proxy.corp" in written, "the useful half of the diagnostic survives"


def test_the_boundary_leaves_an_ordinary_diagnostic_alone(tmp_path: Path) -> None:
    heartbeat = tmp_path / "runs.jsonl"
    diagnostic = "claude -p exited 1: context window exceeded for greenhouse:example:1"
    append_heartbeat(
        heartbeat,
        "filters",
        "error",
        {"pending": 40, "recorded": 12},
        error=diagnostic,
    )
    row = json.loads(heartbeat.read_text())
    assert "<redacted>" not in heartbeat.read_text()
    assert row["error"] == diagnostic
    assert "pending" not in row and "recorded" not in row


JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ2ZW5hdG9yIn0.venatorsentinelsignature"

GLUED_BEFORE = [
    pytest.param("\n", id="start-of-a-later-line"),
    pytest.param("\t", id="after-a-tab"),
    pytest.param("\r", id="after-a-carriage-return"),
    pytest.param("clé", id="glued-to-a-non-ascii-word"),
]


@pytest.mark.parametrize("before", GLUED_BEFORE)
@pytest.mark.parametrize("key", [*KEY_SHAPES, pytest.param(JWT, id="jwt")])
def test_a_key_the_serialiser_would_hide_does_not_survive_the_writer(
    before: str, key: str, tmp_path: Path
) -> None:
    text = f"the runtime refused{before}{key} was the credential it refused"
    heartbeat = tmp_path / "runs.jsonl"
    append_heartbeat(heartbeat, "filters", "error", error=text)
    written = heartbeat.read_text()
    assert key not in written
    assert "<redacted>" in written
    assert "the runtime refused" in json.loads(written)["error"]


def test_a_multi_line_diagnostic_still_reads_after_the_boundary(tmp_path: Path) -> None:
    heartbeat = tmp_path / "runs.jsonl"
    diagnostic = (
        "claude -p exited 1\n"
        "  at Object.<anonymous> (/usr/lib/node_modules/x.js:12:9)\n"
        "  the task-scheduler-timeout was reached for greenhouse:example:1"
    )
    append_heartbeat(heartbeat, "filters", "error", {"pending": 40}, error=diagnostic)
    row = json.loads(heartbeat.read_text())
    assert row["error"] == diagnostic
    assert "<redacted>" not in heartbeat.read_text()


def test_a_key_nested_below_the_top_level_does_not_survive_the_boundary(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "runs.jsonl"
    row = {
        "error": "the runtime refused",
        "attempts": [{"header": f"Authorization: Bearer {SENTINEL}"}],
        IDENTIFIER_SENTINEL: "a key used as a field name",
    }
    heartbeat.write_text(
        scrub(json.dumps(scrub_record(row), ensure_ascii=False)) + "\n",
        encoding="utf-8",
    )
    written = heartbeat.read_text()
    assert SENTINEL not in written
    assert IDENTIFIER_SENTINEL not in written
    assert json.loads(written)["error"] == "the runtime refused"


def test_the_windows_allowlist_carries_no_planted_key_across_the_boundary() -> None:
    """The same containment proof, on the branch a Windows machine takes.

    Appended rather than folded into the assertions above, because those are the
    property this file exists for and are left exactly as they were. What is new
    is that the allowlist now has two tiers, and a tier that only ever runs on a
    machine nobody here has is a tier nothing would have proved anything about.

    The parent environment is spelled the way `os.environ` presents itself on
    Windows — upper-cased keys — because that is what production passes. A plain
    dict is case-sensitive where `os._Environ` is not, so a test that wrote
    `SystemRoot` would prove nothing about the code that reads `SYSTEMROOT`.
    """
    windows_parent = {
        "PATH": r"C:\Windows\system32;C:\Users\owner\.local\bin",
        "SYSTEMROOT": r"C:\Windows",
        "USERPROFILE": r"C:\Users\owner",
        "APPDATA": r"C:\Users\owner\AppData\Roaming",
        "ANTHROPIC_API_KEY": SENTINEL,
        "ANTHROPIC_AUTH_TOKEN": SENTINEL,
        "ANTHROPIC_BASE_URL": f"https://someone-elses-gateway.example/{SENTINEL}",
        "OPENAI_API_KEY": SENTINEL,
        "MY_OWN_KEY": SENTINEL,
    }

    child = child_environment(windows_parent, os_name="nt")

    assert SENTINEL not in "".join(child.values())
    assert SENTINEL not in "".join(child)
    assert "ANTHROPIC_API_KEY" not in child
    assert "ANTHROPIC_AUTH_TOKEN" not in child
    assert "ANTHROPIC_BASE_URL" not in child
    assert "OPENAI_API_KEY" not in child
    assert "MY_OWN_KEY" not in child
    # And the tier did its job: the machine facts a runtime needs to start are
    # there, so this is a containment proof about a working environment rather
    # than about an empty one.
    assert child["SYSTEMROOT"] == r"C:\Windows"
    assert child["USERPROFILE"] == r"C:\Users\owner"
