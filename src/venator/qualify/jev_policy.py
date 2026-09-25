"""Jev policy projection and I10-safe case preparation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from venator.profile.schema import JevPolicy, Profile
from venator.qualify.compile import ProfileInconsistent, canonical_json, compile_profile, project
from venator.qualify.engine import Evidence, _GRAD, _START, _month_from_clause, evidence
from venator.qualify.jev_contract import UNASSESSED_REASONS
from venator.qualify.jev_questions import DOMAIN_DESCRIPTIONS, request_questions
from venator.qualify.jev_release import JEV_RELEASE, JevRelease, STATE_RENDERER_VERSION
from venator.qualify.posting import POSTING_CHAR_CAP, CanonicalPosting, canonical_posting
from venator.qualify.schema import CompiledProfile, E7, StudentProjection
from venator.qualify.versions import (
    jev_accepted_profile_hash,
    jev_input_version,
    jev_policy_hash,
    jev_request_key,
    jev_state_sha256,
)

WIRE_POLICY_FIELDS = (
    "restricted_roles",
    "temporary_student_authorization_exclusion",
    "no_sponsorship_student",
    "no_sponsorship_nonstudent",
    "unmet_completed_degree",
    "domains",
)


@dataclass(frozen=True)
class JevLocalBindings:
    posting_key: str
    posting_revision: str
    posting_group: str
    as_of_month: str
    profile_hash: str
    policy_hash: str
    accepted_profile_hash: str
    input_version: str
    state_sha256: str
    request_key: str
    questions_sha256: str
    qualifier_version: str
    occupancy_all: int
    experience_refutable: bool
    education_refutable: bool
    cutoff_month: str
    expected_graduation: str | None
    start_clause_months: tuple[str, ...]
    graduation_clause_months: tuple[str, ...]


@dataclass(frozen=True)
class PreparedJevCase:
    state: Mapping[str, object]
    questions: Mapping[str, object]
    bindings: JevLocalBindings
    compiled: CompiledProfile
    canonical: CanonicalPosting
    projection: StudentProjection
    policy: JevPolicy
    engine_evidence: Evidence


@dataclass(frozen=True)
class JevPreparation:
    reason: str | None
    case: PreparedJevCase | None


def render_wire_policy(policy: JevPolicy) -> dict[str, object]:
    return {
        "restricted_roles": policy.restricted_roles,
        "temporary_student_authorization_exclusion": policy.temporary_student_authorization_exclusion,
        "no_sponsorship_student": policy.no_sponsorship_student,
        "no_sponsorship_nonstudent": policy.no_sponsorship_nonstudent,
        "unmet_completed_degree": policy.unmet_completed_degree,
        "domains": {token: DOMAIN_DESCRIPTIONS[token] for token in sorted(policy.domains)},
    }


def render_applicant(projection: StudentProjection) -> dict[str, object]:
    return json.loads(canonical_json(projection))


def clause_months(quotes: tuple[str, ...]) -> tuple[str, ...]:
    months: list[str] = []
    for quote in quotes:
        match = _START.search(quote) or _GRAD.search(quote)
        if match is None:
            continue
        month = _month_from_clause(match.group("month"))
        if month is not None:
            months.append(month)
    return tuple(months)


def _as_of_month(value: str) -> str:
    if len(value) >= 7 and value[4] == "-":
        month = value[:7]
        year, number = month.split("-")
        if year.isdigit() and number.isdigit() and 1 <= int(number) <= 12:
            return month
    raise ValueError("as_of_month must be YYYY-MM")


def _description_reason(posting: Mapping[str, object], canonical: CanonicalPosting) -> str | None:
    kind = posting.get("description_kind")
    text = canonical.description_text
    if kind != "full":
        if kind == "snippet":
            return "snippet"
        return "missing"
    if not isinstance(text, str) or not text.strip():
        return "missing"
    if canonical.too_long or len(text) > POSTING_CHAR_CAP:
        return "too_long"
    return None


def prepare_jev_case(
    posting: Mapping[str, object],
    profile: Profile | None,
    as_of_month: str,
    release: JevRelease | None = None,
    *,
    group_index: Mapping[str, object] | None = None,
    compiled_profile: CompiledProfile | None = None,
) -> JevPreparation:
    """Canonicalize, compile, evidence, project (E7), then form wire state.

    Policy is enum-only. ``policy_version`` and any personal status never
    reach the wire. A populated description string is not proof of
    ``description_kind: full``.
    """
    packaged = release or JEV_RELEASE
    month = _as_of_month(as_of_month)
    if profile is None or profile.scaffold:
        assert_reason_closed("no_profile")
        return JevPreparation("no_profile", None)
    policy = profile.filters.jev
    if policy is None:
        assert_reason_closed("policy_missing")
        return JevPreparation("policy_missing", None)
    try:
        canonical = canonical_posting(posting, group_index or {})
    except (TypeError, ValueError):
        assert_reason_closed("missing")
        return JevPreparation("missing", None)
    reason = _description_reason(posting, canonical)
    if reason is not None:
        assert_reason_closed(reason)
        return JevPreparation(reason, None)
    try:
        compiled = compiled_profile or compile_profile(profile, as_of_month=month)
    except (ProfileInconsistent, ValueError):
        assert_reason_closed("no_profile")
        return JevPreparation("no_profile", None)
    engine = evidence(compiled, canonical, month)
    projection, _visibility = project(compiled, E7(engine.recency_clause, engine.gpa_clause))
    wire_policy = render_wire_policy(policy)
    assert tuple(wire_policy) == WIRE_POLICY_FIELDS
    assert "policy_version" not in wire_policy
    assert "status" not in wire_policy
    applicant = render_applicant(projection)
    state = MappingProxyType({
        "applicant": applicant,
        "policy": wire_policy,
        "posting": {
            "title": canonical.title,
            "description_text": canonical.description_text,
        },
    })
    policy_digest = jev_policy_hash(policy)
    accepted = jev_accepted_profile_hash(compiled.profile_hash, policy_digest)
    input_digest = jev_input_version(
        posting_key=canonical.posting_key,
        posting_revision=canonical.posting_revision,
        profile_hash_value=compiled.profile_hash,
        policy_hash_value=policy_digest,
        as_of_month=month,
        canonicalizer_hash=packaged.canonicalizer_hash,
        compiler_hash=packaged.compiler_hash,
        evidence_hash=packaged.evidence_hash,
        projection_hash=packaged.projection_hash,
        deny_hash=packaged.deny_hash,
        state_renderer_version=STATE_RENDERER_VERSION,
    )
    state_digest = jev_state_sha256(dict(state))
    request_digest = jev_request_key(
        model_requested=packaged.model_requested,
        state_sha256=state_digest,
        questions_sha256=packaged.questions_sha256,
        api_contract=packaged.api_contract,
    )
    starts = clause_months(tuple(clause.quote for clause in engine.start_date_clauses))
    grads = clause_months(tuple(clause.quote for clause in engine.graduation_clauses))
    bindings = JevLocalBindings(
        posting_key=canonical.posting_key,
        posting_revision=canonical.posting_revision,
        posting_group=canonical.posting_group,
        as_of_month=month,
        profile_hash=compiled.profile_hash,
        policy_hash=policy_digest,
        accepted_profile_hash=accepted,
        input_version=input_digest,
        state_sha256=state_digest,
        request_key=request_digest,
        questions_sha256=packaged.questions_sha256,
        qualifier_version=packaged.qualifier_version,
        occupancy_all=engine.occupancy_all,
        experience_refutable=engine.experience_refutable,
        education_refutable=engine.education_refutable,
        cutoff_month=engine.cutoff_month,
        expected_graduation=compiled.availability.expected_graduation,
        start_clause_months=starts,
        graduation_clause_months=grads,
    )
    case = PreparedJevCase(
        state=state,
        questions=MappingProxyType(request_questions()),
        bindings=bindings,
        compiled=compiled,
        canonical=canonical,
        projection=projection,
        policy=policy,
        engine_evidence=engine,
    )
    return JevPreparation(None, case)


def assert_reason_closed(reason: str | None) -> None:
    if reason is not None and reason not in UNASSESSED_REASONS:
        raise ValueError(f"unassessed reason is not in the closed set: {reason}")
