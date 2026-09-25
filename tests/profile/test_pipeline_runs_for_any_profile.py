"""The pipeline runs for a Profile that has been filled in with nothing.

The point of the Profile refactor is that a person starts with an empty one.
Every stage must therefore degrade to "nothing is configured, so nothing is
killed and nothing is fetched" — and still record a Filter Decision for every
Posting, because no-silent-drops is not conditional on being configured.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).parents[2]
POSTINGS = [
    {
        "key": "greenhouse:acme:1",
        "source": "greenhouse",
        "board": "acme",
        "title": "Backend Engineer",
        "location": "Chicago, IL",
        "url": "https://example.test/apply/1",
        "description_html": "<p>Ten years of experience and a completed PhD required.</p>",
    },
    {
        "key": "greenhouse:acme:2",
        "source": "greenhouse",
        "board": "acme",
        "title": "Data Intern",
        "location": "Reykjavik, Iceland",
        "url": "https://example.test/apply/2",
        "description_html": "<p>Must be a U.S. citizen.</p>",
    },
]


def empty_profile(tmp_path: Path) -> Path:
    directory = tmp_path / "profiles" / "nobody"
    directory.mkdir(parents=True)
    for name in ("resume.yaml", "constraints.yaml", "targeting.yaml"):
        (directory / name).write_text("", encoding="utf-8")
    return directory


def stage(module: str, *arguments: object, cwd: Path = REPOSITORY) -> subprocess.CompletedProcess[str]:
    """Run one stage. `cwd` is the checkout the stage resolves `profiles/` against."""
    return subprocess.run(
        [sys.executable, "-m", module, *(str(argument) for argument in arguments)],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    postings_dir = tmp_path / "postings"
    postings_dir.mkdir()
    (postings_dir / "2026-08-18.jsonl").write_text(
        "".join(json.dumps(posting) + "\n" for posting in POSTINGS), encoding="utf-8"
    )
    return {
        "root": tmp_path,
        "profile": empty_profile(tmp_path),
        "postings": postings_dir,
        "decisions": tmp_path / "decisions",
        "database": tmp_path / "venator.db",
        "out": tmp_path / "fill",
    }


def test_discovery_asks_for_nothing_rather_than_failing(workspace: dict[str, Path]) -> None:
    result = stage(
        "venator.discover.run",
        "--profile", "nobody",
        "--postings-dir", workspace["postings"],
        cwd=workspace["root"],
    )

    assert result.returncode == 0, result.stderr
    assert "no board is registered" in result.stdout
    assert "total new postings: 0" in result.stdout


def test_every_posting_still_gets_a_filter_decision(workspace: dict[str, Path]) -> None:
    result = stage(
        "venator.match.run",
        "--profile", "nobody",
        "--postings-dir", workspace["postings"],
        "--decisions-dir", workspace["decisions"],
        cwd=workspace["root"],
    )

    assert result.returncode == 0, result.stderr
    decisions = [
        json.loads(line)
        for path in sorted(workspace["decisions"].glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]

    assert {decision["posting_key"] for decision in decisions} == {p["key"] for p in POSTINGS}
    assert all(decision["verdict"] == "pass" for decision in decisions)
    assert all(decision["reason"] == "no Hard Filter is configured; passed" for decision in decisions)
    assert len({decision["filters_version"] for decision in decisions}) == 1


def test_the_rest_of_the_pipeline_runs_on_that_output(workspace: dict[str, Path]) -> None:
    stage(
        "venator.match.run",
        "--profile", "nobody",
        "--postings-dir", workspace["postings"],
        "--decisions-dir", workspace["decisions"],
        cwd=workspace["root"],
    )

    view = stage(
        "venator.view.build",
        "--profile", "nobody",
        "--postings-dir", workspace["postings"],
        "--decisions-dir", workspace["decisions"],
        "--database", workspace["database"],
        "--track-dir", workspace["profile"] / "no-track",
        "--runs-file", workspace["profile"] / "no-runs.jsonl",
        cwd=workspace["root"],
    )
    assert view.returncode == 0, view.stderr
    assert workspace["database"].is_file()


def test_a_fill_plan_from_an_empty_profile_maps_nothing_and_still_refuses_to_submit(
    workspace: dict[str, Path],
) -> None:
    result = stage(
        "venator.fill.plan",
        "greenhouse:acme:1",
        "--profile", "nobody",
        "--postings-dir", workspace["postings"],
        "--out", workspace["out"],
        cwd=workspace["root"],
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads((workspace["out"] / "greenhouse-acme-1" / "plan.json").read_text(encoding="utf-8"))

    assert plan["never_submit"] is True
    assert plan["fields"] == []
    assert len(plan["unmapped"]) == 12
    assert all(entry["reason"] for entry in plan["unmapped"])
