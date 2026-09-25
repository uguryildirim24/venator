"""Finding a runtime's executable, and reading what it says back.

Two Windows properties of the one place this package starts a child process.
Neither has ever been run on Windows — there is no Windows machine here — so
both are reasoned from documented behaviour and exercised by simulation, and the
seams (`os_name`, `which`) exist for that and for nothing else.

**Resolution.** `CreateProcessW` with a NULL application name appends `.exe` to
an extension-less command and never reads `PATHEXT`; it cannot start a batch
file at all ("To run a batch file, you must start the command interpreter").
Anthropic's native Windows installer leaves a real `claude.exe` at
`%USERPROFILE%\\.local\\bin`, so the bare name already works on that route and
`shutil.which` buys nothing. The npm route leaves `claude.cmd`, which the bare
name misses — and which `shutil.which` returns happily, because `PATHEXT`
contains `.CMD`. A naive `which` swap therefore trades one broken spawn for
another while looking resolved. So: prefer an executable image, and diagnose
anything else `shutil.which` finds rather than trying to run it — the test is
what `CreateProcess` can start, not a list of shim extensions, because a file
with no extension at all is found the same way and starts no better. Running one through `COMSPEC /c` is
deliberately out of scope — it moves the argv into cmd.exe's grammar, where
Python adds no escaping of its own.

**Decoding.** `text=True` decodes with `locale.getencoding()`, which on a stock
Windows install is the ANSI code page. A runtime's reply is UTF-8 JSON from a
Node process, so cp1252 either mangles it into mojibake that is appended to
`data/decisions/` forever or raises `UnicodeDecodeError` out of `communicate()`,
past every handler in the lanes.

Naming the encoding fixes the first half and changes nothing on POSIX. Naming
`errors="replace"` is the second half and *does* change POSIX: it replaces the
implicit `errors="strict"` that came with `text=True`, so a byte that used to
end the run now decodes to U+FFFD. The trade is deliberate — an exception
raised inside `communicate()` cannot say which command failed — and it is only
safe because the damage is refused rather than recorded. The last four tests
below are that refusal, on the platform this suite actually runs on.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any

import pytest

from venator.llm import lanes, runtime
from venator.llm.runtime import (
    Invocation,
    LaneFailed,
    LaneUnavailable,
    Request,
    Result,
    UnspawnableCommand,
    resolve_executable,
    run_process,
)

NPM_SHIM = r"C:\Users\owner\AppData\Roaming\npm\claude.cmd"
NATIVE_EXE = r"C:\Users\owner\.local\bin\claude.exe"


def path_holding(**entries: str):
    """A `shutil.which` that finds exactly these commands and nothing else."""

    def which(command: str) -> str | None:
        return entries.get(command)

    return which


def test_posix_resolution_is_the_bare_name_and_nothing_is_searched() -> None:
    # Byte-identical to what this always did. `which` is not even consulted, so
    # a POSIX spawn cannot start behaving differently because PATH moved.
    def refuse(command: str) -> str | None:
        raise AssertionError(f"which() must not be called on POSIX (got {command!r})")

    assert resolve_executable("claude", os_name="posix", which=refuse) == "claude"


def test_the_native_installers_exe_is_resolved_to_its_absolute_path() -> None:
    which = path_holding(**{"claude.exe": NATIVE_EXE})

    assert resolve_executable("claude", os_name="nt", which=which) == NATIVE_EXE


def test_an_exe_wins_over_a_shim_of_the_same_name() -> None:
    # Both installers' output on PATH at once. CreateProcess can start one of
    # them, so that is the one to hand it.
    which = path_holding(**{"claude.exe": NATIVE_EXE, "claude": NPM_SHIM})

    assert resolve_executable("claude", os_name="nt", which=which) == NATIVE_EXE


def test_nothing_on_path_still_falls_back_to_the_bare_name() -> None:
    # So the spawn raises FileNotFoundError and the lane says "not installed",
    # which is then true. The fallback is the whole reason this cannot report a
    # missing command itself.
    assert resolve_executable("claude", os_name="nt", which=path_holding()) == "claude"


@pytest.mark.parametrize("shim", ["claude.cmd", "claude.bat", "claude.ps1"])
def test_a_shim_is_diagnosed_rather_than_spawned(shim: str) -> None:
    which = path_holding(claude=rf"C:\Users\owner\AppData\Roaming\npm\{shim}")

    with pytest.raises(UnspawnableCommand) as raised:
        resolve_executable("claude", os_name="nt", which=which)

    assert raised.value.command == "claude"
    # The filename, never the directory: this text is shown on screen
    # and written to logs, and the npm prefix carries someone's home
    # directory.
    assert raised.value.found == shim
    assert "owner" not in str(raised.value)


def test_an_extension_less_file_on_path_is_diagnosed_too() -> None:
    # The rule is what CreateProcess can start, not a list of shim extensions.
    # CreateProcessW appends `.exe` to a bare command and never reads PATHEXT,
    # so a file on PATH named `claude` with no extension is found by
    # shutil.which and is exactly as unstartable as a `.cmd`. Returning the
    # bare name here sent that person to "not installed", which is the same lie
    # the shim branch exists to avoid.
    which = path_holding(claude=r"C:\Users\owner\bin\claude")

    with pytest.raises(UnspawnableCommand) as raised:
        resolve_executable("claude", os_name="nt", which=which)

    assert raised.value.found == "claude"
    assert "owner" not in str(raised.value)


def test_the_shim_becomes_a_lane_unavailable_that_does_not_claim_it_is_missing() -> None:
    def runner(invocation: Invocation) -> Result:
        raise UnspawnableCommand("claude", "claude.cmd")

    with pytest.raises(LaneUnavailable) as raised:
        lanes.ClaudeLane().complete(Request(prompt="hello"), runner, {})

    unavailable = raised.value
    assert unavailable.kind == LaneUnavailable.NOT_SPAWNABLE
    assert unavailable.kind != LaneUnavailable.NOT_INSTALLED
    assert "Windows cannot start" in unavailable.reason
    assert "claude.cmd" not in unavailable.reason
    assert "is installed" in unavailable.reason
    # The remedy names the thing to do differently, not the thing already done.
    assert "npm" in unavailable.remedy
    assert ".exe" in unavailable.remedy
    # And it offers npm as the likely cause rather than asserting it: an
    # extension-less file reaches the same remedy, and telling that person
    # "what is on PATH is a batch shim" would be a confident false statement
    # about their own machine.
    assert "is a batch shim" not in unavailable.remedy


def test_the_codex_lane_says_the_same_true_thing_about_its_own_command() -> None:
    def runner(invocation: Invocation) -> Result:
        raise UnspawnableCommand("codex", "codex.cmd")

    with pytest.raises(LaneUnavailable) as raised:
        lanes.CodexLane().complete(Request(prompt="hello"), runner, {})

    assert raised.value.kind == LaneUnavailable.NOT_SPAWNABLE
    assert "codex" in raised.value.remedy
    assert "claude" not in raised.value.remedy


def test_a_missing_command_is_still_reported_as_not_installed() -> None:
    # The existing path, unchanged: the new branch must not swallow it.
    def runner(invocation: Invocation) -> Result:
        raise FileNotFoundError(2, "The system cannot find the file specified")

    with pytest.raises(LaneUnavailable) as raised:
        lanes.ClaudeLane().complete(Request(prompt="hello"), runner, {})

    assert raised.value.kind == LaneUnavailable.NOT_INSTALLED


def test_the_child_is_decoded_as_utf8_rather_than_the_machines_code_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def spy(argv: list[str], **kwargs: Any) -> Completed:
        seen["argv"] = argv
        seen.update(kwargs)
        return Completed()

    monkeypatch.setattr(runtime.subprocess, "run", spy)

    run_process(Invocation(argv=("claude", "-p"), stdin="hello", environment={}))

    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"
    # `text=True` would have decoded with locale.getencoding(); naming the
    # encoding is what removes the platform from the answer.
    assert seen.get("text") is None


def test_the_spawn_resolves_argv0_and_leaves_every_other_argument_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def spy(argv: list[str], **kwargs: Any) -> Completed:
        seen["argv"] = argv
        return Completed()

    monkeypatch.setattr(runtime.subprocess, "run", spy)
    monkeypatch.setattr(runtime, "resolve_executable", lambda command: NATIVE_EXE)

    run_process(
        Invocation(
            argv=("claude", "-p", "--model", "sonnet", "--output-format", "json"),
            stdin="hello",
            environment={},
        )
    )

    assert seen["argv"] == [
        NATIVE_EXE,
        "-p",
        "--model",
        "sonnet",
        "--output-format",
        "json",
    ]


def test_an_environment_mapping_still_reaches_the_child_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def spy(argv: list[str], **kwargs: Any) -> Completed:
        seen.update(kwargs)
        return Completed()

    monkeypatch.setattr(runtime.subprocess, "run", spy)
    environment: Mapping[str, str] = {"PATH": "/usr/bin", "HOME": "/home/owner"}

    run_process(Invocation(argv=("claude",), stdin="", environment=environment))

    assert seen["env"] == {"PATH": "/usr/bin", "HOME": "/home/owner"}


def test_a_byte_that_does_not_decode_now_reaches_the_caller_as_u_fffd() -> None:
    # The honest statement of what `errors="replace"` changed, run against a
    # real child on this platform rather than reasoned about. Before it, this
    # raised UnicodeDecodeError out of communicate(); after it, the caller gets
    # the reply with the undecodable bytes replaced. Both halves of that
    # sentence matter, which is why the comment in run_process says so and this
    # asserts it.
    emit = "import sys; sys.stdout.buffer.write(b'{\"result\": \"caf\\xff\"}')"

    result = run_process(
        Invocation(argv=(sys.executable, "-c", emit), stdin="", environment={})
    )

    assert result.stdout.count(runtime.REPLACEMENT_CHARACTER) == 1
    # Still valid JSON — which is precisely why the lane has to look. Nothing
    # downstream of here would notice.
    assert json.loads(result.stdout)["result"] == "caf" + runtime.REPLACEMENT_CHARACTER


def test_a_reply_that_did_not_decode_is_refused_instead_of_being_recorded() -> None:
    # data/decisions/ is append-only (ADR-0001). A mangled reason appended to it
    # is permanent, and a run that fails loudly can simply be run again.
    mangled = '{"type": "result", "result": "the applicant caf\ufffd\ufffd"}'

    def runner(invocation: Invocation) -> Result:
        return Result(returncode=0, stdout=mangled, stderr="")

    with pytest.raises(LaneFailed) as raised:
        lanes.ClaudeLane().complete(Request(prompt="hello"), runner, {})

    message = str(raised.value)
    assert "2" in message
    # Constructed, never quoted. A lane failure is shown on screen and
    # written to logs, and bytes that did not decode are exactly the bytes
    # nobody can vouch for.
    assert runtime.REPLACEMENT_CHARACTER not in message
    assert "the applicant" not in message
    assert "caf" not in message


def test_the_codex_lane_refuses_the_same_thing() -> None:
    def runner(invocation: Invocation) -> Result:
        return Result(
            returncode=0,
            stdout='{"type": "item.completed", "item": {"text": "caf\ufffd"}}',
            stderr="",
        )

    with pytest.raises(LaneFailed) as raised:
        lanes.CodexLane().complete(Request(prompt="hello"), runner, {})

    assert runtime.REPLACEMENT_CHARACTER not in str(raised.value)


def test_a_reply_that_decoded_cleanly_is_still_handed_back_whole() -> None:
    # The refusal must not cost the ordinary case anything, accents included.
    def runner(invocation: Invocation) -> Result:
        return Result(
            returncode=0,
            stdout='{"type": "result", "result": "café — naïve ✓"}',
            stderr="",
        )

    text = lanes.ClaudeLane().complete(Request(prompt="hello"), runner, {}).text

    assert text == "café — naïve ✓"
