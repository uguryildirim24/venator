"""The child environment on the branch a Windows machine takes.

The allowlist was written for POSIX and, read against a stock Windows box, every
name in it except PATH is unset: no HOME, no USER, no SHELL, no TMPDIR, no XDG_*.
So a runtime was handed `{"PATH": ...}` and nothing else — which is not a
hardened environment but a broken one. CPython's own suite records that "Windows
requires at least the SYSTEMROOT environment variable to start Python" and skips
its empty-environment test on win32 for exactly that reason.

These tests exist because that tier will otherwise never be executed by anybody:
this suite runs on Linux, and the failure it guards against is one where the
lane reports `not_installed` or quotes a runtime's own confusion, neither of
which points at the environment.

Nothing here relaxes anything. The POSIX assertions in `test_environment.py`
stand unchanged, `DENIED` is untouched, and the proxy entries — the one part of
this module that decides where traffic goes — are the same on both branches.
"""

from __future__ import annotations

import os

import pytest

from venator.llm.environment import (
    ALLOWED,
    DENIED,
    WINDOWS_ALLOWED,
    allowed_names,
    child_environment,
)

# A Windows environment as `os.environ` presents it: upper-cased keys, because
# `os._Environ.encodekey` upper-cases every one of them on that platform. A test
# that spelled these the way Windows itself does — `SystemRoot`, `ProgramData` —
# would be handing a *plain dict* to code that looks names up case-sensitively,
# and would prove the opposite of what it claimed.
WINDOWS_PARENT = {
    "PATH": r"C:\Windows\system32;C:\Users\owner\.local\bin",
    "SYSTEMROOT": r"C:\Windows",
    "WINDIR": r"C:\Windows",
    "COMSPEC": r"C:\Windows\system32\cmd.exe",
    "PATHEXT": ".COM;.EXE;.BAT;.CMD",
    "TEMP": r"C:\Users\owner\AppData\Local\Temp",
    "TMP": r"C:\Users\owner\AppData\Local\Temp",
    "APPDATA": r"C:\Users\owner\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\owner\AppData\Local",
    "USERPROFILE": r"C:\Users\owner",
    "HOMEDRIVE": "C:",
    "HOMEPATH": r"\Users\owner",
    "NUMBER_OF_PROCESSORS": "8",
    "ANTHROPIC_API_KEY": "planted-anthropic-key",
    "OPENAI_API_KEY": "planted-openai-key",
    "SOME_UNRELATED_VARIABLE": "kept out on principle",
}


class CaseFoldingEnvironment(dict[str, str]):
    """`os.environ`'s Windows behaviour: every key looked up in upper case.

    Not decoration either. CPython's `os._Environ` upper-cases keys on Windows
    and does not override `get`, so `source.get("SYSTEMROOT")` finds a variable
    the OS spells `SystemRoot`. A plain dict does not, and the difference is the
    whole reason the allowlist is spelled in upper case.
    """

    def get(self, key: str, default: str | None = None) -> str | None:  # type: ignore[override]
        return super().get(key.upper(), default)

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(key.upper())


def test_the_windows_tier_only_adds() -> None:
    assert allowed_names("posix") == ALLOWED
    assert allowed_names("nt")[: len(ALLOWED)] == ALLOWED
    assert set(ALLOWED) <= set(allowed_names("nt"))
    assert set(allowed_names("nt")) - set(ALLOWED) == set(WINDOWS_ALLOWED)


def test_no_windows_name_is_credential_shaped() -> None:
    # The belt to the allowlist's braces has to hold on this branch too. If a
    # name added here ever matches, it is dropped on the way out and the run
    # breaks confusingly — better to fail here.
    assert [name for name in WINDOWS_ALLOWED if DENIED.search(name)] == []


def test_a_windows_child_gets_what_it_needs_to_start() -> None:
    child = child_environment(WINDOWS_PARENT, os_name="nt")

    # The one CPython names outright, and the ones a Node runtime reads to find
    # a scratch directory and this account's own configuration.
    assert child["SYSTEMROOT"] == r"C:\Windows"
    assert child["COMSPEC"] == r"C:\Windows\system32\cmd.exe"
    assert child["PATHEXT"] == ".COM;.EXE;.BAT;.CMD"
    assert child["TEMP"] == r"C:\Users\owner\AppData\Local\Temp"
    assert child["APPDATA"] == r"C:\Users\owner\AppData\Roaming"
    assert child["LOCALAPPDATA"] == r"C:\Users\owner\AppData\Local"
    assert child["USERPROFILE"] == r"C:\Users\owner"
    assert child["HOMEDRIVE"] == "C:"
    assert child["HOMEPATH"] == r"\Users\owner"


def test_a_windows_child_gets_nothing_beyond_the_two_tiers() -> None:
    child = child_environment(WINDOWS_PARENT, os_name="nt")

    assert set(child) <= set(allowed_names("nt"))
    assert "SOME_UNRELATED_VARIABLE" not in child


def test_planted_keys_do_not_survive_the_windows_branch_either() -> None:
    child = child_environment(WINDOWS_PARENT, os_name="nt")

    assert "ANTHROPIC_API_KEY" not in child
    assert "OPENAI_API_KEY" not in child
    assert "planted-anthropic-key" not in "".join(child.values())
    assert "planted-openai-key" not in "".join(child.values())


def test_a_careless_addition_to_the_windows_tier_still_does_not_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "venator.llm.environment.WINDOWS_ALLOWED",
        (*WINDOWS_ALLOWED, "ANTHROPIC_API_KEY"),
    )

    assert "ANTHROPIC_API_KEY" not in child_environment(WINDOWS_PARENT, os_name="nt")


def test_the_windows_names_survive_the_case_folding_os_environ_really_does() -> None:
    """The spelling Windows itself uses reaches the child through `os.environ`."""
    parent = CaseFoldingEnvironment(
        {
            "PATH": r"C:\Windows\system32",
            "SYSTEMROOT": r"C:\Windows",
            "PROGRAMDATA": r"C:\ProgramData",
        }
    )

    child = child_environment(parent, os_name="nt")

    assert child["SYSTEMROOT"] == r"C:\Windows"
    assert child["PROGRAMDATA"] == r"C:\ProgramData"


def test_a_windows_name_never_crosses_on_a_posix_machine() -> None:
    # The split is what keeps this true: on POSIX nothing sets SYSTEMROOT for a
    # good reason, and if something did it would not be an OS fact.
    child = child_environment({**WINDOWS_PARENT, "HOME": "/home/owner"}, os_name="posix")

    assert child == {"PATH": WINDOWS_PARENT["PATH"], "HOME": "/home/owner"}


def test_the_platform_is_read_from_os_name_when_nobody_names_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")

    assert child_environment(WINDOWS_PARENT)["SYSTEMROOT"] == r"C:\Windows"


def test_a_relocated_configuration_directory_reaches_a_windows_child() -> None:
    # Anthropic documents this variable for Windows as well as Linux, and on
    # Windows it is what moves `%USERPROFILE%\.claude\.credentials.json`
    # (https://code.claude.com/docs/en/env-vars). It lives in the shared tier
    # because a person sets it themselves on every platform; this asserts the
    # branch a Windows machine actually takes.
    parent = {**WINDOWS_PARENT, "CLAUDE_CONFIG_DIR": r"C:\Users\owner\claude-work"}

    child = child_environment(parent, os_name="nt")

    assert child["CLAUDE_CONFIG_DIR"] == r"C:\Users\owner\claude-work"
    assert set(child) <= set(allowed_names("nt"))
    assert "ANTHROPIC_API_KEY" not in child
    assert "OPENAI_API_KEY" not in child
    assert "SOME_UNRELATED_VARIABLE" not in child


def test_the_windows_spelling_of_the_configuration_directory_resolves() -> None:
    # `os.environ` upper-cases every key on Windows, so a person who typed
    # `set Claude_Config_Dir=...` is still found — the same reason every name in
    # WINDOWS_ALLOWED is spelled in upper case here.
    parent = CaseFoldingEnvironment(
        {
            "PATH": r"C:\Windows\system32",
            "SYSTEMROOT": r"C:\Windows",
            "CLAUDE_CONFIG_DIR": r"C:\Users\owner\claude-work",
        }
    )

    assert (
        child_environment(parent, os_name="nt")["CLAUDE_CONFIG_DIR"]
        == r"C:\Users\owner\claude-work"
    )
