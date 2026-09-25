"""Closed Jev exclusion rules, review flags and triage precedence."""

from __future__ import annotations

import re
from typing import Literal

from venator.qualify.engine import _ordinal
from venator.qualify.jev_contract import (
    DIAGNOSTIC_CODES,
    JevExclusion,
    JevResult,
    JevValidation,
    ValidatedJevResponse,
)
from venator.qualify.jev_policy import PreparedJevCase
from venator.qualify.jev_questions import YEARS_LOWER_EDGE
from venator.qualify.jev_release import JEV_RELEASE, JevRelease
from venator.qualify.posting import Atom, CanonicalPosting
from venator.qualify.schema import CompiledProfile, StudentProjection

_RANK = {"high_school": 0, "associate": 1, "bachelor": 2, "master": 3, "doctorate": 4}
_BACHELOR_PLUS = frozenset({"bachelor", "master", "doctorate"})
_WORD = re.compile(r"[a-z]+")

REVIEW_FLAG_ORDER: tuple[str, ...] = (
    "restricted_role_review",
    "temporary_authorization_review",
    "sponsorship_review",
    "student_status_review",
    "degree_student_path_review",
    "degree_evidence_review",
    "degree_rescue_review",
    "degree_timing_review",
    "completed_degree_review",
    "experience_evidence_review",
    "experience_timing_review",
    "mandatory_license_gap_review",
    "mandatory_certification_gap_review",
    "mandatory_language_gap_review",
    "mandatory_technique_gap_review",
    "mandatory_unknown_gap_review",
    "domain_review",
    "profile_evidence_review",
    "below_threshold",
)
DIAGNOSTIC_FLAG_ORDER: tuple[str, ...] = (
    "sponsorship_note",
    "low_score_confidence",
    "years_low_confidence",
    "degree_timing_clause",
    "experience_timing_clause",
    "score_mismatch",
    "sum_mismatch",
)


def _sort_flags(flags: set[str], order: tuple[str, ...]) -> tuple[str, ...]:
    rank = {name: index for index, name in enumerate(order)}
    return tuple(sorted(flags, key=lambda name: (rank.get(name, len(order)), name)))


def _pair(question_id: str, value: float) -> tuple[tuple[str, float], ...]:
    return ((question_id, value),)


def _exclusion(
    rule: str,
    question_ids: tuple[str, ...],
    probabilities: tuple[tuple[str, float], ...],
    guard_code: str,
    policy_value: str | None,
    policy_exclusion: bool,
) -> JevExclusion:
    return JevExclusion(
        rule=rule,
        basis="model_assisted_policy",
        question_ids=question_ids,
        probabilities=probabilities,
        guard_code=guard_code,
        policy_value=policy_value,
        policy_exclusion=policy_exclusion,
    )


def degree_exclusion_applicable(compiled: CompiledProfile, education_refutable: bool) -> bool:
    """High degree-required signal may exclude only an in-progress bachelor's Owner."""
    if not education_refutable:
        return False
    awarded_plus = any(
        row.fact_status == "confirmed"
        and not row.withheld
        and row.status == "awarded"
        and row.degree_level in _BACHELOR_PLUS
        for row in compiled.education
    )
    if awarded_plus:
        return False
    return any(
        row.fact_status == "confirmed"
        and not row.withheld
        and row.status == "in_progress"
        and row.degree_level == "bachelor"
        for row in compiled.education
    )


def experience_bound_usable(compiled: CompiledProfile, *, occupancy_all: int,
                            experience_refutable: bool) -> bool:
    if not compiled.experience:
        return False
    if not experience_refutable:
        return False
    return occupancy_all >= 0


def timing_review_fires(clause_months: tuple[str, ...], expected_graduation: str | None) -> bool:
    """Fire only when a parsed clause month is on or after expected graduation."""
    if expected_graduation is None or not clause_months:
        return False
    expected = expected_graduation[:7]
    _ordinal(expected)
    for month in clause_months:
        if _ordinal(month) >= _ordinal(expected):
            return True
    return False


def degree_rescue(
    canonical: CanonicalPosting,
    compiled: CompiledProfile,
    atoms: tuple[Atom, ...],
) -> Literal["met", "unknown"]:
    """Report met only for understood mandatory awarded-degree requirements.

    Rank or one shared field word is not field equivalence. Unresolved
    ``or equivalent`` branches are unknown. ``unknown`` does not disable an
    otherwise applicable exclusion.
    """
    del canonical
    mandatory = tuple(
        atom for atom in atoms
        if atom.kind == "degree"
        and atom.modality_hint == "required"
        and atom.attainment == "awarded"
        and atom.required_level in _RANK
    )
    if not mandatory:
        return "unknown"
    awarded = tuple(
        row for row in compiled.education
        if row.fact_status == "confirmed"
        and not row.withheld
        and row.status == "awarded"
        and row.degree_level in _RANK
    )
    if not awarded:
        return "unknown"
    for atom in mandatory:
        if atom.or_equivalent:
            return "unknown"
        target = _RANK[atom.required_level]
        matches = tuple(row for row in awarded if _RANK[row.degree_level] >= target)
        if not matches:
            return "unknown"
        required = {word.casefold() for word in atom.field_words}
        if not required:
            continue
        matched_field = False
        for row in matches:
            have = set(_WORD.findall((row.field_of_study or "").casefold()))
            if len(required & have) >= 2:
                matched_field = True
                break
        if not matched_field:
            return "unknown"
    return "met"


def _substantive_confirmed(projection: StudentProjection) -> bool:
    for row in projection.education:
        if row.fact_status == "confirmed" and row.degree_level != "unknown":
            return True
    for row in projection.experience:
        if row.fact_status == "confirmed" and row.role != "[withheld]":
            return True
    for row in (*projection.skills, *projection.coursework, *projection.credentials):
        if row.fact_status == "confirmed" and row.name:
            return True
    return False


def _degree_atoms(case: PreparedJevCase) -> tuple[Atom, ...]:
    return tuple(atom for span in case.engine_evidence.spans for atom in span.atoms)


def reduce_jev(
    prepared: PreparedJevCase | None,
    validation: JevValidation,
    release: JevRelease | None = None,
) -> JevResult:
    """Combine validated probabilities with local arithmetic and Owner policy."""
    packaged = release or JEV_RELEASE
    empty = JevResult("unassessed", None, None, (), None, (), (), False, False)
    if prepared is None or not validation.passed or validation.response is None:
        return empty
    response: ValidatedJevResponse = validation.response
    policy = prepared.policy
    compiled = prepared.compiled
    bindings = prepared.bindings
    p_student = response.noul("is_internship_or_coop")
    p_sponsor = response.noul("refuses_sponsorship")
    p_restrict = response.noul("citizenship_or_clearance")
    p_temp = response.noul("temporary_authorization_excluded")
    p_degree = response.noul("degree_completed_required")
    p_domain = response.noul("domain_match")
    p_gap = response.noul("confirmed_requirement_gap")
    p_plausible = response.noul("plausible_candidate")
    p_would = response.noul("would_apply")
    level0, level1 = response.level.probabilities[0], response.level.probabilities[1]
    fit_probability = p_would
    fit_score = (p_plausible + p_would + p_domain + level0 + level1) / 4.0

    exclusions: list[JevExclusion] = []
    review: set[str] = set()
    diagnostic: set[str] = set()
    for code in validation.error_codes:
        if code in DIAGNOSTIC_CODES:
            diagnostic.add(code)
    high = packaged.restriction_threshold
    clause_months = bindings.start_clause_months + bindings.graduation_clause_months
    degree_timing = timing_review_fires(clause_months, bindings.expected_graduation)
    experience_timing = timing_review_fires(clause_months, bindings.expected_graduation)
    if clause_months and not degree_timing:
        diagnostic.add("degree_timing_clause")
    if clause_months and not experience_timing:
        diagnostic.add("experience_timing_clause")

    if p_restrict >= high:
        if policy.restricted_roles == "exclude":
            exclusions.append(_exclusion(
                "restricted_role", ("citizenship_or_clearance",),
                _pair("citizenship_or_clearance", p_restrict),
                "fired", policy.restricted_roles, True,
            ))
        else:
            review.add("restricted_role_review")

    if p_temp >= high:
        review.add("temporary_authorization_review")
        if (policy.temporary_student_authorization_exclusion == "exclude"
                and packaged.temporary_exclusion_enabled):
            exclusions.append(_exclusion(
                "temporary_authorization_exclusion",
                ("temporary_authorization_excluded",),
                _pair("temporary_authorization_excluded", p_temp),
                "fired", policy.temporary_student_authorization_exclusion, True,
            ))
            review.discard("temporary_authorization_review")

    if p_sponsor >= high:
        if p_student <= packaged.student_no_threshold:
            if policy.no_sponsorship_nonstudent == "exclude":
                exclusions.append(_exclusion(
                    "sponsorship_nonstudent",
                    ("refuses_sponsorship", "is_internship_or_coop"),
                    (("refuses_sponsorship", p_sponsor), ("is_internship_or_coop", p_student)),
                    "student_no", policy.no_sponsorship_nonstudent, True,
                ))
            else:
                review.add("sponsorship_review")
        elif p_student >= packaged.student_yes_threshold:
            if policy.no_sponsorship_student == "retain":
                diagnostic.add("sponsorship_note")
            else:
                review.add("sponsorship_review")
        else:
            review.add("student_status_review")

    if p_degree >= high:
        if p_student > packaged.student_no_threshold:
            review.add("degree_student_path_review")
        elif not degree_exclusion_applicable(compiled, bindings.education_refutable):
            review.add("degree_evidence_review")
        elif degree_timing:
            review.add("degree_timing_review")
        elif degree_rescue(prepared.canonical, compiled, _degree_atoms(prepared)) == "met":
            review.add("degree_rescue_review")
        elif policy.unmet_completed_degree == "exclude":
            exclusions.append(_exclusion(
                "completed_degree",
                ("degree_completed_required", "is_internship_or_coop"),
                (("degree_completed_required", p_degree), ("is_internship_or_coop", p_student)),
                "fired", policy.unmet_completed_degree, True,
            ))
        else:
            review.add("completed_degree_review")

    years = response.years_floor
    years_prob = dict(years.probabilities)[years.choice]
    lower = YEARS_LOWER_EDGE[years.choice]
    if years.choice in {"none", "unknown"}:
        pass
    elif years_prob < packaged.years_choice_threshold:
        diagnostic.add("years_low_confidence")
    elif not experience_bound_usable(
        compiled, occupancy_all=bindings.occupancy_all,
        experience_refutable=bindings.experience_refutable,
    ):
        review.add("experience_evidence_review")
    elif experience_timing:
        review.add("experience_timing_review")
    elif lower is not None and lower > bindings.occupancy_all:
        exclusions.append(_exclusion(
            "experience_floor",
            ("years_floor",),
            (("years_floor", years_prob),),
            "lower_edge", None, False,
        ))

    if p_gap >= packaged.confirmed_gap_threshold:
        kind = response.mandatory_requirement_kind.choice
        if kind not in {"license", "certification", "language", "technique"}:
            kind = "unknown"
        review.add(f"mandatory_{kind}_gap_review")
    if p_domain < packaged.domain_review_threshold:
        review.add("domain_review")
    if not _substantive_confirmed(prepared.projection):
        review.add("profile_evidence_review")
    if fit_score < packaged.theta:
        review.add("below_threshold")
    if response.level.confidence < packaged.score_confidence_threshold:
        diagnostic.add("low_score_confidence")

    primary = exclusions[0].rule if exclusions else None
    review_flags = _sort_flags(review, REVIEW_FLAG_ORDER)
    diagnostic_flags = _sort_flags(diagnostic, DIAGNOSTIC_FLAG_ORDER)
    if exclusions:
        decision: Literal["prioritize", "review", "exclude", "unassessed"] = "exclude"
    elif review_flags:
        decision = "review"
    elif fit_score >= packaged.theta:
        decision = "prioritize"
    else:
        decision = "review"
    return JevResult(
        decision=decision,
        fit_probability=fit_probability,
        fit_score=fit_score,
        exclusions=tuple(exclusions),
        primary_rule=primary,
        review_flags=review_flags,
        diagnostic_flags=diagnostic_flags,
        degree_timing_review="degree_timing_review" in review,
        experience_timing_review="experience_timing_review" in review,
    )
