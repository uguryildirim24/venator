"""The launchd agent: what it names, and that every path in it is absolute.

A plist is a macOS artifact and `venator.schedule.agent`'s CLI refuses to write
one anywhere else. `launch_agent` itself is a pure function, though, and this
file drives it directly — on whatever host CI happens to be — so two kinds of
path end up in the same document and they have to be asked different questions.

The entries built from directories on *this* machine are joined by `pathlib` and
are absolute in this host's terms. `SEARCH_PATH_SUFFIX` is four macOS
directories written as literals in the generator, and they are absolute in
*macOS's* terms — a question `Path` answers "no" to on a Windows host, where a
rooted path with no drive letter is not absolute. Asking `Path` about them there
would fail a test about a plist on the one platform that cannot read one.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path, PurePosixPath

from venator.schedule.agent import (
    INTERVAL_SECONDS,
    LABEL,
    SEARCH_PATH_SUFFIX,
    launch_agent,
    render,
)

#: How this host's `pathlib` joins. Stated rather than asked of `Path`, which is
#: what builds the values under test.
SEPARATOR = os.sep

#: The four macOS directories the generator appends to PATH, written out rather
#: than imported. `SEARCH_PATH_SUFFIX` is imported as well, but only where the
#: question is "are these absolute in macOS's terms" — a question about whatever
#: the constant holds. What PATH *is* has to be spelled out, or the expectation
#: agrees with the generator however the generator changes, which is the exact
#: defect this branch already had to correct in `ui/tests/locations.test.ts`.
#: These are macOS literals and read the same on every host, so there is nothing
#: platform-shaped to translate.
MACOS_SEARCH_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def agent(tmp_path: Path, **changes: object) -> dict:
    """A generated agent for a checkout and an account that are not the author's."""
    home = tmp_path / "home" / "someone"
    uv = home / ".local" / "bin" / "uv"
    uv.parent.mkdir(parents=True)
    uv.touch()
    defaults: dict[str, object] = {
        "repository": tmp_path / "checkout" / "venator",
        "home": home,
        "uv": uv,
    }
    defaults.update(changes)
    (tmp_path / "checkout" / "venator").mkdir(parents=True, exist_ok=True)
    return launch_agent(**defaults)  # type: ignore[arg-type]


def test_launch_agent_is_well_formed_and_absolute(tmp_path: Path) -> None:
    plist = plistlib.loads(render(agent(tmp_path)))
    home = str(tmp_path / "home" / "someone")
    uv = f"{home}{SEPARATOR}.local{SEPARATOR}bin{SEPARATOR}uv"
    logs = f"{home}{SEPARATOR}Library{SEPARATOR}Logs{SEPARATOR}venator"

    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == [uv, "run", "python", "-m", "venator.schedule.loop"]
    assert plist["WorkingDirectory"] == str(tmp_path / "checkout" / "venator")
    assert plist["StartInterval"] == INTERVAL_SECONDS == 6 * 60 * 60
    assert plist["ProcessType"] == "Background"
    assert plist["StandardOutPath"].startswith(f"{logs}{SEPARATOR}")
    assert plist["StandardErrorPath"].startswith(f"{logs}{SEPARATOR}")
    assert plist["EnvironmentVariables"]["HOME"] == home
    # PATH is `uv`'s own directory and then the four macOS directories, joined
    # with ":" because that is macOS's separator. It is checked from the front
    # rather than split back apart: ":" is also what follows a drive letter, so
    # on a Windows host the joined value does not split into the parts that went
    # into it.
    assert plist["EnvironmentVariables"]["PATH"] == ":".join(
        [f"{home}{SEPARATOR}.local{SEPARATOR}bin", *MACOS_SEARCH_PATH]
    )
    # And the constant really is those four, so the import used below to ask
    # about absoluteness is asking about the same list this pinned.
    assert SEARCH_PATH_SUFFIX == MACOS_SEARCH_PATH
    # RunAtLoad would start a pipeline run the moment the agent is bootstrapped.
    assert "RunAtLoad" not in plist


def test_launch_agent_paths_are_all_absolute_and_name_no_author(tmp_path: Path) -> None:
    """launchd has no working directory of its own, so every path has to be absolute."""
    plist = plistlib.loads(render(agent(tmp_path)))
    variables = plist["EnvironmentVariables"]
    # The PATH entries are recovered from the constant the generator joined,
    # rather than split back out of the joined value — see the note above about
    # ":" being a drive letter's separator too.
    macos_suffix = ":" + ":".join(SEARCH_PATH_SUFFIX)
    assert variables["PATH"].endswith(macos_suffix)
    on_this_machine = [
        plist["ProgramArguments"][0],
        plist["WorkingDirectory"],
        plist["StandardOutPath"],
        plist["StandardErrorPath"],
        variables["PATH"][: -len(macos_suffix)],
        variables["HOME"],
    ]

    assert all(Path(path).is_absolute() for path in on_this_machine)
    assert all(PurePosixPath(path).is_absolute() for path in SEARCH_PATH_SUFFIX)
    # The temporary root can itself contain the local account name. Check
    # generated path segments, not the machine-dependent pytest prefix.
    assert not any(
        "sample" in str(Path(path).relative_to(tmp_path)) for path in on_this_machine
    )
    assert not any("sample" in path for path in SEARCH_PATH_SUFFIX)


def test_launch_agent_carries_the_profile_the_loop_runs_for(tmp_path: Path) -> None:
    plist = plistlib.loads(render(agent(tmp_path, profile="someone")))

    assert plist["ProgramArguments"][-2:] == ["--profile", "someone"]
