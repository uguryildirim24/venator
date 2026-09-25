"""Line endings are declared, so a Windows clone cannot move `filters_version`.

`filters_version()` hashes the *working-tree bytes* of a Profile's
constraints.yaml, without normalising anything —
deliberately, because normalising there would move the hash for everyone and
replay many Filter Decisions. So the bytes have to be the same on every machine
before the hash ever sees them, and that is `.gitattributes`' job.

Git for Windows' installer default is `core.autocrlf=true`, which rewrites LF to
CRLF in the working tree on checkout. targeting.yaml escapes it only by accident (it has NON_DECIDING_BLOCKS
exclusions, so it is YAML-parsed and re-serialised canonically before hashing).
It is asserted here anyway: the accident is not the guarantee.

This asks Git itself rather than parsing the file, so what is asserted is the
attribute that will actually be applied at checkout, precedence and all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from venator.profile.select import available_profiles

REPOSITORY = Path(__file__).resolve().parents[2]


def eol_attribute(path: Path) -> str:
    """What `git check-attr` says the `eol` attribute resolves to for `path`."""
    relative = path.relative_to(REPOSITORY)
    answer = subprocess.run(
        ["git", "check-attr", "eol", "--", str(relative)],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # "<path>: eol: lf"
    return answer.rsplit(": ", 1)[-1]


def hashed_paths() -> list[Path]:
    """Every file whose bytes reach `filters_version`, for every committed Profile."""
    paths: list[Path] = []
    for directory in [REPOSITORY / "profiles" / "example"]:
        paths.append(directory / "constraints.yaml")
        paths.append(directory / "targeting.yaml")
    return [path for path in paths if path.is_file()]


def test_the_repository_declares_its_line_endings() -> None:
    assert (REPOSITORY / ".gitattributes").is_file(), (
        "without a .gitattributes, Git for Windows' default core.autocrlf=true "
        "checks this repository out with CRLF and filters_version moves"
    )


@pytest.mark.parametrize("path", hashed_paths(), ids=lambda path: path.name)
def test_every_file_filters_version_hashes_is_pinned_to_lf(path: Path) -> None:
    assert eol_attribute(path) == "lf", (
        f"{path} reaches filters_version as raw or re-serialised bytes, so its "
        "line endings decide the hash — it must be checked out LF everywhere"
    )


def test_the_append_only_stores_are_pinned_to_lf() -> None:
    # A line written into a day file is permanent (ADR-0001). The writers pin
    # LF themselves (tests/ci/test_text_file_encoding.py); this is the other
    # half — that checkout does not hand the reader CRLF back.
    for path in sorted((REPOSITORY / "data").rglob("*.jsonl")):
        assert eol_attribute(path) == "lf", path


def test_the_committed_corpus_has_no_carriage_returns() -> None:
    for path in sorted((REPOSITORY / "data").rglob("*.jsonl")):
        assert b"\r" not in path.read_bytes(), path
