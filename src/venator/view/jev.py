"""Jev triage rows for the disposable view.

The Jev triage DDL follows SPEC-jev §10. Row identity comes from the kind-aware
store: ``store.current`` and ``effective_rows`` are called with
``qualifier_kind='typesafe_jev'``. This module never makes a network request.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from venator.discover.store import posting_revision
from venator.profile.schema import Profile
from venator.qualify.jev_release import JEV_RELEASE
from venator.qualify.store import JEV_KIND, JEV_SCHEMA, JEV_SUCCESS, selected_shadow
from venator.qualify.versions import jev_accepted_profile_hash, jev_input_version, jev_policy_hash

SUCCESS_DECISIONS = JEV_SUCCESS
MODES = ("promoted", "shadow")

# Live assessments schema; kept in sync with coordination/CONTRACTS.md and the fixture builder.
ASSESSMENTS_DDL = """\
CREATE TABLE assessments (
  posting_key TEXT PRIMARY KEY,
  status TEXT,
  summary TEXT,
  evidence TEXT,
  conflicts TEXT,
  unknowns TEXT,
  listing_status TEXT,
  last_verified_at TEXT,
  description_kind TEXT,
  apply_url TEXT,
  opportunity_type TEXT,
  input_version TEXT,
  assessed_by TEXT NOT NULL DEFAULT 'deterministic'
    CHECK (assessed_by IN ('deterministic', 'jev')),
  assessed_as_of TEXT
);"""

JEV_TRIAGE_DDL = """\
CREATE TABLE jev_triage (
  posting_key TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('shadow', 'promoted')),
  state TEXT NOT NULL CHECK (state IN ('current', 'stale', 'unavailable')),
  decision TEXT NOT NULL
    CHECK (decision IN ('prioritize', 'review', 'exclude', 'unassessed')),
  fit_probability REAL
    CHECK (fit_probability IS NULL OR fit_probability BETWEEN 0.0 AND 1.0),
  fit_score REAL
    CHECK (fit_score IS NULL OR fit_score BETWEEN 0.0 AND 1.0),
  primary_rule TEXT,
  exclusions TEXT NOT NULL,
  review_flags TEXT NOT NULL,
  diagnostic_flags TEXT NOT NULL,
  qualifier_version TEXT,
  assessment_key TEXT,
  input_version TEXT,
  policy_hash TEXT,
  model_id TEXT,
  as_of_month TEXT NOT NULL,
  decided_at TEXT,
  reason TEXT,
  PRIMARY KEY (posting_key, mode),
  CHECK (decision <> 'unassessed' OR
         (fit_probability IS NULL AND fit_score IS NULL AND primary_rule IS NULL)),
  CHECK (state <> 'current' OR
         (decision <> 'unassessed' AND fit_probability IS NOT NULL AND
          fit_score IS NOT NULL AND assessment_key IS NOT NULL AND
          qualifier_version IS NOT NULL AND input_version IS NOT NULL))
);"""


def is_jev_row(row: Mapping[str, Any]) -> bool:
    return row.get("schema") == JEV_SCHEMA and row.get("qualifier_kind") == JEV_KIND


def is_success(row: Mapping[str, Any]) -> bool:
    validation = row.get("response_validation")
    passed = isinstance(validation, Mapping) and validation.get("passed") is True
    return (
        row.get("decision") in SUCCESS_DECISIONS
        and passed
        and isinstance(row.get("fit_probability"), (int, float))
        and isinstance(row.get("fit_score"), (int, float))
        and isinstance(row.get("assessment_key"), str)
        and row["assessment_key"] != ""
        and isinstance(row.get("qualifier_version"), str)
        and row["qualifier_version"] != ""
        and isinstance(row.get("input_version"), str)
        and row["input_version"] != ""
    )


def selected_jev_shadow(rows: Iterable[Mapping[str, Any]], profile_id: str) -> str | None:
    return selected_shadow(rows, profile_id, qualifier_kind=JEV_KIND)


def jev_view_bindings(
    profile: Profile,
    compiled: object,
    postings: Sequence[Mapping[str, Any]],
    as_of_month: str,
) -> tuple[str | None, dict[str, str]]:
    """Accepted Profile hash and per-Posting Jev input versions, from R1.

    ``accepted`` is None when the Profile has no Jev policy, so the view
    cannot look up a Jev activation.
    """
    policy = profile.filters.jev
    profile_hash = getattr(compiled, "profile_hash", None)
    if policy is None or not isinstance(profile_hash, str):
        return None, {}
    policy_digest = jev_policy_hash(policy)
    accepted = jev_accepted_profile_hash(profile_hash, policy_digest)
    release = JEV_RELEASE
    versions = {
        str(posting["key"]): jev_input_version(
            posting_key=str(posting["key"]),
            posting_revision=posting_revision(posting),
            profile_hash_value=profile_hash,
            policy_hash_value=policy_digest,
            as_of_month=as_of_month,
            canonicalizer_hash=release.canonicalizer_hash,
            compiler_hash=release.compiler_hash,
            evidence_hash=release.evidence_hash,
            projection_hash=release.projection_hash,
            deny_hash=release.deny_hash,
            state_renderer_version=release.state_renderer_version,
        )
        for posting in postings
    }
    return accepted, versions


def _json_array(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    return "[]"


def _model_id(row: Mapping[str, Any]) -> str | None:
    returned = row.get("model_returned")
    if isinstance(returned, str) and returned:
        return returned
    identity = row.get("qualifier_identity")
    if isinstance(identity, Mapping):
        requested = identity.get("model_requested")
        if isinstance(requested, str) and requested:
            return requested
    return None


def _row_matches_binding(
    row: Mapping[str, Any],
    *,
    as_of_month: str,
    posting_revision: str,
    qualifier_version: str | None,
    input_version: str | None = None,
) -> bool:
    """Whether ``row`` answers today's input. ``input_version`` carries the
    Profile and policy hashes, so an edit to either leaves an old row stale."""
    if qualifier_version is None:
        return False
    return (
        str(row.get("as_of_month") or "") == as_of_month
        and str(row.get("posting_revision") or "") == posting_revision
        and str(row.get("qualifier_version") or "") == qualifier_version
        and (input_version is None or row.get("input_version") == input_version)
    )


def _is_current_success(
    row: Mapping[str, Any],
    posting: Mapping[str, Any],
    *,
    as_of_month: str,
    posting_revision: str,
    qualifier_version: str | None,
    input_version: str | None,
    current_check: Callable[..., bool] | None,
) -> bool:
    if not is_success(row) or not _row_matches_binding(
        row,
        as_of_month=as_of_month,
        posting_revision=posting_revision,
        qualifier_version=qualifier_version,
        input_version=input_version,
    ):
        return False
    if current_check is None:
        return True
    return bool(current_check(row, posting))


def _tuple_from_row(row: Mapping[str, Any], *, state: str) -> tuple[object, ...]:
    decision = str(row.get("decision") or "unassessed")
    if decision not in SUCCESS_DECISIONS and decision != "unassessed":
        decision = "unassessed"
    if state == "current" and decision == "unassessed":
        state = "unavailable"
    success = is_success(row) and decision in SUCCESS_DECISIONS
    fit_probability = float(row["fit_probability"]) if success else None
    fit_score = float(row["fit_score"]) if success else None
    primary_rule = str(row["primary_rule"]) if success and row.get("primary_rule") else None
    if decision == "unassessed":
        fit_probability = None
        fit_score = None
        primary_rule = None
    return (
        str(row["posting_key"]),
        str(row["mode"]),
        state,
        decision if success or decision == "unassessed" else "unassessed",
        fit_probability,
        fit_score,
        primary_rule,
        _json_array(row.get("exclusions")) if success else "[]",
        _json_array(row.get("review_flags")) if success else "[]",
        _json_array(row.get("diagnostic_flags")) if success else "[]",
        str(row["qualifier_version"]) if row.get("qualifier_version") else None,
        str(row["assessment_key"]) if success else None,
        str(row["input_version"]) if row.get("input_version") else None,
        str(row["policy_hash"]) if row.get("policy_hash") else None,
        _model_id(row),
        str(row.get("as_of_month") or ""),
        str(row["decided_at"]) if row.get("decided_at") else None,
        str(row["reason"]) if row.get("reason") else None,
    )


def _placeholder(posting_key: str, mode: str, as_of_month: str, reason: str | None) -> tuple[object, ...]:
    return (
        posting_key,
        mode,
        "unavailable",
        "unassessed",
        None,
        None,
        None,
        "[]",
        "[]",
        "[]",
        None,
        None,
        None,
        None,
        None,
        as_of_month,
        None,
        reason,
    )


def materialize_triage(
    postings: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    profile_id: str,
    as_of_month: str,
    posting_revisions: Mapping[str, str],
    promoted_qualifier: str | None,
    shadow_qualifier: str | None,
    input_versions: Mapping[str, str] | None = None,
    current_check: Callable[..., bool] | None = None,
) -> list[tuple[object, ...]]:
    """One triage row per Posting per mode: current success, else stale, else unavailable.

    A current failure is shown as unavailable with its reason. An older success is
    never relabelled current in that case. Historical scores are marked stale.
    Current success is a store identity match: the active Jev qualifier from
    ``PromotionState.current(..., qualifier_kind='typesafe_jev')``.
    """
    check = current_check
    by_key_mode: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        if not is_jev_row(row):
            continue
        key = row.get("posting_key")
        mode = row.get("mode")
        if not isinstance(key, str) or mode not in MODES:
            continue
        if row.get("profile_id") != profile_id:
            continue
        by_key_mode.setdefault((key, mode), []).append(row)

    materialized: list[tuple[object, ...]] = []
    for posting in postings:
        key = str(posting["key"])
        revision = posting_revisions.get(key, "")
        current_input = None if input_versions is None else input_versions.get(key, "")
        for mode in MODES:
            expected_qualifier = promoted_qualifier if mode == "promoted" else shadow_qualifier
            history = list(by_key_mode.get((key, mode), []))
            current_success = None
            current_failure = None
            for row in reversed(history):
                if _is_current_success(
                    row,
                    posting,
                    as_of_month=as_of_month,
                    posting_revision=revision,
                    qualifier_version=expected_qualifier,
                    input_version=current_input,
                    current_check=check,
                ):
                    current_success = row
                    break
                if _row_matches_binding(
                    row,
                    as_of_month=as_of_month,
                    posting_revision=revision,
                    qualifier_version=expected_qualifier,
                    input_version=current_input,
                ) and not is_success(row):
                    current_failure = row
                    break
            if current_success is not None:
                materialized.append(_tuple_from_row(current_success, state="current"))
                continue
            if current_failure is not None:
                reason = str(current_failure.get("reason") or "Jev assessment is unavailable")
                materialized.append(_placeholder(key, mode, as_of_month, reason))
                continue
            stale = None
            for row in reversed(history):
                if is_success(row):
                    stale = row
                    break
            if stale is not None:
                materialized.append(_tuple_from_row(stale, state="stale"))
                continue
            materialized.append(
                _placeholder(key, mode, as_of_month, "Jev assessment is unavailable")
            )
    return materialized
