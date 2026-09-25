"""`venator.view.build` must never quietly build a view with no employer names.

`company` for an ATS Posting comes from the resolved Profile's
``sources.names``, so a Profile that cannot be resolved does not degrade the
view a little — it empties one column for every board at once while the command
prints the same line and the same exit code a healthy build prints. The Profile
layer refuses to guess between two Profiles precisely so that a run belongs to a
known Owner; the view builder must let that refusal through.

The one absence it tolerates is an install that holds no Profile at all: the
view is disposable, a fresh checkout should still be able to produce a database,
and the operator is told in the output that employer names are missing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).parents[2]
POSTINGS = [
    {
        "key": "greenhouse:acmebio:1",
        "source": "greenhouse",
        "board": "acmebio",
        "title": "Research Associate",
        "location": "Boston, MA",
        "url": "https://example.test/apply/1",
    },
    {
        "key": "greenhouse:acmebio:2",
        "source": "greenhouse",
        "board": "acmebio",
        "title": "Laboratory Technician",
        "location": "Cambridge, MA",
        "url": "https://example.test/apply/2",
    },
]
TARGETING = """
profile:
  name: {name}
  scaffold: {scaffold}
sources:
  names:
    acmebio: {display}
"""


def write_profile(root: Path, name: str, *, display: str, scaffold: bool = False) -> Path:
    directory = root / "profiles" / name
    directory.mkdir(parents=True)
    (directory / "targeting.yaml").write_text(
        TARGETING.format(name=name, scaffold=str(scaffold).lower(), display=display),
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout with Postings whose employer name only a Profile can supply."""
    postings_dir = tmp_path / "data" / "postings"
    postings_dir.mkdir(parents=True)
    (postings_dir / "2026-08-24.jsonl").write_text(
        "\n".join(json.dumps(posting) for posting in POSTINGS) + "\n", encoding="utf-8"
    )
    return tmp_path


def build(checkout: Path, *arguments: object) -> subprocess.CompletedProcess[str]:
    """Run the view builder with `checkout` as the directory it resolves against."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "venator.view.build",
            "--postings-dir",
            str(checkout / "data" / "postings"),
            "--decisions-dir",
            str(checkout / "data" / "decisions"),
            "--database",
            str(checkout / "build" / "venator.db"),
            "--track-dir",
            str(checkout / "data" / "track"),
            "--runs-file",
            str(checkout / "data" / "runs.jsonl"),
            *(str(argument) for argument in arguments),
        ],
        cwd=checkout,
        env={**os.environ, "VENATOR_HOME": str(checkout / "install")},
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )


def companies(checkout: Path) -> list[str | None]:
    database = sqlite3.connect(checkout / "build" / "venator.db")
    try:
        return [row[0] for row in database.execute("SELECT company FROM postings ORDER BY key")]
    finally:
        database.close()


def test_the_implied_profile_names_the_employer(checkout: Path) -> None:
    write_profile(checkout, "sample", display="Acme Bio")

    result = build(checkout)

    assert result.returncode == 0, result.stderr
    assert companies(checkout) == ["Acme Bio", "Acme Bio"]


def test_two_profiles_and_no_choice_refuses_instead_of_emptying_the_column(
    checkout: Path,
) -> None:
    write_profile(checkout, "sample", display="Acme Bio")
    write_profile(checkout, "second", display="Acme Bio")

    result = build(checkout)

    assert result.returncode != 0
    assert "more than one Profile" in result.stderr
    assert "sample" in result.stderr and "second" in result.stderr
    assert not (checkout / "build" / "venator.db").exists(), (
        "a view built from an unresolved Profile is worse than no view: it looks fine"
    )


def test_a_stale_view_is_not_replaced_by_one_with_no_employer_names(checkout: Path) -> None:
    write_profile(checkout, "sample", display="Acme Bio")
    assert build(checkout).returncode == 0
    write_profile(checkout, "second", display="Acme Bio")

    result = build(checkout)

    assert result.returncode != 0
    assert companies(checkout) == ["Acme Bio", "Acme Bio"]


def test_a_named_profile_that_does_not_exist_is_reported(checkout: Path) -> None:
    write_profile(checkout, "sample", display="Acme Bio")

    result = build(checkout, "--profile", "nobody")

    assert result.returncode != 0
    assert "no Profile named 'nobody'" in result.stderr
    assert not (checkout / "build" / "venator.db").exists()


def test_no_profile_at_all_still_builds_and_says_the_names_are_missing(
    checkout: Path,
) -> None:
    result = build(checkout)

    assert result.returncode == 0, result.stderr
    assert "no Profile is configured" in result.stderr
    assert "employer display names" in result.stderr
    assert companies(checkout) == [None, None]


def test_a_scaffold_is_not_a_configured_profile(checkout: Path) -> None:
    write_profile(checkout, "example", display="Example Bio", scaffold=True)

    result = build(checkout)

    assert result.returncode == 0, result.stderr
    assert "no Profile is configured" in result.stderr
    assert companies(checkout) == [None, None]


def test_the_no_profile_build_names_how_many_postings_lost_an_employer_name(
    checkout: Path,
) -> None:
    """An Install holding no Profile is legitimate, so this warns and builds on.

    Absent means "not configured", not "invalid" — but the loss is otherwise
    silent and total: with no board registry every ATS Posting lands with
    `company` NULL and the dashboard reads "via Greenhouse" throughout.
    """
    result = build(checkout)

    assert result.returncode == 0, result.stderr
    assert f"warning: {len(POSTINGS)} of {len(POSTINGS)} Postings have no employer name" in result.stderr
    assert "no Profile is configured" in result.stderr


def test_a_healthy_build_warns_about_nothing(checkout: Path) -> None:
    write_profile(checkout, "sample", display="Acme Bio")

    result = build(checkout)

    assert result.returncode == 0, result.stderr
    assert "warning:" not in result.stdout
    assert "warning:" not in result.stderr


def built_line(checkout: Path) -> str:
    """The one line this command's stdout is contracted to carry."""
    database = checkout / "build" / "venator.db"
    return f"built {database}: {len(POSTINGS)} Postings, 0 Filter Decisions"


def test_the_warning_is_on_stderr_and_never_on_stdout(checkout: Path) -> None:
    """stdout is a machine-readable channel; the warning is addressed to a person.

    Asserting only that the text appears *somewhere* would pass today and pass
    again the day someone moved it back onto stdout, so this pins the stream:
    the diagnostics are on stderr, absent from stdout, and stdout is the `built`
    line by itself (coordination/CONTRACTS.md, "Command output streams").
    """
    result = build(checkout)

    assert result.returncode == 0, result.stderr
    assert "warning:" in result.stderr
    assert "no Profile is configured" in result.stderr
    assert "warning:" not in result.stdout
    assert "no employer name" not in result.stdout
    assert "no Profile is configured" not in result.stdout
    assert result.stdout.splitlines() == [built_line(checkout)]


def test_the_built_line_stays_on_stdout_for_a_healthy_build(checkout: Path) -> None:
    """The consumed line does not move either — stderr is for the diagnostics only."""
    write_profile(checkout, "sample", display="Acme Bio")

    result = build(checkout)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [built_line(checkout)]
    assert "built " not in result.stderr
