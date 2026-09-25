"""Freshness for jev-mode matching."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from venator.match.run import run
from venator.match.store import (
    FILTERS_REVISION,
    claim_decisions_dir,
    filters_version,
    iter_jsonl,
    load_latest_decisions,
)
from venator.profile.loader import load_profile
from venator.qualify.jev_effective import (
    current_filter_version,
    effective_jev_context,
    hard_decision_is_fresh,
    jev_promotion_state,
    reserved_jev_facts,
)
from venator.qualify.jev_policy import prepare_jev_case
from venator.qualify.jev_release import JEV_RELEASE
from venator.qualify.store import JEV_KIND, append_event, append_rows, promotion_events, qualification_rows
from venator.qualify.versions import jev_assessment_key

FIXTURE = Path(__file__).resolve().parents[1] / "qualify" / "fixtures" / "jev"
AS_OF = "2026-09"
AS_OF_DAY = "2026-09-13"


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def fixture_profile():
    return load_profile(FIXTURE / "profile")


def fixture_posting() -> dict:
    return json.loads((FIXTURE / "posting.json").read_text(encoding="utf-8"))


def activate_jev(directory: Path, profile_id: str, bindings) -> str:
    common = {
        "profile_id": profile_id,
        "profile_hash": bindings.accepted_profile_hash,
        "compiled_profile_hash": bindings.profile_hash,
        "policy_hash": bindings.policy_hash,
        "qualifier_version": bindings.qualifier_version,
        "qualifier_kind": JEV_KIND,
        "components": {},
        "finalization_sha256": "sha",
        "cohort_hashes": {},
        "at": "2026-09-13T00:00:00+00:00",
    }
    finalized = append_event(
        directory, profile_id, {**common, "event": "finalized", "refers_to": None, "by": "finalize"}
    )
    signed = append_event(
        directory, profile_id, {**common, "event": "signed", "refers_to": finalized, "by": "sample"}
    )
    return append_event(
        directory, profile_id, {**common, "event": "activated", "refers_to": signed, "by": "sample"}
    )


def success_row(bindings, profile_id: str, *, decision: str = "prioritize", **extra: object) -> dict:
    request = str(extra.pop("request_key", bindings.request_key))
    response = extra.pop("response_sha256", "b" * 64)
    row = {
        "schema": "jev-assessment-2",
        "qualifier_kind": JEV_KIND,
        "posting_key": bindings.posting_key,
        "profile_id": profile_id,
        "posting_revision": bindings.posting_revision,
        "profile_hash": bindings.profile_hash,
        "policy_hash": bindings.policy_hash,
        "accepted_profile_hash": bindings.accepted_profile_hash,
        "as_of_month": bindings.as_of_month,
        "input_version": bindings.input_version,
        "qualifier_version": bindings.qualifier_version,
        "qualifier_identity": dict(JEV_RELEASE.qualifier_identity),
        "assessment_key": extra.pop(
            "assessment_key",
            jev_assessment_key(
                input_version_value=bindings.input_version,
                qualifier_version_value=bindings.qualifier_version,
                request_key=request,
                response_sha256=None if response is None else str(response),
            ),
        ),
        "request_key": request,
        "state_sha256": bindings.state_sha256,
        "questions_sha256": bindings.questions_sha256,
        "mode": extra.pop("mode", "promoted"),
        "decision": decision,
        "fit_probability": extra.pop("fit_probability", 0.9),
        "fit_score": extra.pop("fit_score", 0.8),
        "exclusions": extra.pop("exclusions", []),
        "primary_rule": extra.pop("primary_rule", None),
        "review_flags": extra.pop("review_flags", []),
        "diagnostic_flags": extra.pop("diagnostic_flags", []),
        "response_validation": extra.pop(
            "response_validation", {"passed": True, "version": "1", "error_codes": []}
        ),
        "response_sha256": response,
        "response_ref": extra.pop("response_ref", f"jev/responses/{request}.json"),
        "model_returned": extra.pop("model_returned", "jev-1.13.0"),
        "usage": extra.pop("usage", {"input_tokens": 12, "output_tokens": 0}),
        "latency_ms": extra.pop("latency_ms", 15.0),
        "decided_at": extra.pop("decided_at", "2026-09-13T00:00:00+00:00"),
        "cache_hit": extra.pop("cache_hit", False),
        "reason": extra.pop("reason", None),
    }
    row.update(extra)
    return row


def primed_store(tmp_path: Path, posting: dict | None = None, **row_extra: object):
    profile = fixture_profile()
    posting = posting or fixture_posting()
    qualifications = tmp_path / "qualifications"
    prepared = prepare_jev_case(posting, profile, AS_OF)
    assert prepared.case is not None
    bindings = prepared.case.bindings
    activate_jev(qualifications, profile.identifier, bindings)
    append_rows(
        qualifications,
        profile.identifier,
        [success_row(bindings, profile.identifier, **row_extra)],
        day=AS_OF_DAY,
    )
    return profile, posting, qualifications, bindings


def test_empty_jev_release_hash_matches_legacy_callers(tmp_path: Path) -> None:
    targeting = tmp_path / "targeting.yaml"
    targeting.write_text("filters: {}\n", encoding="utf-8")
    constraints = tmp_path / "constraints.yaml"
    constraints.write_text("{}\n", encoding="utf-8")
    baseline = filters_version(constraints, targeting)
    assert baseline == filters_version(constraints, targeting, jev_release_hash="")
    assert baseline != filters_version(constraints, targeting, jev_release_hash="changed")
    assert FILTERS_REVISION == b"22-smartrecruiters-no-location-filter"


def test_current_filter_version_adds_release_only_in_jev_mode(tmp_path: Path) -> None:
    profile = fixture_profile()
    promotion = jev_promotion_state([], profile.identifier)
    jev_version = current_filter_version(profile, promotion, JEV_RELEASE)
    plain = filters_version(profile.constraints_path, profile.targeting_path)
    assert jev_version != plain
    deterministic = replace(profile, filters=replace(profile.filters, qualification_mode="deterministic"))
    assert current_filter_version(deterministic, promotion, JEV_RELEASE) == filters_version(
        deterministic.constraints_path, deterministic.targeting_path
    )


def test_month_change_drops_the_cached_success(tmp_path: Path) -> None:
    profile, posting, qualifications, bindings = primed_store(tmp_path)
    current = effective_jev_context(
        posting, profile, as_of_month=AS_OF, release=JEV_RELEASE, qualifications_dir=qualifications
    )
    assert current.context is not None
    later = effective_jev_context(
        posting, profile, as_of_month="2026-10", release=JEV_RELEASE, qualifications_dir=qualifications
    )
    assert later.context is None
    assert later.fallback_reason == "no_current_success"
    assert later.input_version != bindings.input_version


def test_release_change_is_stale_release(tmp_path: Path) -> None:
    profile, posting, qualifications, _bindings = primed_store(tmp_path)
    stale = replace(JEV_RELEASE, theta=0.99)
    resolution = effective_jev_context(
        posting, profile, as_of_month=AS_OF, release=stale, qualifications_dir=qualifications
    )
    assert resolution.context is None
    assert resolution.fallback_reason == "stale_release"


def test_revocation_falls_back(tmp_path: Path) -> None:
    profile, posting, qualifications, bindings = primed_store(tmp_path)
    activation = jev_promotion_state(promotion_events(qualifications), profile.identifier).active
    assert activation is not None
    append_event(
        qualifications,
        profile.identifier,
        {
            "event": "revoked",
            "profile_id": profile.identifier,
            "profile_hash": bindings.accepted_profile_hash,
            "compiled_profile_hash": bindings.profile_hash,
            "policy_hash": bindings.policy_hash,
            "qualifier_version": bindings.qualifier_version,
            "qualifier_kind": JEV_KIND,
            "components": {},
            "finalization_sha256": "sha",
            "cohort_hashes": {},
            "refers_to": activation.event_id,
            "at": "2026-09-13T01:00:00+00:00",
            "by": "sample",
        },
    )
    resolution = effective_jev_context(
        posting, profile, as_of_month=AS_OF, release=JEV_RELEASE, qualifications_dir=qualifications
    )
    assert resolution.context is None
    assert resolution.fallback_reason == "revoked"


def test_stale_hard_pass_is_not_fresh(tmp_path: Path) -> None:
    profile, posting, qualifications, bindings = primed_store(tmp_path)
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(postings_dir / f"{AS_OF_DAY}.jsonl", [posting])
    claim_decisions_dir(decisions_dir, profile.identifier)
    version = current_filter_version(
        profile,
        jev_promotion_state(promotion_events(qualifications), profile.identifier),
        JEV_RELEASE,
    )
    write_jsonl(
        decisions_dir / f"{AS_OF_DAY}.jsonl",
        [
            {
                "posting_key": posting["key"],
                "stage": "hard_filter",
                "verdict": "pass",
                "rule": None,
                "reason": "passed",
                "facts": {"jev": "old-assessment", "jev_input": bindings.input_version},
                "filters_version": version,
                "profile_id": profile.identifier,
            }
        ],
    )
    resolution = effective_jev_context(
        posting, profile, as_of_month=AS_OF, release=JEV_RELEASE, qualifications_dir=qualifications
    )
    assert not hard_decision_is_fresh(
        load_latest_decisions(decisions_dir)[(posting["key"], "hard_filter")],
        resolution,
        version=version,
        jev_mode=True,
    )


def test_historical_llm_score_rows_still_fold(tmp_path: Path) -> None:
    profile, posting, qualifications, bindings = primed_store(tmp_path)
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(postings_dir / f"{AS_OF_DAY}.jsonl", [posting])
    summary = run(
        postings_dir,
        decisions_dir,
        profile=profile,
        qualifications_dir=qualifications,
        as_of_month=AS_OF,
    )
    write_jsonl(
        decisions_dir / f"{AS_OF_DAY}-score.jsonl",
        [
            {
                "posting_key": posting["key"],
                "stage": "llm_score",
                "verdict": "queue",
                "rule": None,
                "score": 80,
                "reason": "fits",
                "profile_id": profile.identifier,
                "filters_version": summary["filters_version"],
                "decided_at": "2026-09-13T12:00:00+00:00",
            }
        ],
    )
    latest = load_latest_decisions(decisions_dir)
    scored = latest[(posting["key"], "llm_score")]
    assert scored["score"] == 80
    assert scored["filters_version"] == summary["filters_version"]
    facts = reserved_jev_facts(
        effective_jev_context(
            posting, profile, as_of_month=AS_OF, release=JEV_RELEASE, qualifications_dir=qualifications
        )
    )
    standing = latest[(posting["key"], "hard_filter")]
    assert standing["facts"]["jev"] == facts["jev"]
    assert standing["facts"]["jev_input"] == bindings.input_version
    assert standing["facts"]["education_fit"] == "delegated"


def test_snippet_fallback_is_bound_and_settled(tmp_path: Path) -> None:
    """A snippet's Jev input is known, so its fallback settles.

    Reported as ``unavailable`` it was re-appended on every ``match.run`` and was
    never fresh. The month still moves the binding.
    """
    from venator.qualify.jev_effective import bound_input_version

    profile = fixture_profile()
    snippet = {**fixture_posting(), "description_kind": "snippet"}
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    qualifications = tmp_path / "qualifications"
    write_jsonl(postings_dir / f"{AS_OF_DAY}.jsonl", [snippet])
    for _ in range(3):
        run(postings_dir, decisions_dir, profile=profile,
            qualifications_dir=qualifications, as_of_month=AS_OF)
    rows = list(iter_jsonl(decisions_dir))
    assert len(rows) == 1
    bound = bound_input_version(snippet, profile, AS_OF)
    assert bound is not None
    assert rows[0]["facts"]["jev"] == "fallback"
    assert rows[0]["facts"]["jev_input"] == bound
    resolution = effective_jev_context(
        snippet, profile, as_of_month=AS_OF, release=JEV_RELEASE, qualifications_dir=qualifications
    )
    assert resolution.fallback_reason == "snippet"
    assert resolution.input_version == bound
    version = current_filter_version(
        profile, jev_promotion_state(promotion_events(qualifications), profile.identifier), JEV_RELEASE
    )
    if rows[0]["verdict"] == "pass":
        assert hard_decision_is_fresh(rows[0], resolution, version=version, jev_mode=True)
    run(postings_dir, decisions_dir, profile=profile,
        qualifications_dir=qualifications, as_of_month="2026-10")
    later = list(iter_jsonl(decisions_dir))
    assert len(later) == 2
    assert later[-1]["facts"]["jev_input"] == bound_input_version(snippet, profile, "2026-10")
    assert later[-1]["facts"]["jev_input"] != bound


def test_resolve_postings_matches_the_per_posting_resolver_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from venator.qualify import jev_effective

    profile, posting, qualifications, _ = primed_store(tmp_path)
    snippet = {**fixture_posting(), "key": "p:snippet", "description_kind": "snippet"}
    postings = [posting, snippet]
    reads = {"rows": 0}
    real_rows = jev_effective.qualification_rows

    def counted(directory: Path) -> list[dict]:
        reads["rows"] += 1
        return real_rows(directory)

    monkeypatch.setattr(jev_effective, "qualification_rows", counted)
    batch = jev_effective.resolve_postings(
        postings, profile, as_of_month=AS_OF, qualifications_dir=qualifications, release=JEV_RELEASE
    )
    assert reads["rows"] == 1
    for item in postings:
        assert batch[item["key"]] == effective_jev_context(
            item, profile, as_of_month=AS_OF, release=JEV_RELEASE, qualifications_dir=qualifications
        )
    absent = tmp_path / "no-store"
    empty = jev_effective.resolve_postings(
        postings, profile, as_of_month=AS_OF, qualifications_dir=absent, release=JEV_RELEASE
    )
    assert not absent.exists()
    assert all(resolution.context is None for resolution in empty.values())
