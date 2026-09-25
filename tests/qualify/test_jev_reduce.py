"""Closed Jev reducer: exclusions, review flags, timing arithmetic and triage."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from venator.profile.loader import load_profile
from venator.qualify.jev_contract import validate_jev_response
from venator.qualify.jev_policy import prepare_jev_case
from venator.qualify.jev_questions import YEARS_OPTIONS
from venator.qualify.jev_reduce import (
    degree_exclusion_applicable,
    degree_rescue,
    experience_bound_usable,
    reduce_jev,
    timing_review_fires,
)
from venator.qualify.jev_release import JEV_RELEASE

ROOT = Path(__file__).resolve().parent / "fixtures" / "jev"


def _case():
    profile = load_profile(ROOT / "profile")
    posting = json.loads((ROOT / "posting.json").read_text(encoding="utf-8"))
    prepared = prepare_jev_case(posting, profile, "2026-09")
    assert prepared.case is not None
    return prepared.case


def _body(**noul_overrides: float) -> dict:
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    for key, value in noul_overrides.items():
        body["answers"][key]["noul"] = value
    return body


def _reduce(case, body: dict):
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return reduce_jev(case, validate_jev_response(case, raw))


def _result_dict(result) -> dict:
    return {
        "decision": result.decision,
        "fit_probability": result.fit_probability,
        "fit_score": result.fit_score,
        "primary_rule": result.primary_rule,
        "review_flags": list(result.review_flags),
        "diagnostic_flags": list(result.diagnostic_flags),
        "degree_timing_review": result.degree_timing_review,
        "experience_timing_review": result.experience_timing_review,
        "exclusions": [
            {
                "rule": item.rule,
                "basis": item.basis,
                "question_ids": list(item.question_ids),
                "probabilities": [[key, value] for key, value in item.probabilities],
                "guard_code": item.guard_code,
                "policy_value": item.policy_value,
                "policy_exclusion": item.policy_exclusion,
            }
            for item in result.exclusions
        ],
    }


def test_golden_triage_outcomes() -> None:
    case = _case()
    prioritize = _reduce(case, json.loads((ROOT / "valid_raw_response.json").read_text()))
    expected = json.loads((ROOT / "reduced_prioritize.json").read_text())
    assert _result_dict(prioritize) == expected

    review_body = _body(confirmed_requirement_gap=0.90, would_apply=0.40)
    review = _reduce(case, review_body)
    expected_review = json.loads((ROOT / "reduced_review.json").read_text())
    payload = _result_dict(review)
    payload["raw_variant"] = "mandatory_gap"
    assert payload == expected_review

    exclude = _reduce(case, _body(citizenship_or_clearance=0.95))
    expected_exclude = json.loads((ROOT / "reduced_exclude.json").read_text())
    payload = _result_dict(exclude)
    payload["raw_variant"] = "restricted_role"
    assert payload == expected_exclude
    assert exclude.exclusions[0].policy_exclusion is True

    invalid = validate_jev_response(case, (ROOT / "invalid_response.json").read_text())
    unassessed = reduce_jev(case, invalid)
    expected_unassessed = json.loads((ROOT / "reduced_unassessed.json").read_text())
    assert _result_dict(unassessed) == expected_unassessed
    assert unassessed.fit_probability is None
    assert unassessed.primary_rule is None


def test_thermo_style_nonstudent_sponsorship_is_review() -> None:
    case = _case()
    assert case.policy.no_sponsorship_nonstudent == "review"
    result = _reduce(case, _body(refuses_sponsorship=0.97, is_internship_or_coop=0.03))
    assert result.decision == "review"
    assert "sponsorship_review" in result.review_flags
    assert result.primary_rule is None
    excluded = replace(case, policy=replace(case.policy, no_sponsorship_nonstudent="exclude"))
    hard = _reduce(excluded, _body(refuses_sponsorship=0.97, is_internship_or_coop=0.03))
    assert hard.decision == "exclude"
    assert hard.primary_rule == "sponsorship_nonstudent"
    assert hard.exclusions[0].policy_exclusion is True


def test_thresholds_fire_on_equality() -> None:
    case = _case()
    assert _reduce(case, _body(citizenship_or_clearance=0.8)).primary_rule == "restricted_role"
    assert _reduce(case, _body(citizenship_or_clearance=0.799999)).primary_rule is None
    gap = _reduce(case, _body(confirmed_requirement_gap=0.85))
    assert "mandatory_technique_gap_review" in gap.review_flags
    below = _reduce(case, _body(confirmed_requirement_gap=0.849999))
    assert not any(flag.startswith("mandatory_") for flag in below.review_flags)
    student = _reduce(case, _body(refuses_sponsorship=0.8, is_internship_or_coop=0.3))
    assert "sponsorship_review" in student.review_flags
    band = _reduce(case, _body(refuses_sponsorship=0.8, is_internship_or_coop=0.3000001))
    assert "student_status_review" in band.review_flags
    retain = _reduce(case, _body(refuses_sponsorship=0.8, is_internship_or_coop=0.8))
    assert "sponsorship_note" in retain.diagnostic_flags
    assert retain.decision != "exclude"


def test_confirmed_gap_review_names_the_requirement_kind() -> None:
    case = _case()
    body = _body(confirmed_requirement_gap=0.9)
    choice = body["answers"]["mandatory_requirement_kind"]
    choice["choice"] = "certification"
    choice["probabilities"] = {
        "license": 0.01,
        "certification": 0.9,
        "language": 0.01,
        "technique": 0.01,
        "none": 0.02,
        "unknown": 0.05,
    }
    result = _reduce(case, body)
    assert "mandatory_certification_gap_review" in result.review_flags

    no_gap = _body(confirmed_requirement_gap=0.1)
    no_gap["answers"]["mandatory_requirement_kind"] = choice
    assert "mandatory_certification_gap_review" not in _reduce(case, no_gap).review_flags


def test_degree_applicability_rescue_and_timing_arithmetic() -> None:
    case = _case()
    assert degree_exclusion_applicable(case.compiled, case.bindings.education_refutable) is True
    assert timing_review_fires(case.bindings.start_clause_months, "2027-05") is True
    assert timing_review_fires(("2027-04",), "2027-05") is False
    present_before = replace(
        case, bindings=replace(case.bindings, start_clause_months=("2027-04",), graduation_clause_months=()),
    )
    high_degree = _body(degree_completed_required=0.9, is_internship_or_coop=0.1)
    deferred = _reduce(case, high_degree)
    assert "degree_timing_review" in deferred.review_flags
    assert deferred.degree_timing_review is True
    assert deferred.primary_rule is None
    killed = _reduce(present_before, high_degree)
    assert killed.primary_rule == "completed_degree"
    assert "degree_timing_clause" in killed.diagnostic_flags
    assert killed.degree_timing_review is False

    awarded = replace(case.compiled.education[0], status="awarded", degree_level="bachelor")
    with_award = replace(case, compiled=replace(case.compiled, education=(awarded,)))
    assert degree_exclusion_applicable(with_award.compiled, True) is False
    evidence_review = _reduce(replace(present_before, compiled=with_award.compiled), high_degree)
    assert "degree_evidence_review" in evidence_review.review_flags
    assert evidence_review.primary_rule is None

    atoms = tuple(atom for span in case.engine_evidence.spans for atom in span.atoms)
    assert degree_rescue(case.canonical, case.compiled, atoms) == "unknown"


def test_lower_bin_comparison_and_unknown_years() -> None:
    case = _case()
    assert case.bindings.occupancy_all == 8
    assert experience_bound_usable(
        case.compiled, occupancy_all=8, experience_refutable=True,
    ) is True
    body = _body()
    body["answers"]["years_floor"]["choice"] = "three_to_under_5"
    probs = {option: 0.0 for option in YEARS_OPTIONS}
    probs["three_to_under_5"] = 0.6
    probs["none"] = 0.4
    body["answers"]["years_floor"]["probabilities"] = probs
    timed_out = replace(
        case,
        bindings=replace(case.bindings, start_clause_months=(), graduation_clause_months=()),
    )
    floor = _reduce(timed_out, body)
    assert floor.primary_rule == "experience_floor"
    equal = replace(timed_out, bindings=replace(timed_out.bindings, occupancy_all=36))
    assert _reduce(equal, body).primary_rule is None
    unknown = _body()
    unknown["answers"]["years_floor"]["choice"] = "unknown"
    unknown_probs = {option: 0.0 for option in YEARS_OPTIONS}
    unknown_probs["unknown"] = 0.9
    unknown_probs["none"] = 0.1
    unknown["answers"]["years_floor"]["probabilities"] = unknown_probs
    assert _reduce(timed_out, unknown).primary_rule is None
    low = _body()
    low["answers"]["years_floor"]["choice"] = "eight_plus"
    low_probs = {option: 0.0 for option in YEARS_OPTIONS}
    low_probs["eight_plus"] = 0.599
    low_probs["none"] = 0.401
    low["answers"]["years_floor"]["probabilities"] = low_probs
    low_result = _reduce(case, low)
    assert "years_low_confidence" in low_result.diagnostic_flags
    assert low_result.primary_rule is None


def test_draft_experience_is_not_treated_as_zero() -> None:
    case = _case()
    draft = replace(case.compiled.experience[0], fact_status="draft")
    unusable = replace(
        case,
        compiled=replace(case.compiled, experience=(draft,)),
        bindings=replace(case.bindings, experience_refutable=False, occupancy_all=0),
    )
    body = _body()
    body["answers"]["years_floor"]["choice"] = "two_to_under_3"
    probs = {option: 0.0 for option in YEARS_OPTIONS}
    probs["two_to_under_3"] = 0.7
    probs["none"] = 0.3
    body["answers"]["years_floor"]["probabilities"] = probs
    result = _reduce(unusable, body)
    assert "experience_evidence_review" in result.review_flags
    assert result.primary_rule is None


def test_temporary_exclusion_stays_review_when_capability_is_off() -> None:
    case = _case()
    assert JEV_RELEASE.temporary_exclusion_enabled is False
    enabled_policy = replace(case, policy=replace(
        case.policy, temporary_student_authorization_exclusion="exclude",
    ))
    result = _reduce(enabled_policy, _body(temporary_authorization_excluded=0.95))
    assert "temporary_authorization_review" in result.review_flags
    assert result.primary_rule is None
    armed = replace(JEV_RELEASE, temporary_exclusion_enabled=True)
    live = reduce_jev(
        enabled_policy,
        validate_jev_response(case, json.dumps(_body(temporary_authorization_excluded=0.95),
                                               separators=(",", ":"))),
        armed,
    )
    assert live.primary_rule == "temporary_authorization_exclusion"


def test_domain_below_half_and_below_theta_are_review() -> None:
    case = _case()
    domain = _reduce(case, _body(domain_match=0.49, would_apply=0.9, plausible_candidate=0.9))
    assert "domain_review" in domain.review_flags
    assert domain.decision == "review"
    low = _reduce(case, _body(would_apply=0.1, plausible_candidate=0.1, domain_match=0.9,
                              confirmed_requirement_gap=0.0))
    assert "below_threshold" in low.review_flags
    assert low.decision == "review"
    assert low.fit_score < JEV_RELEASE.theta


def test_exclusion_beats_review_and_student_degree_path_is_not_a_kill() -> None:
    case = _case()
    both = _reduce(case, _body(citizenship_or_clearance=0.9, confirmed_requirement_gap=0.99))
    assert both.decision == "exclude"
    assert both.primary_rule == "restricted_role"
    student_degree = _reduce(case, _body(degree_completed_required=0.95, is_internship_or_coop=0.4))
    assert "degree_student_path_review" in student_degree.review_flags
    assert student_degree.primary_rule is None


def test_score_identity_notes_do_not_unassess() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    body["answers"]["level"]["score"] = 1.5
    result = _reduce(case, body)
    assert result.decision != "unassessed"
    assert "score_mismatch" in result.diagnostic_flags
    body["answers"]["level"]["probabilities"]["0"] = 0.74
    summed = _reduce(case, body)
    assert summed.decision != "unassessed"
    assert "sum_mismatch" in summed.diagnostic_flags
    assert "score_mismatch" in summed.diagnostic_flags


def test_sum_1_04_reduces_from_the_renormalised_distribution() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    exact = _reduce(case, body)
    body["answers"]["level"]["probabilities"]["0"] = 0.74
    drifted = _reduce(case, body)
    assert drifted.decision != "unassessed"
    scale = 1.04
    expected = (
        0.88 + 0.85 + 0.92 + (0.74 / scale) + (0.2 / scale)
    ) / 4.0
    assert drifted.fit_score == expected
    assert drifted.fit_score != exact.fit_score
    assert "sum_mismatch" in drifted.diagnostic_flags

    body["answers"]["level"]["probabilities"]["0"] = 0.76
    refused = _reduce(case, body)
    assert refused.decision == "unassessed"
    assert refused.fit_score is None

    body["answers"]["level"]["probabilities"]["0"] = 0.64
    also_refused = _reduce(case, body)
    assert also_refused.decision == "unassessed"
