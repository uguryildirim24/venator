"""Jev-mode Hard Filters, complete fallback, reserved facts and Dedup."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from venator.match.dedup import DUPLICATE_RULE
from venator.match.filters import apply_filters, shift_preference_reading
from venator.match.run import run
from venator.match.store import load_latest_decisions
from venator.profile.loader import load_profile
from venator.profile.schema import FilterPolicy, JevPolicy
from venator.qualify.jev_contract import JevContext, JevResolution
from venator.qualify.jev_effective import (
    FALLBACK_REASONS,
    effective_jev_context,
    label_fallback_reason,
)
from venator.qualify.jev_policy import prepare_jev_case
from venator.qualify.jev_release import JEV_RELEASE
from venator.qualify.store import JEV_KIND, append_event, append_rows, promotion_events
from venator.qualify.versions import event_id, jev_assessment_key

FIXTURE = Path(__file__).resolve().parents[1] / "qualify" / "fixtures" / "jev"
AS_OF = "2026-09"
AS_OF_DAY = "2026-09-13"
OWNED = (
    Path("src/venator/match/filters.py"),
    Path("src/venator/match/budget.py"),
    Path("src/venator/match/run.py"),
    Path("src/venator/match/store.py"),
    Path("src/venator/qualify/jev_effective.py"),
)
JEV_POLICY = JevPolicy(
    policy_version="v",
    restricted_roles="exclude",
    temporary_student_authorization_exclusion="review",
    no_sponsorship_student="retain",
    no_sponsorship_nonstudent="review",
    unmet_completed_degree="exclude",
    domains=("biochemistry",),
)
PHD = {
    "key": "p:phd",
    "title": "Laboratory Intern",
    "location": "Boston, MA",
    "description_kind": "full",
    "description_html": "<p>Minimum qualifications: PhD in biochemistry.</p>",
    "discovered_at": "2026-09-01T00:00:00+00:00",
}
WEEKEND = {
    "key": "p:weekend",
    "title": "Weekend Technician",
    "location": "Boston, MA",
    "description_kind": "full",
    "description_html": "<p>Laboratory support on a weekend shift.</p>",
}
NIGHT = {
    "key": "p:night",
    "title": "Research Associate",
    "location": "Boston, MA",
    "description_kind": "full",
    "description_html": "<p>This role includes a night shift in the core lab.</p>",
}
GYM = {
    "key": "p:gym",
    "title": "Research Associate",
    "location": "Boston, MA",
    "description_kind": "full",
    "description_html": "<p>Benefits include a gym that is open on weekends.</p>",
}


def make_context(**overrides: object) -> JevContext:
    values: dict[str, object] = {
        "posting_key": "p:phd",
        "posting_revision": "rev",
        "as_of_month": AS_OF,
        "input_version": "input-v",
        "qualifier_version": "jev:abc",
        "accepted_profile_hash": "accepted",
        "assessment_key": "assess-1",
        "response_sha256": "a" * 64,
        "decision": "prioritize",
        "fit_probability": 0.9,
        "fit_score": 0.85,
        "exclusions": (),
        "primary_rule": None,
        "review_flags": (),
        "diagnostic_flags": (),
    }
    values.update(overrides)
    return JevContext(**values)  # type: ignore[arg-type]


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


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


def success_row(bindings, profile_id: str, **extra: object) -> dict:
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
        "decision": extra.pop("decision", "prioritize"),
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


def test_no_preference_never_fires() -> None:
    policy = FilterPolicy(shift_preference="no_preference")
    assert shift_preference_reading(WEEKEND, policy) is None
    assert apply_filters(WEEKEND, policy).verdict == "pass"


@pytest.mark.parametrize(
    "preference,posting,rule",
    [
        ("no_weekends", WEEKEND, "shift"),
        ("weekdays_only", WEEKEND, "shift"),
        ("no_nights", NIGHT, "shift"),
        ("weekdays_only", NIGHT, "shift"),
    ],
)
def test_shift_preference_kills_excluded_shifts(preference: str, posting: dict, rule: str) -> None:
    policy = FilterPolicy(shift_preference=preference)
    reading = shift_preference_reading(posting, policy)
    assert reading is not None
    assert reading.verdict == "kill"
    assert reading.rule == rule
    assert apply_filters(posting, policy).rule == rule


def test_benefits_copy_about_weekends_does_not_fire() -> None:
    policy = FilterPolicy(shift_preference="no_weekends")
    reading = shift_preference_reading(GYM, policy)
    assert reading is not None
    assert reading.verdict == "pass"
    assert apply_filters(GYM, policy).verdict == "pass"


def test_jev_context_outside_jev_mode_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="only valid when qualification_mode is jev"):
        apply_filters(PHD, FilterPolicy(), jev=make_context())


def test_fallback_runs_education_and_labels_the_reason(tmp_path: Path) -> None:
    profile = load_profile(FIXTURE / "profile")
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    qualifications = tmp_path / "qualifications"
    write_jsonl(postings_dir / f"{AS_OF_DAY}.jsonl", [PHD])
    summary = run(
        postings_dir,
        decisions_dir,
        profile=profile,
        qualifications_dir=qualifications,
        as_of_month=AS_OF,
    )
    standing = load_latest_decisions(decisions_dir)[(PHD["key"], "hard_filter")]
    assert standing["verdict"] == "kill"
    assert standing["rule"] == "education_fit"
    assert "Jev unavailable (not_activated)" in standing["reason"]
    assert "deterministic fallback" in standing["reason"]
    assert standing["facts"] == {
        "jev": "fallback",
        "jev_input": prepare_jev_case(PHD, profile, AS_OF).case.bindings.input_version,  # type: ignore[union-attr]
    }
    assert summary["kill"] == 1


def test_jev_exclude_runs_after_operational_filters() -> None:
    profile = load_profile(Path(__file__).resolve().parents[2] / "profiles" / "example")
    policy = replace(profile.filters, qualification_mode="jev", jev=JEV_POLICY)
    intern = {
        "key": "p:ok",
        "title": "Research Associate",
        "location": "Boston, MA",
        "description_kind": "full",
        "description_html": "<p>A BS and two years of experience.</p>",
    }
    exclude = make_context(
        posting_key="p:ok",
        decision="exclude",
        primary_rule="restricted_role",
        assessment_key="assess-x",
    )
    killed = apply_filters(intern, policy, jev=exclude)
    assert killed.verdict == "kill"
    assert killed.rule == "jev_policy:restricted_role"
    assert dict(killed.facts) == {"jev": "assess-x", "jev_input": "input-v"}
    remote = {**intern, "location": "Salt Lake City, Utah", "description_html": "<p>On-site role.</p>"}
    remote_excluded = apply_filters(remote, policy, jev=exclude)
    assert remote_excluded.rule == "jev_policy:restricted_role"
    assert dict(remote_excluded.facts) == {"jev": "assess-x", "jev_input": "input-v"}


def test_old_education_kill_does_not_block_a_new_jev_pass(tmp_path: Path) -> None:
    profile = load_profile(FIXTURE / "profile")
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    qualifications = tmp_path / "qualifications"
    write_jsonl(postings_dir / f"{AS_OF_DAY}.jsonl", [PHD])
    first = run(
        postings_dir,
        decisions_dir,
        profile=profile,
        qualifications_dir=qualifications,
        as_of_month=AS_OF,
    )
    assert first["kill"] == 1
    prepared = prepare_jev_case(PHD, profile, AS_OF)
    assert prepared.case is not None
    bindings = prepared.case.bindings
    activate_jev(qualifications, profile.identifier, bindings)
    row = success_row(bindings, profile.identifier)
    append_rows(qualifications, profile.identifier, [row], day=AS_OF_DAY)
    second = run(
        postings_dir,
        decisions_dir,
        profile=profile,
        qualifications_dir=qualifications,
        as_of_month=AS_OF,
    )
    standing = load_latest_decisions(decisions_dir)[(PHD["key"], "hard_filter")]
    assert second["pass"] == 1
    assert standing["verdict"] == "pass"
    assert standing["facts"]["education_fit"] == "delegated"
    assert standing["facts"]["jev"] == row["assessment_key"]
    assert "Jev unavailable" not in standing["reason"]


def test_duplicate_kill_keeps_only_reserved_facts(tmp_path: Path) -> None:
    profile = load_profile(FIXTURE / "profile")
    identity = {
        "source": "greenhouse",
        "board": "one",
        "external_id": "42",
        "url": "https://example.test/job/42",
        "description_kind": "full",
        "listing_status": "open",
        "title": "Laboratory Intern",
        "description_html": (
            "<p>Summer internship for currently enrolled undergraduates. "
            "Bachelor's degree required.</p>"
        ),
    }
    winner = {**identity, "key": "greenhouse:one:42", "discovered_at": "2026-09-04T00:00:00+00:00"}
    loser = {**identity, "key": "greenhouse:one:old", "discovered_at": "2026-09-01T00:00:00+00:00"}
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    qualifications = tmp_path / "qualifications"
    write_jsonl(postings_dir / f"{AS_OF_DAY}.jsonl", [winner, loser])
    prepared = prepare_jev_case(winner, profile, AS_OF)
    assert prepared.case is not None
    activate_jev(qualifications, profile.identifier, prepared.case.bindings)
    rows = []
    for posting in (winner, loser):
        case = prepare_jev_case(posting, profile, AS_OF).case
        assert case is not None
        rows.append(success_row(case.bindings, profile.identifier))
    append_rows(qualifications, profile.identifier, rows, day=AS_OF_DAY)
    run(
        postings_dir,
        decisions_dir,
        profile=profile,
        qualifications_dir=qualifications,
        as_of_month=AS_OF,
    )
    latest = load_latest_decisions(decisions_dir)
    duplicate = [
        latest[(key, "hard_filter")]
        for key in (winner["key"], loser["key"])
        if latest[(key, "hard_filter")]["rule"] == DUPLICATE_RULE
    ]
    passed = [
        latest[(key, "hard_filter")]
        for key in (winner["key"], loser["key"])
        if latest[(key, "hard_filter")]["verdict"] == "pass"
    ]
    assert len(duplicate) == 1
    assert len(passed) == 1
    assert set(duplicate[0]["facts"]) == {"jev", "jev_input"}
    assert "education_fit" in passed[0]["facts"]
    assert passed[0]["facts"]["jev"] != "fallback"


def test_jev_activation_without_compiled_hashes_is_refused(tmp_path: Path) -> None:
    profile = load_profile(FIXTURE / "profile")
    posting = json.loads((FIXTURE / "posting.json").read_text(encoding="utf-8"))
    prepared = prepare_jev_case(posting, profile, AS_OF)
    assert prepared.case is not None
    bindings = prepared.case.bindings
    common = {
        "profile_id": profile.identifier,
        "profile_hash": bindings.accepted_profile_hash,
        "qualifier_version": bindings.qualifier_version,
        "qualifier_kind": JEV_KIND,
        "components": {},
        "finalization_sha256": "sha",
        "cohort_hashes": {},
        "at": "2026-09-13T00:00:00+00:00",
        "event": "finalized",
        "refers_to": None,
        "by": "finalize",
    }
    with pytest.raises(ValueError, match="compiled_profile_hash and policy_hash"):
        append_event(tmp_path / "qualifications", profile.identifier, common)


def test_checkpoint_activation_does_not_satisfy_jev_mode(tmp_path: Path) -> None:
    profile = load_profile(FIXTURE / "profile")
    posting = json.loads((FIXTURE / "posting.json").read_text(encoding="utf-8"))
    qualifications = tmp_path / "qualifications"
    common = {
        "profile_id": profile.identifier,
        "profile_hash": "checkpoint-hash",
        "qualifier_version": "aaaaaaaaaaaa",
        "components": {},
        "finalization_sha256": "sha",
        "cohort_hashes": {},
        "at": "2026-09-13T00:00:00+00:00",
    }
    # Replay an existing checkpoint event journal; new checkpoint writes are refused.
    events = []
    parent = None
    for kind, by in (("finalized", "finalize"), ("signed", "sample"), ("activated", "sample")):
        row = {**common, "event": kind, "refers_to": parent, "by": by}
        parent = event_id(row)
        events.append({**row, "event_id": parent})
    qualifications.mkdir()
    (qualifications / "promotions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8", newline="\n"
    )
    resolution = effective_jev_context(
        posting, profile, as_of_month=AS_OF, release=JEV_RELEASE, qualifications_dir=qualifications
    )
    assert resolution.context is None
    assert resolution.fallback_reason == "not_activated"


def test_shadow_only_and_wrong_profile_and_corrupt_rows(tmp_path: Path) -> None:
    profile = load_profile(FIXTURE / "profile")
    posting = json.loads((FIXTURE / "posting.json").read_text(encoding="utf-8"))
    prepared = prepare_jev_case(posting, profile, AS_OF)
    assert prepared.case is not None
    bindings = prepared.case.bindings
    qualifications = tmp_path / "qualifications"
    activate_jev(qualifications, profile.identifier, bindings)
    shadow = success_row(bindings, profile.identifier, mode="shadow")
    append_rows(qualifications, profile.identifier, [shadow], day=AS_OF_DAY)
    shadow_resolution = effective_jev_context(
        posting, profile, as_of_month=AS_OF, release=JEV_RELEASE, qualifications_dir=qualifications
    )
    assert shadow_resolution.fallback_reason == "shadow_only"
    promoted = success_row(bindings, profile.identifier, response_sha256="")
    with pytest.raises(ValueError, match="corrupt"):
        effective_jev_context(
            posting,
            profile,
            as_of_month=AS_OF,
            release=JEV_RELEASE,
            rows=[promoted],
            events=promotion_events(qualifications),
        )
    foreign = success_row(bindings, "someone-else")
    with pytest.raises(ValueError, match="names Profile"):
        effective_jev_context(
            posting, profile, as_of_month=AS_OF, release=JEV_RELEASE, rows=[foreign], events=[]
        )


def test_snippet_is_a_labelled_preparation_fallback() -> None:
    profile = load_profile(FIXTURE / "profile")
    posting = json.loads((FIXTURE / "posting.json").read_text(encoding="utf-8"))
    posting["description_kind"] = "snippet"
    resolution = effective_jev_context(posting, profile, as_of_month=AS_OF, release=JEV_RELEASE, rows=[], events=[])
    assert resolution.context is None
    assert resolution.fallback_reason == "snippet"
    assert "snippet" in FALLBACK_REASONS
    labelled = label_fallback_reason("passed education fit", resolution)
    assert labelled.endswith("Jev unavailable (snippet); deterministic fallback")


def test_matching_modules_make_no_http_calls() -> None:
    root = Path(__file__).resolve().parents[2]
    forbidden = ("typesafe.ai", "TYPESAFE_API_KEY", "httpx", "urllib.request")
    for relative in OWNED:
        text = (root / relative).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{relative} names {token}"


def test_a_settled_fallback_is_not_relabelled_or_appended_again(tmp_path: Path) -> None:
    profile = load_profile(FIXTURE / "profile")
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(postings_dir / f"{AS_OF_DAY}.jsonl", [PHD])
    for _ in range(3):
        run(postings_dir, decisions_dir, profile=profile,
            qualifications_dir=tmp_path / "qualifications", as_of_month=AS_OF)
    rows = [json.loads(line) for path in sorted(decisions_dir.glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["reason"].count("deterministic fallback") == 1
    resolution = JevResolution(None, "in", "not_activated")
    once = label_fallback_reason("passed", resolution)
    assert label_fallback_reason(once, resolution) == once
