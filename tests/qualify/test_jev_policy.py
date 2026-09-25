"""Jev policy loading, wire projection and I10-safe preparation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.match.store import NON_DECIDING_BLOCKS
from venator.profile.loader import ProfileError, load_profile
from venator.profile.schema import JEV_DOMAIN_TOKENS, SHIFT_PREFERENCES
from venator.qualify.jev_policy import prepare_jev_case, render_wire_policy
from venator.qualify.posting import POSTING_CHAR_CAP

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "jev"


def _write(directory: Path, **files: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / f"{name}.yaml").write_text(text, encoding="utf-8")
    return directory


def _targeting(extra: str) -> str:
    return f"""profile:
  name: tmp
  synthetic: true
filters:
  enabled: [education_fit]
  education_fit:
    in_progress: bachelor
{extra}
"""


def test_fixture_profile_loads_jev_and_shift_preference() -> None:
    profile = load_profile(FIXTURE / "profile")
    assert profile.filters.qualification_mode == "jev"
    assert profile.filters.shift_preference == "no_weekends"
    assert profile.filters.shift_preference in SHIFT_PREFERENCES
    policy = profile.filters.jev
    assert policy is not None
    assert policy.no_sponsorship_nonstudent == "review"
    assert policy.policy_version == "fictional-student-1"
    assert policy.domains == JEV_DOMAIN_TOKENS


def test_missing_jev_block_is_none_unless_mode_is_jev(tmp_path: Path) -> None:
    profile = load_profile(_write(tmp_path / "plain", resume="", constraints="", targeting=""))
    assert profile.filters.jev is None
    assert profile.filters.qualification_mode == "deterministic"
    assert profile.filters.shift_preference == "no_preference"
    targeting = "filters:\n  qualification_mode: jev\n"
    with pytest.raises(ProfileError, match="filters.jev is required"):
        load_profile(_write(tmp_path / "jev-missing", resume="", constraints="", targeting=targeting))


def test_unknown_jev_keys_and_incomplete_blocks_are_rejected(tmp_path: Path) -> None:
    unknown = _targeting(
        "  qualification_mode: jev\n"
        "  jev:\n"
        "    policy_version: v1\n"
        "    restricted_roles: exclude\n"
        "    temporary_student_authorization_exclusion: review\n"
        "    no_sponsorship_student: retain\n"
        "    no_sponsorship_nonstudent: review\n"
        "    unmet_completed_degree: exclude\n"
        "    domains: [biochemistry]\n"
        "    status: unrecognized value\n"
    )
    with pytest.raises(ProfileError, match="is not a Jev policy field"):
        load_profile(_write(tmp_path / "unknown", resume="", constraints="", targeting=unknown))
    incomplete = _targeting(
        "  qualification_mode: jev\n"
        "  jev:\n"
        "    policy_version: v1\n"
        "    restricted_roles: exclude\n"
    )
    with pytest.raises(ProfileError, match="is required when filters.jev is present"):
        load_profile(_write(tmp_path / "incomplete", resume="", constraints="", targeting=incomplete))


def test_shift_preference_is_closed(tmp_path: Path) -> None:
    targeting = "filters:\n  shift_preference: nights_only\n"
    with pytest.raises(ProfileError, match="shift_preference"):
        load_profile(_write(tmp_path / "shift", resume="", constraints="", targeting=targeting))


def test_wire_policy_omits_version_and_status() -> None:
    policy = load_profile(FIXTURE / "profile").filters.jev
    assert policy is not None
    wire = render_wire_policy(policy)
    assert "policy_version" not in wire
    assert "status" not in wire
    assert set(wire) == {
        "restricted_roles", "temporary_student_authorization_exclusion",
        "no_sponsorship_student", "no_sponsorship_nonstudent",
        "unmet_completed_degree", "domains",
    }
    assert list(wire["domains"]) == sorted(policy.domains)
    dumped = json.dumps(wire)
    assert "fictional-student-1" not in dumped
    assert "F-1" not in dumped
    assert "unrecognized value" not in dumped


def test_prepare_matches_golden_and_keeps_i10() -> None:
    profile = load_profile(FIXTURE / "profile")
    posting = json.loads((FIXTURE / "posting.json").read_text(encoding="utf-8"))
    prepared = prepare_jev_case(posting, profile, "2026-09")
    assert prepared.reason is None and prepared.case is not None
    golden = json.loads((FIXTURE / "prepared_state.json").read_text(encoding="utf-8"))
    state = dict(prepared.case.state)
    assert state == golden["state"]
    assert "policy_version" not in state["policy"]
    rendered = json.dumps(state)
    assert "Example Polytechnic" not in rendered
    assert "Example Robotics Lab" not in rendered
    assert prepared.case.bindings.occupancy_all == golden["occupancy_all"] == 8
    assert prepared.case.bindings.expected_graduation == "2029-05"
    assert prepared.case.bindings.start_clause_months == ("2029-06",)
    assert prepared.case.projection.education[0].gpa == "3.40"


def test_e7_hides_gpa_without_a_gpa_clause() -> None:
    profile = load_profile(FIXTURE / "profile")
    posting = json.loads((FIXTURE / "posting.json").read_text(encoding="utf-8"))
    posting["description_html"] = "<p>Summer internship for currently enrolled undergraduates.</p>"
    prepared = prepare_jev_case(posting, profile, "2026-09")
    assert prepared.case is not None
    assert prepared.case.projection.education[0].gpa is None
    assert prepared.case.projection.education[0].award_year is None
    assert prepared.case.engine_evidence.gpa_clause is None


def test_explicit_full_is_required_and_length_is_bounded() -> None:
    profile = load_profile(FIXTURE / "profile")
    posting = json.loads((FIXTURE / "posting.json").read_text(encoding="utf-8"))
    inferred = dict(posting)
    inferred.pop("description_kind")
    assert prepare_jev_case(inferred, profile, "2026-09").reason == "missing"
    snippet = dict(posting)
    snippet["description_kind"] = "snippet"
    assert prepare_jev_case(snippet, profile, "2026-09").reason == "snippet"
    empty = dict(posting)
    empty["description_html"] = "<p> </p>"
    assert prepare_jev_case(empty, profile, "2026-09").reason == "missing"
    huge = dict(posting)
    huge["description_html"] = "<p>" + ("word " * (POSTING_CHAR_CAP + 50)) + "</p>"
    assert prepare_jev_case(huge, profile, "2026-09").reason == "too_long"
    assert prepare_jev_case(posting, None, "2026-09").reason == "no_profile"


def test_jev_block_is_inside_the_filters_hash() -> None:
    targeting_exclusions = NON_DECIDING_BLOCKS[1]
    assert "filters.jev" not in targeting_exclusions
    assert "jev" not in targeting_exclusions
