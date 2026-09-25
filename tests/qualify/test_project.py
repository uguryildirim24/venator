"""Student projection is narrow, scrubbed, and clause-specific."""

from __future__ import annotations

import warnings
from dataclasses import fields
from pathlib import Path

import pytest

from venator.profile.loader import load_profile
from venator.qualify.compile import canonical_json, compile_profile, project, scrub_text
from venator.qualify.deny_terms import denied_class
from venator.qualify.schema import E7, StudentProjection

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "profiles"


@pytest.mark.parametrize("name", ["two-degrees", "non-science"])
def test_projection_and_visibility_golden(name: str) -> None:
    source = ROOT / "profiles" / "example" if name == "example" else FIXTURES / name
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compiled = compile_profile(load_profile(source), as_of_month="2026-09")
    projection, visibility = project(compiled, E7())
    assert isinstance(projection, StudentProjection)
    assert projection.schema == "qualify-profile-3"
    assert "history" not in {field.name for field in fields(StudentProjection)}
    assert "hist" not in compiled.facts_by_id
    golden = FIXTURES / name / "projection-golden.json"
    assert canonical_json({"projection": projection, "visibility": visibility}) + "\n" == golden.read_text(encoding="utf-8")
    rendered = canonical_json(projection)
    for secret in ("source_path", "profile_hash", "compiler_version", "North College",
                   "West University", "Acme Labs", "Cedar Works", "East School", "Civic Group",
                   "medical condition", "church worship", "Turkish", "2026-08", "2025-09"):
        assert secret not in rendered
    assert not any(denied_class(token) for token in rendered.split('"') if token and token != "[withheld]")
    assert {fact.id for fact in compiled.facts_by_id.values()} == set(visibility)
    assert {field.name for field in fields(StudentProjection)}.isdisjoint({"source_path", "profile_hash", "names", "awarded_on", "reference_date"})


def test_organisation_and_institution_embedded_names() -> None:
    compiled = compile_profile(load_profile(FIXTURES / "two-degrees"), as_of_month="2026-09")
    assert compiled.experience[0].bullets[0].text == "Built assays at Organisation A using PCR"
    assert scrub_text("BS in Biology at InstitutionName", {"institutionname": "Institution A"}) == (
        "BS in Biology at Institution A", None,
    )
    projection, _ = project(compiled, E7())
    assert "Acme Labs" not in canonical_json(projection)


def test_whole_bullet_and_role_withholding() -> None:
    compiled = compile_profile(load_profile(FIXTURES / "non-science"), as_of_month="2026-09")
    projection, visibility = project(compiled, E7())
    assert compiled.experience[1].role_withheld == "pronoun"
    assert compiled.facts_by_id["exp-02"].text == "[withheld]"
    assert projection.experience[1].role == "[withheld]"
    assert visibility["exp-02"] == "withheld"
    assert [bullet.text for bullet in projection.experience[0].bullets] == [
        "Built a scheduling workflow at Organisation A", "[withheld]", "[withheld]",
    ]
    assert visibility["exp-01-b2"] == "withheld"
    assert visibility["exp-01-b3"] == "withheld"
    assert projection.withheld_counts.bullets == 2
    assert projection.withheld_counts.other == 1


def test_gpa_and_award_year_only_for_matching_e7_clauses() -> None:
    compiled = compile_profile(load_profile(FIXTURES / "two-degrees"), as_of_month="2026-09")
    without, _ = project(compiled, E7())
    with_both, _ = project(compiled, E7(recency_clause="req-01", gpa_clause="req-02"))
    assert without.education[0].gpa is None
    assert without.education[0].award_year is None
    assert with_both.education[0].gpa == "3.91"
    assert with_both.education[0].award_year == "2022"
    assert with_both.education[1].award_year is None


def test_whole_word_deny_terms() -> None:
    assert denied_class("her experiments") == "pronoun"
    assert denied_class("herbal extracts") is None
    assert denied_class("church project") == "protected_term"
    assert denied_class("research") is None


@pytest.mark.parametrize("gpa", ["3.9 she", "3.9 at Acme Labs", "3.9, born 2000", "2024-05-01"])
def test_e7_gpa_does_not_bypass_student_text_boundary(gpa: str) -> None:
    from dataclasses import replace
    profile = load_profile(FIXTURES / "two-degrees")
    education = [{**profile.resume["education"][0], "gpa": gpa}, profile.resume["education"][1]]
    with pytest.warns(UserWarning, match="GPA"):
        compiled = compile_profile(replace(profile, resume={**profile.resume, "education": education}), as_of_month="2026-09")
    projection, _ = project(compiled, E7(gpa_clause="req-01"))
    assert projection.education[0].gpa is None
    assert gpa not in canonical_json(projection)


def test_name_replacement_does_not_rewrite_inserted_placeholders() -> None:
    assert scrub_text("Acme Labs and Institution", {
        "acme labs": "Institution A", "institution": "Organisation A",
    }) == ("Institution A and Organisation A", None)


def test_projection_excludes_private_fields_even_under_e7() -> None:
    from dataclasses import replace
    profile = load_profile(FIXTURES / "two-degrees")
    resume = {**profile.resume, "name": "Private Applicant", "contact": {"email": "private@example.test"},
              "memberships": ["Private Society"]}
    constraints = {**profile.constraints, "work_authorization": {
        "status": "private immigration status", "authorized_to_work": True,
    }}
    compiled = compile_profile(replace(profile, resume=resume, constraints=constraints), as_of_month="2026-09")
    projection, _ = project(compiled, E7(gpa_clause="req-01", recency_clause="req-02"))
    rendered = canonical_json(projection)
    for forbidden in ("Private Applicant", "private@example.test", "Private Society", "private immigration status",
                      "reference_date", "interval", "awarded_on", "source_path", "2022-06", "2023-01", "2024-03"):
        assert forbidden not in rendered


def test_unicode_name_matching_uses_the_matched_alternative() -> None:
    assert scrub_text("Built assays at ı Labs", {"i labs": "Organisation A"}) == (
        "Built assays at Organisation A", None,
    )
