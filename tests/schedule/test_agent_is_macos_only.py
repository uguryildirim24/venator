"""The launchd generator refuses a platform that has no launchd.

`python -m venator.schedule.agent` had no platform check at all, so on Windows
it succeeded and printed a well-formed macOS plist full of
`C:\\Users\\...\\Library\\Logs\\venator\\loop.stdout.log`, with a PATH of Windows
directories joined by `:` instead of `;`. Nothing on Windows reads a plist, so
the output was inert — and confidently wrong, which is worse than an error,
because a person can act on it.

This deliberately builds no scheduler for anywhere else. `venator.schedule.loop`
still runs by hand on any platform, and the refusal says so rather than leaving
someone to work out why the file they generated does nothing.

`launch_agent` itself is untouched: it is a library function that builds a
macOS plist and its existing tests call it directly, on Linux, and should keep
working. The guard belongs at the CLI, which is what a person types.
"""

from __future__ import annotations

import sys

import pytest

from venator.schedule import agent


def test_the_cli_refuses_on_windows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["venator.schedule.agent"])

    with pytest.raises(SystemExit) as raised:
        agent.main()

    assert raised.value.code != 0
    message = capsys.readouterr().err
    assert "win32" in message
    assert "launchd" in message
    # It names what to do instead, rather than only what it will not do.
    assert "venator.schedule.loop" in message


def test_the_cli_refuses_on_linux_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Not a Windows guard with a Windows name: a plist is macOS's, full stop.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", ["venator.schedule.agent"])

    with pytest.raises(SystemExit):
        agent.main()

    assert "launchd" in capsys.readouterr().err


def test_nothing_is_written_when_the_platform_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    out = tmp_path / "loop.plist"  # type: ignore[operator]
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["venator.schedule.agent", "--out", str(out)])

    with pytest.raises(SystemExit):
        agent.main()

    assert not out.exists()


def test_macos_still_generates_the_plist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "argv", ["venator.schedule.agent"])
    monkeypatch.setattr(agent, "find_uv", lambda home: home / ".local" / "bin" / "uv")

    assert agent.main() == 0
