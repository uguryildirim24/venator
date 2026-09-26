"""Read-only per-Posting Jev context for matching and freshness.

``effective_jev_context`` is the one seam ``match`` uses. It performs no HTTP,
creates no state and does not call an evaluator. A context is returned only
for a current promoted success. Missing activation, a stale binding, no
current success or a preparation failure returns ``context is None`` with a
fixed reason. Wrong-Profile ownership or corrupt authoritative data is a
hard error.

Kind dispatch uses the store: ``state.current(..., qualifier_kind='typesafe_jev')``
and ``effective_rows(..., qualifier_kind='typesafe_jev')``. This module never
writes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from venator.discover.store import posting_revision
from venator.match.store import filters_version
from venator.profile.schema import Profile
from venator.qualify.compile import ProfileInconsistent, compile_profile
from venator.qualify.jev_contract import JevContext, JevExclusion, JevResolution
from venator.qualify.jev_policy import prepare_jev_case
from venator.qualify.jev_release import JEV_RELEASE, JevRelease
from venator.qualify.versions import jev_input_version, jev_policy_hash
from venator.qualify.store import (
    Activation,
    JEV_KIND,
    JEV_SUCCESS,
    PromotionState,
    effective_rows,
    fold_events,
    promotion_events,
    qualification_rows,
    qualifier_kind_of,
    verify_store,
)

SUCCESS_DECISIONS = JEV_SUCCESS

FALLBACK_INACTIVE = "inactive"
FALLBACK_NOT_ACTIVATED = "not_activated"
FALLBACK_REVOKED = "revoked"
FALLBACK_STALE_PROFILE = "stale_profile"
FALLBACK_STALE_RELEASE = "stale_release"
FALLBACK_NO_CURRENT_SUCCESS = "no_current_success"
FALLBACK_SHADOW_ONLY = "shadow_only"
FALLBACK_WRONG_KIND = "wrong_kind"

FALLBACK_REASONS = frozenset(
    {
        FALLBACK_INACTIVE,
        FALLBACK_NOT_ACTIVATED,
        FALLBACK_REVOKED,
        FALLBACK_STALE_PROFILE,
        FALLBACK_STALE_RELEASE,
        FALLBACK_NO_CURRENT_SUCCESS,
        FALLBACK_SHADOW_ONLY,
        FALLBACK_WRONG_KIND,
        "snippet",
        "missing",
        "too_long",
        "no_profile",
        "policy_missing",
        "credentials_missing",
        "transport_failed",
        "response_invalid",
        "model_mismatch",
        "budget_exhausted",
        "preparation_failed",
    }
)


#: Preparation failures that are about the Posting's text. Their input binding
#: is still known — Posting revision, Profile, policy, month and release — so
#: a fallback decision on them is settled until one of those moves.
DESCRIPTION_REASONS = frozenset({"snippet", "missing", "too_long"})


def bound_input_version(
    posting: Mapping[str, object],
    profile: Profile,
    as_of_month: str,
    release: JevRelease | None = None,
) -> str | None:
    """The Jev ``input_version`` of a Posting whose state could not be prepared.

    Same formula as ``prepare_jev_case``. None only when no binding exists: no
    Jev policy, a Profile that does not compile, or a Posting without a key.
    """
    packaged = release or JEV_RELEASE
    policy = profile.filters.jev
    key = posting.get("key")
    if policy is None or not isinstance(key, str) or not key:
        return None
    try:
        compiled = compile_profile(profile, as_of_month=as_of_month)
    except (ProfileInconsistent, ValueError):
        return None
    return jev_input_version(
        posting_key=key,
        posting_revision=posting_revision(posting),
        profile_hash_value=compiled.profile_hash,
        policy_hash_value=jev_policy_hash(policy),
        as_of_month=as_of_month,
        canonicalizer_hash=packaged.canonicalizer_hash,
        compiler_hash=packaged.compiler_hash,
        evidence_hash=packaged.evidence_hash,
        projection_hash=packaged.projection_hash,
        deny_hash=packaged.deny_hash,
        state_renderer_version=packaged.state_renderer_version,
    )


def reserved_jev_facts(resolution: JevResolution) -> dict[str, str]:
    """The two freshness tokens stamped on every new jev-mode Filter Decision."""
    context = resolution.context
    return {
        "jev": context.assessment_key if context is not None else "fallback",
        "jev_input": resolution.input_version or "unavailable",
    }


def current_filter_version(
    profile: Profile,
    promotion_state: PromotionState | None = None,
    release: JevRelease | None = None,
) -> str:
    """The version stamped on Filter Decisions.

    Promotion revision and release hash are supplied only in jev mode. Legacy
    callers of ``filters_version`` with empty keywords keep their old digest.
    """
    revision = ""
    release_hash = ""
    if profile.filters.qualification_mode == "jev":
        if promotion_state is not None:
            revision = promotion_state.revision
        if release is not None:
            release_hash = release.release_hash
    return filters_version(
        profile.constraints_path,
        profile.targeting_path,
        promotion_state_revision=revision,
        jev_release_hash=release_hash,
    )


def hard_decision_is_fresh(
    decision: Mapping[str, object] | None,
    resolution: JevResolution,
    *,
    version: str,
    posting_revision: str | None = None,
    jev_mode: bool = False,
) -> bool:
    """Whether a stored hard Filter Decision may be trusted as current.

    An unavailable input binding is never treated as settled. In deterministic mode the existing version and
    pass checks are unchanged.
    """
    if decision is None or decision.get("verdict") != "pass":
        return False
    if decision.get("filters_version") != version:
        return False
    if posting_revision is not None and decision.get("posting_version") != posting_revision:
        return False
    if not jev_mode:
        return True
    facts = decision.get("facts")
    if not isinstance(facts, dict):
        return False
    reserved = reserved_jev_facts(resolution)
    if reserved["jev_input"] == "unavailable":
        return False
    return facts.get("jev") == reserved["jev"] and facts.get("jev_input") == reserved["jev_input"]


def label_fallback_reason(reason: str, resolution: JevResolution) -> str:
    """Append the visible fallback label without claiming Jev checked the Posting."""
    if resolution.context is not None or resolution.fallback_reason is None:
        return reason
    label = f"; Jev unavailable ({resolution.fallback_reason}); deterministic fallback"
    # A settled decision comes back from ``decide`` with the label it was
    # recorded with. Labelling it again made the reason grow on every run, so
    # the unchanged decision never stood and was appended again each time.
    if reason.endswith(label):
        return reason
    return f"{reason}{label}"


def apply_reserved_facts(verdict: str, facts: Mapping[str, str] | None, resolution: JevResolution) -> dict[str, str]:
    """Reserved pair on every jev-mode decision; other tokens only on a pass."""
    reserved = reserved_jev_facts(resolution)
    if verdict == "kill":
        return reserved
    merged = dict(facts or ())
    merged.update(reserved)
    return merged


def jev_promotion_state(events: Iterable[Mapping[str, object]], profile_id: str) -> PromotionState:
    """The store's kind-aware fold. Jev current() is a separate lookup."""
    return fold_events(events, profile_id)


def _exclusions(raw: object) -> tuple[JevExclusion, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    packed: list[JevExclusion] = []
    for item in raw:
        if isinstance(item, JevExclusion):
            packed.append(item)
            continue
        if not isinstance(item, dict):
            raise ValueError("corrupt Jev exclusion payload")
        probabilities_raw = item.get("probabilities") or ()
        probabilities: list[tuple[str, float]] = []
        for pair in probabilities_raw:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("corrupt Jev exclusion probabilities")
            probabilities.append((str(pair[0]), float(pair[1])))
        packed.append(
            JevExclusion(
                rule=str(item["rule"]),
                basis="model_assisted_policy",
                question_ids=tuple(str(value) for value in item.get("question_ids") or ()),
                probabilities=tuple(probabilities),
                guard_code=str(item.get("guard_code") or ""),
                policy_value=None if item.get("policy_value") is None else str(item["policy_value"]),
                policy_exclusion=bool(item.get("policy_exclusion")),
            )
        )
    return tuple(packed)


def context_from_row(row: Mapping[str, object]) -> JevContext:
    """Build a ``JevContext`` from a stored success row. Missing fields are corrupt."""
    decision = row.get("decision")
    if decision not in SUCCESS_DECISIONS:
        raise ValueError("corrupt Jev success row: decision is not prioritize, review or exclude")
    try:
        fit_probability = float(row["fit_probability"])
        fit_score = float(row["fit_score"])
        posting_key = str(row["posting_key"])
        posting_revision = str(row["posting_revision"])
        as_of_month = str(row["as_of_month"])
        input_version = str(row["input_version"])
        qualifier_version = str(row["qualifier_version"])
        accepted = str(row["accepted_profile_hash"])
        assessment_key = str(row["assessment_key"])
        response_sha256 = str(row["response_sha256"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("corrupt Jev success row") from error
    if not all((posting_key, input_version, qualifier_version, accepted, assessment_key, response_sha256)):
        raise ValueError("corrupt Jev success row")
    primary = row.get("primary_rule")
    return JevContext(
        posting_key=posting_key,
        posting_revision=posting_revision,
        as_of_month=as_of_month,
        input_version=input_version,
        qualifier_version=qualifier_version,
        accepted_profile_hash=accepted,
        assessment_key=assessment_key,
        response_sha256=response_sha256,
        decision=decision,
        fit_probability=fit_probability,
        fit_score=fit_score,
        exclusions=_exclusions(row.get("exclusions")),
        primary_rule=None if primary is None else str(primary),
        review_flags=tuple(str(flag) for flag in row.get("review_flags") or ()),
        diagnostic_flags=tuple(str(flag) for flag in row.get("diagnostic_flags") or ()),
    )


def _current_success(
    rows: Sequence[Mapping[str, object]],
    *,
    posting_key: str,
    profile_id: str,
    input_version: str,
    qualifier_version: str,
    accepted_profile_hash: str,
) -> Mapping[str, object] | None:
    """Latest compatible promoted success at the current marker, never a failure."""
    versions = {posting_key: input_version}
    promoted = effective_rows(
        rows,
        profile_id=profile_id,
        mode="promoted",
        qualifier_version=qualifier_version,
        input_versions=versions,
        qualifier_kind=JEV_KIND,
    ).get(posting_key)
    if promoted is not None:
        if promoted.get("accepted_profile_hash") not in {None, accepted_profile_hash}:
            return None
        decision = promoted.get("decision")
        if decision in SUCCESS_DECISIONS:
            return promoted
        if decision == "unassessed":
            return None
        raise ValueError("corrupt Jev row: unknown decision")
    shadow = effective_rows(
        rows,
        profile_id=profile_id,
        mode="shadow",
        qualifier_version=qualifier_version,
        input_versions=versions,
        qualifier_kind=JEV_KIND,
    ).get(posting_key)
    if shadow is not None:
        return {"_shadow_only": True}
    return None


def effective_jev_context(
    posting: Mapping[str, object],
    profile: Profile,
    *,
    as_of_month: str,
    release: JevRelease | None = None,
    rows: Sequence[Mapping[str, object]] | None = None,
    events: Sequence[Mapping[str, object]] | None = None,
    qualifications_dir: Path | None = None,
) -> JevResolution:
    """Resolve the current promoted Jev assessment for one Posting, or fall back.

    Callers with many Postings use ``resolve_postings``, which reads the store
    once.
    """
    packaged = release or JEV_RELEASE
    if profile.filters.qualification_mode != "jev":
        return JevResolution(None, None, FALLBACK_INACTIVE)

    if qualifications_dir is not None:
        verify_store(qualifications_dir, profile.identifier)
        if rows is None:
            rows = qualification_rows(qualifications_dir)
        if events is None:
            events = promotion_events(qualifications_dir)

    snapshot_rows = list(rows or ())
    snapshot_events = list(events or ())
    for stored in snapshot_rows:
        owner = stored.get("profile_id")
        if isinstance(owner, str) and owner and owner != profile.identifier:
            raise ValueError(
                f"qualification row names Profile {owner!r}, not {profile.identifier!r}"
            )

    prepared = prepare_jev_case(posting, profile, as_of_month, packaged)
    input_version = prepared.case.bindings.input_version if prepared.case is not None else None
    if prepared.reason in DESCRIPTION_REASONS:
        # A snippet is bound as firmly as a full Posting; reporting it
        # unavailable made every match.run append the same fallback again and
        # left the Posting unscoreable for good.
        input_version = bound_input_version(posting, profile, as_of_month, packaged)
    if prepared.reason is not None:
        resolved = JevResolution(None, input_version, prepared.reason)
        return resolved

    assert prepared.case is not None
    bindings = prepared.case.bindings
    state = fold_events(snapshot_events, profile.identifier)
    active: Activation | None = state.current(
        bindings.accepted_profile_hash, qualifier_kind=JEV_KIND
    )
    if active is None:
        pointer = state.active
        saw_jev = any(
            event.get("event") == "activated" and qualifier_kind_of(event) == JEV_KIND
            for event in snapshot_events
        )
        if pointer is None:
            reason = FALLBACK_REVOKED if saw_jev else FALLBACK_NOT_ACTIVATED
        elif pointer.qualifier_kind != JEV_KIND:
            reason = FALLBACK_NOT_ACTIVATED
        else:
            reason = FALLBACK_STALE_PROFILE
        resolved = JevResolution(None, input_version, reason)
        return resolved
    if active.qualifier_version != packaged.qualifier_version:
        resolved = JevResolution(None, input_version, FALLBACK_STALE_RELEASE)
        return resolved
    if active.qualifier_version != bindings.qualifier_version:
        resolved = JevResolution(None, input_version, FALLBACK_STALE_RELEASE)
        return resolved

    found = _current_success(
        snapshot_rows,
        posting_key=bindings.posting_key,
        profile_id=profile.identifier,
        input_version=bindings.input_version,
        qualifier_version=active.qualifier_version,
        accepted_profile_hash=bindings.accepted_profile_hash,
    )
    if found is None:
        resolved = JevResolution(None, input_version, FALLBACK_NO_CURRENT_SUCCESS)
        return resolved
    if found.get("_shadow_only"):
        resolved = JevResolution(None, input_version, FALLBACK_SHADOW_ONLY)
        return resolved
    kind = found.get("qualifier_kind") or found.get("kind")
    if kind not in {None, JEV_KIND}:
        resolved = JevResolution(None, input_version, FALLBACK_WRONG_KIND)
        return resolved
    if found.get("response_sha256") in {None, ""}:
        raise ValueError("corrupt Jev success row: response_sha256 is missing")
    context = context_from_row(found)
    resolved = JevResolution(context, input_version, None)
    return resolved


def resolve_postings(
    postings: Sequence[Mapping[str, object]],
    profile: Profile,
    *,
    as_of_month: str,
    qualifications_dir: Path | None,
    release: JevRelease | None = None,
) -> dict[str, JevResolution]:
    """``effective_jev_context`` for every Posting against one store snapshot.

    The store is verified and read once, then indexed by Posting key. Each
    Posting resolves against only its own history. A missing store is read as
    empty and is never created. ``match.run`` resolves through this.
    """
    rows: list[Mapping[str, object]] = []
    events: list[Mapping[str, object]] = []
    if qualifications_dir is not None and qualifications_dir.exists():
        verify_store(qualifications_dir, profile.identifier)
        rows = list(qualification_rows(qualifications_dir))
        events = list(promotion_events(qualifications_dir))
    # The per-Posting resolver deliberately snapshots its rows and checks Profile
    # ownership. Do the ownership check across the *whole* store here before
    # narrowing the snapshot; a stray row for another Posting must still fail.
    by_key: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        owner = row.get("profile_id")
        if isinstance(owner, str) and owner and owner != profile.identifier:
            raise ValueError(f"qualification row names Profile {owner!r}, not {profile.identifier!r}")
        key = row.get("posting_key")
        if isinstance(key, str):
            by_key[key].append(row)
    return {
        str(posting["key"]): effective_jev_context(
            posting,
            profile,
            as_of_month=as_of_month,
            release=release,
            rows=by_key.get(str(posting["key"]), ()),
            events=events,
        )
        for posting in postings
    }
