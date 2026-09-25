"""Kind-aware qualification store: Jev rows beside legacy checkpoint rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import venator.qualify.store as qualification_store
from venator.qualify.store import (
    JEV_KIND,
    JEV_ROW_FIELDS,
    append_event,
    append_rows,
    effective_rows,
    fold_events,
    promotion_events,
    qualification_rows,
    qualifier_kind_of,
)
from venator.qualify.versions import jev_accepted_profile_hash, sha256_json, sha256_utf8

IDENTITY = {
    "kind": "typesafe_jev",
    "provider": "typesafe",
    "api_contract": "systemone-v1",
    "model_requested": "jev-1.13.0",
    "model_expected": "jev-1.13.0",
    "question_package": "jev-qualification-2",
    "questions_sha256": "d" * 64,
    "release_hash": "e" * 64,
}
PROFILE_HASH = "compiled-profile"
POLICY_HASH = "policy-digest"
ACCEPTED = jev_accepted_profile_hash(PROFILE_HASH, POLICY_HASH)
REQUEST = "a" * 64
RESPONSE = "b" * 64
QUALIFIER = "jev:" + "c" * 64
INPUT = "input-one"


def _assessment(*, request_key: str | None = REQUEST, response_sha256: str | None = RESPONSE) -> str:
    return sha256_json({
        "input_version": INPUT,
        "qualifier_version": QUALIFIER,
        "request_key": request_key,
        "response_sha256": response_sha256,
    })


def _jev_row(**changes: object) -> dict:
    row: dict = {
        "schema": "jev-assessment-2",
        "qualifier_kind": JEV_KIND,
        "posting_key": "p",
        "profile_id": "owner",
        "posting_revision": "rev",
        "profile_hash": PROFILE_HASH,
        "policy_hash": POLICY_HASH,
        "accepted_profile_hash": ACCEPTED,
        "as_of_month": "2026-09",
        "input_version": INPUT,
        "qualifier_version": QUALIFIER,
        "qualifier_identity": dict(IDENTITY),
        "assessment_key": _assessment(),
        "request_key": REQUEST,
        "state_sha256": "f" * 64,
        "questions_sha256": "d" * 64,
        "mode": "shadow",
        "decision": "prioritize",
        "fit_probability": 0.85,
        "fit_score": 0.8,
        "exclusions": [],
        "primary_rule": None,
        "review_flags": [],
        "diagnostic_flags": [],
        "response_validation": {"passed": True, "version": "1", "error_codes": []},
        "response_sha256": RESPONSE,
        "response_ref": f"jev/responses/{REQUEST}.json",
        "model_returned": "jev-1.13.0",
        "usage": {"input_tokens": 12, "output_tokens": 0},
        "latency_ms": 15.0,
        "decided_at": "2026-09-18T00:00:00+00:00",
        "cache_hit": False,
        "reason": None,
    }
    row.update(changes)
    if "assessment_key" not in changes:
        row["assessment_key"] = _assessment(
            request_key=row["request_key"],
            response_sha256=row["response_sha256"],
        )
    return row


def _unassessed(reason: str = "transport_failed", **changes: object) -> dict:
    return _jev_row(
        decision="unassessed",
        reason=reason,
        fit_probability=None,
        fit_score=None,
        exclusions=[],
        primary_rule=None,
        response_validation=None,
        response_sha256=None,
        response_ref=None,
        model_returned=None,
        usage=None,
        latency_ms=None,
        request_key=None,
        state_sha256=None,
        questions_sha256=None,
        **changes,
    )


def _jev_event(kind: str, *, refers_to: str | None = None, qualifier: str = QUALIFIER) -> dict:
    return {
        "event": kind,
        "profile_id": "owner",
        "profile_hash": ACCEPTED,
        "compiled_profile_hash": PROFILE_HASH,
        "policy_hash": POLICY_HASH,
        "qualifier_version": qualifier,
        "qualifier_kind": JEV_KIND,
        "components": {},
        "finalization_sha256": "sha",
        "cohort_hashes": {},
        "refers_to": refers_to,
        "at": "2026-09-13T00:00:00+00:00",
        "by": "test",
    }


def _activate_jev(directory: Path, qualifier: str = QUALIFIER) -> str:
    finalized = append_event(directory, "owner", _jev_event("finalized", qualifier=qualifier))
    signed = append_event(directory, "owner", _jev_event("signed", refers_to=finalized, qualifier=qualifier))
    return append_event(directory, "owner", _jev_event("activated", refers_to=signed, qualifier=qualifier))


def test_closed_schema_lists_every_required_field() -> None:
    assert set(_jev_row()) == set(JEV_ROW_FIELDS)


def test_new_writes_are_jev_but_historical_checkpoint_rows_still_load(tmp_path: Path) -> None:
    checkpoint = {
        "posting_key": "p", "profile_id": "owner", "mode": "shadow",
        "decision": "unassessed", "reason": "missing", "qualifier_version": "old",
        "input_version": "one",
    }
    with pytest.raises(ValueError, match="must be Jev"):
        append_rows(tmp_path, "owner", [checkpoint], day="2026-09-18")
    path = tmp_path / "2026-09-18.jsonl"
    path.write_text(json.dumps(checkpoint) + "\n", encoding="utf-8")
    loaded = qualification_rows(tmp_path)
    assert loaded == [checkpoint]
    assert qualifier_kind_of(loaded[0]) == "checkpoint"


def test_identical_success_is_noop_and_conflict_is_refused(tmp_path: Path) -> None:
    first = _jev_row(decided_at="2026-09-18T00:00:00+00:00", cache_hit=False)
    assert append_rows(tmp_path, "owner", [first], day="2026-09-18") == 1
    again = _jev_row(decided_at="2026-09-18T01:00:00+00:00", cache_hit=True)
    assert append_rows(tmp_path, "owner", [again], day="2026-09-18") == 0
    assert len(qualification_rows(tmp_path)) == 1
    conflict = _jev_row(fit_score=0.1, decided_at="2026-09-18T02:00:00+00:00")
    with pytest.raises(ValueError, match="conflicting"):
        append_rows(tmp_path, "owner", [conflict], day="2026-09-18")
    assert qualification_rows(tmp_path)[0]["fit_score"] == 0.8


@pytest.mark.parametrize("field", ["fit_probability", "fit_score"])
def test_a_success_scored_outside_the_unit_interval_is_refused(tmp_path: Path, field: str) -> None:
    """The view's jev_triage CHECK requires [0, 1]; a row outside it stops every rebuild."""
    with pytest.raises(ValueError, match=r"must lie in \[0, 1\]"):
        append_rows(tmp_path, "owner", [_jev_row(**{field: 1.25})], day="2026-09-18")
    assert qualification_rows(tmp_path) == []


def test_daily_qualification_rows_split_before_the_file_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qualification_store, "QUALIFICATION_PART_MAX_BYTES", 4_000)
    rows = [
        _unassessed(posting_key=f"p-{number}")
        for number in range(6)
    ]

    assert append_rows(tmp_path, "owner", rows, day="2026-09-18") == len(rows)

    paths = sorted(tmp_path.glob("*.jsonl"))
    assert len(paths) > 1
    assert paths[0].name == "2026-09-18.jsonl"
    assert all(path.stat().st_size <= qualification_store.QUALIFICATION_PART_MAX_BYTES for path in paths)
    assert [row["posting_key"] for row in qualification_rows(tmp_path)] == [
        row["posting_key"] for row in rows
    ]


def test_failure_then_success_appends_and_reads_prefer_success(tmp_path: Path) -> None:
    failed = _unassessed("transport_failed", decided_at="2026-09-18T00:00:00+00:00")
    assert append_rows(tmp_path, "owner", [failed], day="2026-09-18") == 1
    success = _jev_row(decided_at="2026-09-18T00:01:00+00:00")
    assert append_rows(tmp_path, "owner", [success], day="2026-09-18") == 1
    later_fail = _unassessed("response_invalid", decided_at="2026-09-18T00:02:00+00:00")
    assert append_rows(tmp_path, "owner", [later_fail], day="2026-09-18") == 0
    rows = qualification_rows(tmp_path)
    assert len(rows) == 2
    effective = effective_rows(rows, profile_id="owner", mode="shadow", qualifier_version=QUALIFIER,
                               input_versions={"p": INPUT}, qualifier_kind=JEV_KIND)
    assert effective["p"]["decision"] == "prioritize"


def test_failed_case_is_visible_when_no_success(tmp_path: Path) -> None:
    failed = _unassessed("budget_exhausted")
    append_rows(tmp_path, "owner", [failed], day="2026-09-18")
    effective = effective_rows(qualification_rows(tmp_path), profile_id="owner", mode="shadow",
                               qualifier_version=QUALIFIER, input_versions={"p": INPUT},
                               qualifier_kind=JEV_KIND)
    assert effective["p"]["decision"] == "unassessed"
    assert effective["p"]["reason"] == "budget_exhausted"


def test_shadow_and_promoted_are_separate(tmp_path: Path) -> None:
    append_rows(tmp_path, "owner", [_jev_row(mode="shadow")], day="2026-09-18")
    _activate_jev(tmp_path)
    promoted = _jev_row(mode="promoted", decided_at="2026-09-18T00:02:00+00:00")
    assert append_rows(tmp_path, "owner", [promoted], day="2026-09-18") == 1
    rows = qualification_rows(tmp_path)
    shadow = effective_rows(rows, profile_id="owner", mode="shadow", qualifier_version=QUALIFIER,
                            input_versions={"p": INPUT}, qualifier_kind=JEV_KIND)
    live = effective_rows(rows, profile_id="owner", mode="promoted", qualifier_version=QUALIFIER,
                          input_versions={"p": INPUT}, qualifier_kind=JEV_KIND)
    assert shadow["p"]["mode"] == "shadow"
    assert live["p"]["mode"] == "promoted"


def test_promoted_jev_row_refused_without_jev_activation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="active, non-stale"):
        append_rows(tmp_path, "owner", [_jev_row(mode="promoted")], day="2026-09-18")
    assert qualification_rows(tmp_path) == []


def test_jev_revoke_does_not_restore_an_earlier_pointer(tmp_path: Path) -> None:
    jev = _activate_jev(tmp_path)
    state = fold_events(promotion_events(tmp_path), "owner")
    assert state.current(ACCEPTED, qualifier_kind=JEV_KIND) is not None
    append_event(tmp_path, "owner", {
        "event": "revoked", "profile_id": "owner", "profile_hash": ACCEPTED,
        "compiled_profile_hash": PROFILE_HASH, "policy_hash": POLICY_HASH,
        "qualifier_version": QUALIFIER, "qualifier_kind": JEV_KIND, "components": {},
        "finalization_sha256": "sha", "cohort_hashes": {}, "refers_to": jev,
        "at": "2026-09-13T00:00:01+00:00", "by": "sample",
    })
    state = fold_events(promotion_events(tmp_path), "owner")
    assert state.active is None


def test_jev_inherited_is_refused(tmp_path: Path) -> None:
    activated = _activate_jev(tmp_path)
    with pytest.raises(ValueError, match="inherited"):
        append_event(tmp_path, "owner", {
            **_jev_event("inherited", refers_to=activated),
            "profile_hash": jev_accepted_profile_hash("new-compiled", POLICY_HASH),
            "compiled_profile_hash": "new-compiled",
        })


def test_wrong_acceptance_hash_is_refused(tmp_path: Path) -> None:
    row = _jev_row(accepted_profile_hash=sha256_utf8("nope"))
    with pytest.raises(ValueError, match="accepted_profile_hash"):
        append_rows(tmp_path, "owner", [row], day="2026-09-18")
