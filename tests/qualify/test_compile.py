"""WP1 compiler contract, including private facts and stable identities."""

from __future__ import annotations

import hashlib
import json
import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from venator.profile.loader import load_profile
from venator.profile.schema import Profile
from venator.qualify.compile import ProfileInconsistent, _degree, _interval, canonical_json, compile_profile
from venator.qualify.schema import Evidence, Segment

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
MONTH = "2026-09"


def loaded(name: str) -> Profile:
    return load_profile(ROOT / "profiles" / "example" if name == "example" else FIXTURES / name)


@pytest.mark.parametrize("name", ["two-degrees", "non-science"])
def test_compiled_golden_and_hash(name: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compiled = compile_profile(loaded(name), as_of_month=MONTH)
    golden = FIXTURES / name / "compiled-golden.json"
    assert canonical_json(compiled) + "\n" == golden.read_text(encoding="utf-8")
    blank = replace(compiled, profile_hash="")
    assert compiled.profile_hash == hashlib.sha256(canonical_json(blank).encode()).hexdigest()[:12]
    assert set(compiled.facts_by_id) == {fact.id for fact in compiled.facts_by_id.values()}
    assert compiled.facts_by_id["auth"].kind == "auth"
    assert compiled.facts_by_id["avail"].kind == "avail"
    assert compiled.facts_by_id["edupol"].kind == "edupol"
    assert "hist" not in compiled.facts_by_id
    assert compiled.schema == "qualify-profile-3"


def test_two_degrees_timing_is_per_entry_and_policy_consistent() -> None:
    compiled = compile_profile(loaded("two-degrees"), as_of_month=MONTH)
    assert [(row.status, row.expected_completion, row.awarded_on) for row in compiled.education] == [
        ("awarded", None, "2022-06"), ("in_progress", "2027-05", None),
    ]
    assert [row.institution_placeholder for row in compiled.education] == ["Institution A", "Institution B"]
    assert compiled.experience[0].organization_placeholder == "Organisation A"
    assert compiled.skills[0].evidence_level == "bullet"
    assert compiled.skills[0].supporting_ids == ("exp-01-b1",)
    assert compiled.synthetic is True


def test_multiple_progressing_degrees_do_not_receive_global_timing() -> None:
    profile = loaded("two-degrees")
    resume = dict(profile.resume)
    resume["education"] = [*resume["education"], {
        "org": "South University", "degree": "PhD in Chemistry", "date": "June 2028 (Expected)",
    }]
    with pytest.warns(UserWarning, match="exactly one"):
        compiled = compile_profile(replace(profile, resume=resume), as_of_month=MONTH)
    assert [row.expected_completion for row in compiled.education] == [None, "2027-05", "2028-06"]


def test_withheld_parent_overrides_confirmed_children() -> None:
    profile = loaded("non-science")
    resume = dict(profile.resume)
    resume["confirmed"] = False
    resume["education"] = [{**resume["education"][0], "confirmed": True}]
    resume["experience"] = [{**resume["experience"][0], "confirmed": True,
                              "bullets": [{"text": "Built scheduling software", "confirmed": True}]}]
    resume["skills"] = {"items": "Scheduling", "confirmed": True}
    resume["certifications"] = {"items": "First Aid", "confirmed": True}
    compiled = compile_profile(replace(profile, resume=resume), as_of_month=MONTH)
    for kind in ("education", "experience", "bullet", "skill", "credential"):
        assert all(fact.fact_status == "draft" for fact in compiled.facts_by_id.values() if fact.kind == kind)


def test_withheld_section_overrides_confirmed_child() -> None:
    profile = loaded("non-science")
    resume = dict(profile.resume)
    resume["experience"] = {"confirmed": False, "entries": [{
        "org": "Cedar Works", "role": "Analyst", "dates": "January 2024 - Present",
        "confirmed": True, "bullets": [{"text": "Built reports", "confirmed": True}],
    }]}
    compiled = compile_profile(replace(profile, resume=resume), as_of_month=MONTH)
    assert compiled.facts_by_id["exp-01"].fact_status == "draft"
    assert compiled.facts_by_id["exp-01-b1"].fact_status == "draft"


def test_draft_degree_stays_draft() -> None:
    profile = loaded("non-science")
    resume = dict(profile.resume)
    resume["education"] = [{**resume["education"][0], "draft": True}]
    compiled = compile_profile(replace(profile, resume=resume), as_of_month=MONTH)
    assert compiled.education[0].status == "awarded"
    assert compiled.facts_by_id["edu-01"].fact_status == "draft"


@pytest.mark.parametrize("policy,education", [
    ({"kill_completed_degree": True}, [{"degree": "BS in Biology", "date": "May 2024"}]),
    ({"holds": "master"}, [{"degree": "BS in Biology", "date": "May 2024"}]),
])
def test_inconsistent_education_refused(policy: dict, education: list[dict]) -> None:
    profile = loaded("non-science")
    filters = replace(profile.filters, education_fit=replace(profile.filters.education_fit, **policy))
    resume = {"education": education}
    with pytest.raises(ProfileInconsistent):
        compile_profile(replace(profile, filters=filters, resume=resume), as_of_month=MONTH)


def test_scaffold_refused() -> None:
    with pytest.raises(ProfileInconsistent, match="scaffold"):
        compile_profile(replace(loaded("non-science"), scaffold=True), as_of_month=MONTH)


@pytest.mark.parametrize("bullet", [
    "Gained exposure to PCR", "Practiced PCR in introductory coursework",
    "No experience with PCR", "Observed PCR",
])
def test_skill_evidence_is_presence_only(bullet: str) -> None:
    profile = loaded("non-science")
    resume = dict(profile.resume)
    resume["experience"] = [{"org": "Cedar Works", "role": "Analyst", "dates": "January 2024 - Present", "bullets": [bullet]}]
    resume["skills"] = "PCR"
    compiled = compile_profile(replace(profile, resume=resume), as_of_month=MONTH)
    assert compiled.skills[0].name == "PCR"
    assert compiled.skills[0].evidence_level == "bullet"
    assert compiled.skills[0].supporting_ids == ("exp-01-b1",)


def test_coursework_evidence_and_no_unconfirmed_bullet_support() -> None:
    profile = loaded("non-science")
    resume = dict(profile.resume)
    resume["education"] = [{**resume["education"][0], "coursework": "PCR methods"}]
    resume["experience"] = [{"org": "Cedar Works", "role": "Analyst", "dates": "January 2024 - Present",
                             "draft": True, "bullets": ["Used PCR"]}]
    resume["skills"] = "PCR"
    compiled = compile_profile(replace(profile, resume=resume), as_of_month=MONTH)
    assert compiled.skills[0].evidence_level == "coursework"
    assert compiled.skills[0].supporting_ids == ()


@pytest.mark.parametrize("text,level", [
    ("High School Diploma", "high_school"), ("H.S. Diploma", "high_school"),
    ("GED", "high_school"), ("Associate in Science", "associate"),
    ("A.A.S. in Nursing", "associate"), ("B.S. in Biology", "bachelor"),
    ("B.Sc. in Chemistry", "bachelor"), ("B.Tech in Computing", "bachelor"),
    ("M.S. in Physics", "master"), ("M.Sc. in Chemistry", "master"),
    ("M.B.A.", "master"), ("Ph.D. in Biology", "doctorate"),
    ("PharmD in Pharmacy", "doctorate"), ("Certificate in Design", "unknown"),
    ("MS and BS", "master"),
])
def test_degree_aliases_first_in_reading_order(text: str, level: str) -> None:
    assert _degree(text) == level


def test_unsupported_shapes_are_preserved_with_reasons() -> None:
    profile = loaded("non-science")
    resume = dict(profile.resume)
    resume["education"] = [42]
    resume["experience"] = ["improper entry"]
    with pytest.warns(UserWarning):
        compiled = compile_profile(replace(profile, resume=resume), as_of_month=MONTH)
    assert compiled.education[0].degree_level == "unknown"
    assert compiled.education[0].unknown_reason == "unsupported education entry shape"
    assert compiled.experience[0].kind == "unknown"
    assert compiled.experience[0].interval is None
    assert compiled.experience[0].unknown_reason == "unsupported experience entry shape"


@pytest.mark.parametrize("source,start,end,current", [
    ("August 2026 - Present", "2026-08", None, True),
    ("February 2026 - May 2026", "2026-02", "2026-05", False),
    ("June 2025 - August 2025", "2025-06", "2025-08", False),
    ("November 2023 - August 2024", "2023-11", "2024-08", False),
    ("September 2025 - Present", "2025-09", None, True),
])
def test_profile_interval_shapes(source: str, start: str, end: str | None, current: bool) -> None:
    interval, reason = _interval(source)
    assert reason is None
    assert interval is not None
    assert (interval.start, interval.end, interval.is_current) == (start, end, current)


@pytest.mark.parametrize("source", [
    "", "2025", "2025-01 - 2025-02", "Jan 2025 - Feb 2025", "January 2025",
    "January 2025 -", "- March 2025", "January 2025 - March", "January 2025 - April 2024",
    "January 2025 - Yesterday", "January 2025 - June 2025 extra", "extra January 2025 - June 2025",
    "January 2025/June 2025", "January 2025 to", "2025-01-01 to 2025-02-01",
    "January 2025 - February 2025 - March 2025", "January 2025 - infinity",
    "January 2025 - 2025", "Foo 2025 - May 2025", "January 2025 - Foo 2025",
])
def test_twenty_adversarial_interval_shapes_warn_as_unknown(source: str) -> None:
    interval, reason = _interval(source)
    assert interval is None
    assert reason is not None


def test_evidence_guaranteed_uses_union_segments() -> None:
    evidence = Evidence((Segment("seg-01", 2, frozenset({"exp-01", "exp-02"}), False),
                         Segment("seg-02", 3, frozenset({"exp-02"}), True)),
                        {}, 5, frozenset(), frozenset(), "2026-09", "as_of", True, True)
    assert evidence.guaranteed(["exp-01", "exp-02"]) == 5
    assert evidence.guaranteed(["exp-01"]) == 2


def test_optional_metadata_is_loaded_without_rule_tier_fields() -> None:
    profile = loaded("two-degrees")
    assert not hasattr(profile, "synthetic")
    assert profile.targeting["profile"]["synthetic"] is True
    compiled = compile_profile(profile, as_of_month=MONTH)
    assert "hist" not in compiled.facts_by_id
    assert compiled.synthetic is True


@pytest.mark.parametrize("value", [None, "true", 1, [], {}, {"experience": True}])
def test_removed_completeness_refused_by_compiler(value: object) -> None:
    profile = loaded("non-science")
    with pytest.raises(ProfileInconsistent, match="SPEC §13 item 48"):
        compile_profile(replace(profile, resume={**profile.resume, "completeness": value}), as_of_month=MONTH)


@pytest.mark.parametrize("value", ["true", 1, [], {}])
def test_invalid_synthetic_refused_by_compiler(value: object) -> None:
    profile = loaded("non-science")
    targeting = {**profile.targeting, "profile": {"synthetic": value}}
    with pytest.raises(ProfileInconsistent, match="targeting.profile.synthetic"):
        compile_profile(replace(profile, targeting=targeting), as_of_month=MONTH)


@pytest.mark.parametrize("source,expected", [
    ("BA of History at InstitutionName", "History"),
    ("BS in Biology at InstitutionName", "Biology"),
    ("Bachelor of Science, Biology", None),
    ("BS in Chemistry/Biology (Honors)", "Chemistry/Biology"),
    ("BA, History", None),
    ("Bachelor of Science in Biochemistry", "Biochemistry"),
    ("Bachelor of Arts in History of Art", "History of Art"),
    ("Master of Business Administration", "Business Administration"),
    ("Bachelor of Biochemistry", "Biochemistry"),
    ("Bachelor of Science", None),
    ("PhD in Chemistry", "Chemistry"),
    ("Bachelor of Arts", None),
    ("Bachelor of Engineering", None),
    ("PhD of Philosophy", None),
    ("Bachelor of Fine Arts", None),
    ("Bachelor of Laws", None),
    ("Bachelor of Medicine", None),
    ("Bachelor of Business", None),
])
def test_field_extraction_contract(source: str, expected: str | None) -> None:
    profile = loaded("non-science")
    resume = {**profile.resume, "education": [{"degree": source, "org": "InstitutionName", "date": "May 2024"}]}
    compiled = compile_profile(replace(profile, resume=resume), as_of_month=MONTH)
    assert compiled.education[0].field_of_study == expected


@pytest.mark.parametrize("field,value", [("degree", "BA in History"), ("date", "May 2024")])
def test_draft_leaf_mapping_cannot_supply_confirmed_degree(field: str, value: str) -> None:
    profile = loaded("non-science")
    row = {**profile.resume["education"][0], field: {"text": value, "confirmed": False}}
    compiled = compile_profile(replace(profile, resume={**profile.resume, "education": [row]}), as_of_month=MONTH)
    assert compiled.facts_by_id["edu-01"].fact_status == "draft"


def test_native_yaml_education_date_and_numeric_gpa() -> None:
    from datetime import date
    profile = loaded("non-science")
    row = {**profile.resume["education"][0], "date": date(2024, 5, 15), "gpa": 3.91}
    compiled = compile_profile(replace(profile, resume={**profile.resume, "education": [row]}), as_of_month=MONTH)
    assert compiled.education[0].awarded_on == "2024-05"
    assert compiled.education[0].gpa == "3.91"


def test_draft_timing_cannot_confirm_degree_override() -> None:
    profile = loaded("two-degrees")
    timing = {**profile.targeting["search"]["timing"], "confirmed": False}
    targeting = {**profile.targeting, "search": {"timing": timing}}
    compiled = compile_profile(replace(profile, targeting=targeting), as_of_month=MONTH)
    assert compiled.education[1].fact_status == "draft"
    assert compiled.availability.fact_status == "draft"


@pytest.mark.parametrize("entries,month,reference,expected", [
    (["January 2025 - January 2027"], "2025-03", None, [(1, 3)]),
    (["September 2026 - Present"], "2026-09", None, [(0, 1)]),
    (["September 2025 - Present"], "2026-09", "2026-03", [(5, 7)]),
    (["November 2026 - May 2027"], "2026-09", None, [(0, 0)]),
    (["January 2025 - January 2026"], "2026-02", None, [(11, 13)]),
    (["January 2025 - June 2025", "June 2025 - December 2025"], "2026-09", None, [(4, 6), (5, 7)]),
    (["September 2026 - Present"], "2026-09", None, [(0, 1)]),
], ids=["G1", "G2", "G3", "G4", "G5", "G6", "G7"])
def test_compiler_e3_named_cases(entries: list[str], month: str, reference: str | None,
                                 expected: list[tuple[int, int]]) -> None:
    from datetime import date
    profile = loaded("non-science")
    timing = replace(profile.search.timing, reference_date=date.fromisoformat(reference + "-01") if reference else None)
    resume = {**profile.resume, "experience": [{"role": "Analyst", "dates": entry} for entry in entries]}
    compiled = compile_profile(replace(profile, resume=resume, search=replace(profile.search, timing=timing)), as_of_month=month)
    assert [(row.guaranteed_months, row.months) for row in compiled.experience] == expected


def test_same_month_days_and_next_month_boundary() -> None:
    profile = loaded("non-science")
    resume = {**profile.resume, "experience": [{"role": "Analyst", "dates": "September 2026 - Present"}]}
    profile = replace(profile, resume=resume)
    pair = [compile_profile(profile, as_of_month=day[:7]) for day in ("2026-09-29", "2026-09-30")]
    assert canonical_json(pair[0]) == canonical_json(pair[1])
    following = compile_profile(profile, as_of_month="2026-10")
    assert (following.experience[0].guaranteed_months, following.experience[0].months) == (0, 2)
    assert following.profile_hash != pair[0].profile_hash


@pytest.mark.parametrize("alias,level", [(alias, level) for level, aliases in (
    ("high_school", ("high school", "HS diploma", "GED", "secondary")),
    ("associate", ("associate", "AA", "AS", "AAS")),
    ("bachelor", ("bachelor", "BS", "BSc", "BA", "BEng", "BE", "BBA", "BFA", "B.Tech")),
    ("master", ("master", "MS", "MSc", "MA", "MEng", "MBA", "MPH", "M.Tech")),
    ("doctorate", ("PhD", "doctorate", "doctoral", "DPhil", "MD", "JD", "PharmD", "DDS", "DVM", "EdD")),
) for alias in aliases])
def test_complete_degree_alias_ladder(alias: str, level: str) -> None:
    assert _degree(alias) == level
