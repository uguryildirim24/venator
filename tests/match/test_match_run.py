from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.match.budget import alarm_can_be_armed
from venator.match.run import run
from venator.profile import Profile, load_profile


MATCHER_TARGETING = """\
profile:
  name: matcher-fixture
filters:
  enabled: [role_target]
  role_target:
    levels:
      - name: intern
        title_terms: [intern]
      - name: senior
        title_terms: [senior, director]
    accept: [intern]
"""


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def test_run_decides_every_posting_once_per_constraints_version(tmp_path: Path) -> None:
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    constraints = tmp_path / "constraints.yaml"
    constraints.write_text("locations: [Boston]\n", encoding="utf-8")
    write_jsonl(
        postings_dir / "2026-08-18.jsonl",
        [
            {
                "key": "source:board:pass",
                "title": "Intern",
                "location": "Boston, MA",
                "description_html": "Currently pursuing a BS.",
            },
            {
                "key": "source:board:kill",
                "title": "Senior Director",
                "location": "San Diego, CA",
                "description_html": "On-site.",
            },
        ],
    )

    profile = profile_at(tmp_path / "profiles" / "matcher", MATCHER_TARGETING)
    first = run(postings_dir, decisions_dir, constraints, profile=profile)
    second = run(postings_dir, decisions_dir, constraints, profile=profile)
    constraints.write_text("locations: [Boston, Remote US]\n", encoding="utf-8")
    replay = run(postings_dir, decisions_dir, constraints, profile=profile)

    assert first | {"filters_version": first["filters_version"]} == first
    assert first["processed"] == 2
    assert (first["pass"], first["kill"], first["skipped"]) == (1, 1, 0)
    assert second["processed"] == 0
    assert second["skipped"] == 2
    assert replay["processed"] == 2
    lines = [line for path in sorted(decisions_dir.glob("*.jsonl"))
             for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 4
    assert all("score" not in json.loads(line) for line in lines)


def test_employer_exclusion_replays_and_undo_restores_stored_postings(tmp_path: Path) -> None:
    postings = tmp_path / "postings"
    decisions = tmp_path / "decisions"
    write_jsonl(postings / "2026-08-18.jsonl", [
        {"key": "greenhouse:cvs:1", "board": "cvs", "title": "Intern"},
        {"key": "greenhouse:other:2", "board": "other", "title": "Intern"},
    ])
    directory = tmp_path / "profiles" / "local"
    base = "sources:\n  names:\n    cvs: CVS Health\n    other: Other\n  boards:\n    greenhouse: [cvs, other]\n"
    profile = profile_at(directory, base)
    first = run(postings, decisions, profile=profile)
    assert first["pass"] == 2
    profile = profile_at(directory, base + "filters:\n  employer:\n    exclude: [CVS Health]\n")
    excluded = run(postings, decisions, profile=profile)
    assert excluded["kill"] == 1 and excluded["pass"] == 1
    profile = profile_at(directory, base)
    restored = run(postings, decisions, profile=profile)
    assert restored["pass"] == 2 and restored["kill"] == 0
    assert restored["filters_version"] == first["filters_version"]
    rows = [json.loads(line) for path in decisions.glob("*.jsonl") for line in path.read_text().splitlines()]
    assert [(row["posting_key"], row["verdict"]) for row in rows[-2:]] == [
        ("greenhouse:cvs:1", "pass"), ("greenhouse:other:2", "pass")
    ]


def test_location_kill_replays_when_profile_removes_scope(tmp_path: Path) -> None:
    postings = tmp_path / "postings"
    decisions = tmp_path / "decisions"
    write_jsonl(postings / "2026-08-18.jsonl", [
        {"key": "board:1", "board": "board", "title": "Intern", "location": "San Diego, CA"},
    ])
    directory = tmp_path / "profiles" / "local"
    restricted = profile_at(directory, "filters:\n  enabled: [location]\n  location:\n    regions: [MA, RI, NH, CT, VT, ME]\n")
    assert run(postings, decisions, profile=restricted)["kill"] == 1
    unrestricted = profile_at(directory, "filters:\n  enabled: []\n")
    assert run(postings, decisions, profile=unrestricted)["pass"] == 1


def test_remote_ma_keeps_its_posting_but_not_employers_empty_sites(tmp_path: Path) -> None:
    postings = tmp_path / "postings"
    decisions = tmp_path / "decisions"
    write_jsonl(postings / "2026-08-18.jsonl", [
        {"key": "syneos:remote-ma", "company": "Syneos Health", "title": "CRA", "location": "USA-MA-Remote"},
        {"key": "syneos:remote-co", "company": "Syneos Health", "title": "CRA", "location": "Remote Colorado"},
        {"key": "syneos:empty", "company": "Syneos Health", "title": "Safety specialist", "location": "", "locations": []},
        {"key": "other:physical", "company": "Local Lab", "title": "Technician", "location": "Boston, MA"},
        {"key": "other:empty", "company": "Local Lab", "title": "Technician II", "location": "", "locations": []},
    ])
    profile = profile_at(tmp_path / "profiles" / "local", "filters:\n  enabled: [location]\n  location:\n    regions: [MA, RI, NH, CT, VT, ME]\n")
    run(postings, decisions, profile=profile)
    rows = [json.loads(line) for path in decisions.glob("*.jsonl") for line in path.read_text().splitlines()]
    assert {row["posting_key"]: row["verdict"] for row in rows} == {
        "syneos:remote-ma": "pass", "syneos:remote-co": "kill", "syneos:empty": "kill",
        "other:physical": "pass", "other:empty": "pass",
    }


def profile_at(directory: Path, targeting: str = "") -> Profile:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "targeting.yaml").write_text(targeting, encoding="utf-8")
    return load_profile(directory)


def test_a_second_profile_is_refused_rather_than_interleaved(tmp_path: Path) -> None:
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(postings_dir / "2026-08-18.jsonl", [{"key": "p:1", "title": "One", "location": "Boston, MA"}])
    first = profile_at(tmp_path / "profiles" / "ada", "profile:\n  name: ada\n")
    second = profile_at(tmp_path / "profiles" / "grace", "profile:\n  name: grace\n")

    run(postings_dir, decisions_dir, profile=first)
    with pytest.raises(ValueError) as error:
        run(postings_dir, decisions_dir, profile=second)

    message = str(error.value)
    assert "holds Filter Decisions for Profile 'ada'" in message
    assert "--decisions-dir" in message


@pytest.mark.parametrize("arm", ["alarm", "worker"])
def test_a_pattern_that_backtracks_forever_fails_with_the_profile_named(
    tmp_path: Path, monkeypatch, arm: str
) -> None:
    """The whole stage, on both arms of the ceiling (`venator.match.budget`).

    `worker` is the arm a platform without `signal.setitimer` takes — Windows,
    and any stage driven from a thread that is not the main one. It is run here
    everywhere rather than only where it is the default, because the arm nobody
    exercises is the arm that turns out to yield and do nothing.
    """
    if arm == "alarm" and not alarm_can_be_armed():
        pytest.skip("this platform has no `signal.setitimer`; the worker arm is the only arm")
    monkeypatch.setattr("venator.match.budget.alarm_can_be_armed", lambda: arm == "alarm")
    postings_dir = tmp_path / "postings"
    write_jsonl(
        postings_dir / "2026-08-18.jsonl",
        [{"key": "p:hang", "title": "One", "description_html": "<p>" + "a" * 40 + "!</p>"}],
    )
    profile = profile_at(
        tmp_path / "profiles" / "typo",
        "filters:\n"
        "  enabled: [work_authorization]\n"
        "  work_authorization:\n"
        "    restriction_patterns: ['(a+)+$']\n",
    )
    monkeypatch.setattr("venator.match.run.FILTER_BUDGET_SECONDS", 0.5)

    with pytest.raises(TimeoutError) as error:
        run(postings_dir, tmp_path / "decisions", profile=profile)

    message = str(error.value)
    assert "'p:hang'" in message
    assert str(profile.targeting_path) in message
    # Nothing is appended: the run is aborted, not completed with a hole in it.
    assert list((tmp_path / "decisions").glob("*.jsonl")) == []
