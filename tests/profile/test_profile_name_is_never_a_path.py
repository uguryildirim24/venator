"""`--profile` takes a name, and the refusal means the same thing on every platform.

The check used to be `name != Path(name).name`, which is correct — and correct
only by accident of which flavour `Path` happens to be. On Windows `Path` is
`WindowsPath` and refuses `a\\b`, `C:foo`, `\\\\server\\share\\x` and `a\\`; on Linux
`Path` is `PosixPath` and accepts all four. So one `--profile` value meant two
different things on two machines, the strict half was never exercised by a suite
that runs on Linux, and a refactor to `PurePosixPath` — or to `os.path.basename`
— would have opened it silently with every test still green.

Nothing here is about traversal being *possible* today: it was not, on Windows.
It is about the guard being written down rather than inherited, and about the
half of it nobody could see being visible on the machine CI actually uses.

An empty Profile name is the failure this whole check exists for: `--profile ""`
once resolved to `.` and loaded a Profile with no Hard Filter, which passes every
Posting. That case has its own coverage; these are its neighbours.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from venator.profile.select import ProfileError, available_profiles, resolve_profile_dir

REPOSITORY = Path(__file__).resolve().parents[2]

# Every shape the Windows path layer would have caught and the POSIX one would
# not, plus the two that were always caught, so the table reads as one rule.
NOT_A_NAME = [
    r"a\b",
    r"..\..\etc",
    "C:foo",
    r"C:\Users\owner\profiles\sample",
    r"\\server\share\x",
    "a\\",
    "a/b",
    "../../etc",
    ".",
    "..",
    ".hidden",
    # Win32 strips trailing dots while normalising, so this would have opened
    # `profiles\sample` and announced a Profile whose name is not the one typed.
    "sample.",
]


@pytest.mark.parametrize("name", NOT_A_NAME)
def test_a_value_that_is_a_path_is_refused_by_name(name: str) -> None:
    with pytest.raises(ProfileError) as raised:
        resolve_profile_dir(name, profiles_dir=REPOSITORY / "profiles", environ={})

    assert "not a path" in str(raised.value)
    assert "--profile-dir" in str(raised.value)


@pytest.mark.parametrize("name", ["a-b", "a_b", "a.b", "Sample2", "a b"])
def test_an_ordinary_name_is_not_caught_by_the_wider_rule(name: str) -> None:
    """These are not paths and must stay usable; the error names the Profile."""
    with pytest.raises(ProfileError) as raised:
        resolve_profile_dir(name, profiles_dir=REPOSITORY / "profiles", environ={})

    assert "not a path" not in str(raised.value)
    assert "no Profile named" in str(raised.value)
