"""Counterexamples for timing and search preferences."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from venator.match.filters import apply_filters, education_fit_reading, role_target_filter
from venator.match.timing import eligibility_finding, pay_range
from venator.profile import load_profile
from venator.profile.schema import EducationFitPolicy, FilterPolicy, SearchTargeting, TimingPolicy

REPOSITORY = Path(__file__).parents[2]
POLICY = load_profile(REPOSITORY / "profiles" / "example").filters


def posting(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "key": "source:board:counterexample",
        "title": "Laboratory Intern",
        "location": "Boston, MA",
        "description_html": "<p>Join a growing laboratory team.</p>",
    }
    value.update(changes)
    return value


def test_strict_experience_upper_bound_does_not_become_a_minimum() -> None:
    for limit in (2, 3):
        policy = FilterPolicy(education_fit=EducationFitPolicy(experience_years_kill=limit))
        assert education_fit_reading(posting(description_html="<2 years experience required"), policy).verdict == "pass"
        assert education_fit_reading(posting(description_html=f"{limit}+ years experience required"), policy).verdict == "kill"


def test_advanced_degree_requires_an_anchor_and_has_no_waiver() -> None:
    policy = FilterPolicy(education_fit=EducationFitPolicy(
        kill_completed_degree=True, current_student_level="undergraduate"
    ))
    for text in ("PhD required", "MS required", "Master's degree required", "MD/DO required", "PharmD required", "Currently pursuing an MS / MBA", "M.D. required", "MD degree required", "MD or DO required", "D.O. degree required", "Doctor of Medicine required", "Doctor of Osteopathic Medicine required", "MD required", "DO required", "Candidates must hold an MD degree", "DO degree is required"):
        assert education_fit_reading(posting(description_html=text), policy).verdict == "kill", text
    for text in ("PharmD track co-op opportunities", "PhD preferred", "PhD or equivalent experience required", "MD optional", "We do lab work."):
        assert education_fit_reading(posting(description_html=text), policy).verdict == "pass", text


@pytest.mark.parametrize("text", (
    "Frederick, MD - Required: 0-2 years lab experience.",
    "Based in Rockville, MD. Required skills: pipetting, cell culture.",
    "Our team in Baltimore, MD is hiring. Must be able to lift 25 lbs (required).",
    "DO NOT apply through agencies. Required: cell culture experience.",
    "Required: pipetting. Location: Bethesda, MD",
    "Baltimore MD required: pipetting.",
    "Location: Rockville, MD 20878. Required: lab work.",
    "Required: collaboration with MD/PA/NPs in multi-person care.",
    "Consults MD, when necessary, before care.",
    "Works with a varied workforce including MD’s, nurses, and administrative staff. Required: scheduling.",
))
def test_medical_degree_abbreviations_are_not_locations_or_verbs(text: str) -> None:
    policy = FilterPolicy(education_fit=EducationFitPolicy(
        kill_completed_degree=True, current_student_level="undergraduate"
    ))
    assert education_fit_reading(posting(description_html=text), policy).verdict == "pass"


def test_umbrella_coop_degree_tracks_do_not_require_a_pharmd() -> None:
    policy = FilterPolicy(education_fit=EducationFitPolicy(
        kill_completed_degree=True, current_student_level="undergraduate"
    ))
    listing = (
        "Currently enrolled at an accredited college or university pursuing a degree in:\n"
        "Bachelor's in Biology, Chemistry, Biotechnology, Biochemistry and Molecular Biology.\n"
        "or\nMaster's in Biology, Chemical Engineering and Bioinformatics.\n"
        "or\nPhD in Biology, Pharmaceutical Sciences, and Pharmacy (PharmD).\n"
        "Candidates must be eligible to work in the US."
    )
    assert education_fit_reading(posting(description_html=listing), policy).verdict == "pass"
    assert education_fit_reading(posting(description_html="PharmD required."), policy).verdict == "kill"


def test_expired_deadline_and_past_internship_cohort_are_kills() -> None:
    policy = TimingPolicy(reference_date=date(2026, 9, 4))

    expired = eligibility_finding(
        posting(application_deadline="2026-09-03"), policy
    )
    past = eligibility_finding(
        posting(title="Summer 2025 Internship", opportunity_type="internship"), policy
    )

    assert (expired.status, expired.fact) == ("kill", "expired")
    assert (past.status, past.fact) == ("kill", "past_cohort")


def test_summer_only_profile_rejects_spring_coop_but_accepts_summer_internship() -> None:
    policy = TimingPolicy(allowed_seasons=("summer",), reference_date=date(2026, 1, 1))

    spring = eligibility_finding(
        posting(title="Spring 2027 Co-op", opportunity_type="co-op"), policy
    )
    summer = eligibility_finding(
        posting(title="Summer 2027 Internship", opportunity_type="internship"), policy
    )

    assert (spring.status, spring.fact) == ("kill", "season_mismatch")
    assert summer.status == "pass"


def test_confirmed_graduation_must_be_before_a_degree_by_start_requirement() -> None:
    policy = TimingPolicy(
        expected_graduation=date(2028, 6, 20),
        expected_graduation_confirmed=True,
        reference_date=date(2026, 9, 4),
    )
    value = posting(
        title="Research Associate",
        role_start="2027-01-10",
        description_html="<p>Bachelor's degree required by role start.</p>",
    )

    finding = eligibility_finding(value, policy)

    assert (finding.status, finding.fact) == ("kill", "degree_after_start")


def test_current_and_future_enrollment_are_distinct() -> None:
    current = eligibility_finding(
        posting(description_html="<p>Must currently be enrolled in a degree program.</p>"),
        TimingPolicy(enrollment="future"),
    )
    future = eligibility_finding(
        posting(description_html="<p>Will be enrolled by the internship start.</p>"),
        TimingPolicy(enrollment="current"),
    )

    assert (current.status, current.fact) == ("kill", "enrollment_mismatch")
    assert (future.status, future.fact) == ("unknown", "enroll_timing")


def test_salary_floor_does_not_turn_missing_or_foreign_units_into_zero_or_fit() -> None:
    policy = SearchTargeting(
        salary_floor=70_000,
        salary_currency="USD",
        salary_period="year",
    )
    missing = apply_filters(
        posting(), FilterPolicy(enabled=("eligibility",), search=policy)
    )
    foreign = apply_filters(
        posting(salary={"minimum": 80_000, "currency": "EUR", "period": "year"}),
        FilterPolicy(enabled=("eligibility",), search=policy),
    )
    low = apply_filters(
        posting(salary={"maximum": 60_000, "currency": "USD", "period": "year"}),
        FilterPolicy(enabled=("eligibility",), search=policy),
    )

    assert (missing.verdict, missing.rule, missing.facts["eligibility"]) == (
        "pass",
        None,
        "salary_unknown",
    )
    assert (foreign.verdict, foreign.facts["eligibility"]) == ("pass", "salary_currency")
    assert (low.verdict, low.rule, low.reason) == ("kill", "eligibility", "stated salary ceiling 60000 is below the floor 70000 USD per year")
    assert pay_range(posting(salary={"minimum": 80_000, "currency": "EUR", "period": "year"})).currency == "EUR"


@pytest.mark.parametrize("start, expected", [("2027-04-01", "kill"), ("2027-07-01", "pass")])
def test_structured_start_enforces_season_without_a_named_cohort(start: str, expected: str) -> None:
    finding = eligibility_finding(
        posting(title="Research Associate", start_date=start),
        TimingPolicy(allowed_seasons=("summer",), reference_date=date(2026, 9, 4)),
    )
    assert finding.status == expected


@pytest.mark.parametrize("degree_requirement", ["", " Bachelor's degree required by role start."])
def test_unknown_dates_do_not_waive_explicit_enrollment_conflict(degree_requirement: str) -> None:
    finding = eligibility_finding(
        posting(description_html="Must currently be enrolled." + degree_requirement),
        TimingPolicy(
            allowed_seasons=("summer",),
            enrollment="future",
            expected_graduation=date(2028, 6, 1),
            expected_graduation_confirmed=True,
        ),
    )
    assert (finding.status, finding.fact) == ("kill", "enrollment_mismatch")


def test_minimum_only_salary_is_not_an_invented_ceiling() -> None:
    finding = eligibility_finding(
        posting(salary={"minimum": 60_000, "currency": "USD", "period": "year"}),
        TimingPolicy(), salary_floor=70_000, salary_currency="USD", salary_period="year",
    )
    assert (finding.status, finding.fact) == ("unknown", "salary_unknown")


def test_future_enrollment_phrase_is_not_a_current_enrollment_requirement() -> None:
    finding = eligibility_finding(
        posting(description_html="Must be enrolled by the internship start."),
        TimingPolicy(enrollment="current"),
    )
    assert (finding.status, finding.fact) == ("unknown", "enroll_timing")


def test_salary_floor_without_units_cannot_reject_hourly_pay() -> None:
    finding = eligibility_finding(
        posting(salary={"maximum": 100, "currency": "USD", "period": "hour"}),
        TimingPolicy(), salary_floor=70_000,
    )
    assert (finding.status, finding.fact) == ("unknown", "salary_unknown")


def test_unknown_country_code_is_not_an_invented_jurisdiction() -> None:
    from venator.countries import country_code
    assert country_code('ZZ') is None
    assert country_code({'alpha2Code': 'MY', 'descriptor': 'Malaysia'}) == 'MY'
    assert country_code('MYS') == 'MY'


def test_unconfirmed_student_level_does_not_invent_an_enrollment_conflict() -> None:
    from venator.match.filters import education_fit_reading
    from venator.profile.schema import EducationFitPolicy

    policy = FilterPolicy(education_fit=EducationFitPolicy(experience_years_kill=3))
    value = posting(description_html="<p>Currently pursuing a PhD in Chemistry.</p>")
    assert education_fit_reading(value, policy).fact == "enroll_unknown"
    value["description_html"] += "<p>5+ years of industry experience required.</p>"
    assert education_fit_reading(value, policy).verdict == "kill"


def test_current_student_as_alternative_to_recent_graduate_is_not_mandatory_enrollment() -> None:
    finding = eligibility_finding(
        posting(description_html="<p>Recent graduate (BS/MS) or current student in a related STEM field.</p>"),
        TimingPolicy(),
    )
    assert finding.status == "pass"
    assert finding.fact != "enroll_unknown"


def test_expected_graduation_alone_does_not_invent_current_enrollment(tmp_path: Path) -> None:
    (tmp_path / "resume.yaml").write_text("education:\n  - degree: Bachelor of Science\n    date: June 2028 (Expected)\n")
    profile = load_profile(tmp_path)
    assert profile.filters.education_fit.current_student_level is None
