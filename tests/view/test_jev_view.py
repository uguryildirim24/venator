"""Jev triage in the disposable view: DDL, states, and no Jev-only suitable status."""

from __future__ import annotations

import ast
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from venator.discover.store import posting_revision
from venator.profile.loader import load_profile
from venator.qualify.compile import compile_profile
from venator.qualify.jev_effective import current_filter_version, jev_promotion_state
from venator.qualify.jev_release import JEV_RELEASE
from venator.qualify.store import (
    JEV_KIND,
    JEV_SCHEMA,
    append_event,
    append_rows,
    promotion_events,
)
from venator.qualify.versions import jev_accepted_profile_hash, jev_assessment_key, jev_policy_hash
from venator.view.build import SCHEMA, build_database
from venator.view.jev import ASSESSMENTS_DDL, JEV_TRIAGE_DDL, is_jev_row, jev_view_bindings, materialize_triage
from venator.view.verify import verify_dashboard

JEV = Path(__file__).parents[1] / "qualify/fixtures/jev"
CONTRACTS = Path(__file__).resolve().parents[2] / "coordination" / "CONTRACTS.md"
REQUEST = "a" * 64
RESPONSE = "b" * 64
STATE = "f" * 64


def _reduced(name: str) -> dict:
    payload = json.loads((JEV / f"reduced_{name}.json").read_text(encoding="utf-8"))
    payload.pop("raw_variant", None)
    return payload


def _posting() -> dict:
    posting = json.loads((JEV / "posting.json").read_text(encoding="utf-8"))
    posting.update({
        "source": "greenhouse",
        "board": "jevlab",
        "url": "https://example.test/jev-lab-intern",
        "location": "Remote",
        "last_verified_at": "2026-09-13T00:00:00+00:00",
        "verification_status": "verified",
        "opportunity_type": "job",
        "discovered_at": "2026-09-13T00:00:00+00:00",
    })
    return posting


def _jev_event(kind: str, profile: object, compiled: object, policy_hash: str, accepted: str,
               *, refers_to: str | None = None) -> dict:
    return {
        "event": kind,
        "profile_id": profile.identifier,
        "profile_hash": accepted,
        "compiled_profile_hash": compiled.profile_hash,
        "policy_hash": policy_hash,
        "qualifier_version": JEV_RELEASE.qualifier_version,
        "qualifier_kind": JEV_KIND,
        "components": {},
        "finalization_sha256": "sha",
        "cohort_hashes": {},
        "refers_to": refers_to,
        "at": "2026-09-13T00:00:00+00:00",
        "by": "finalize" if kind == "finalized" else "sample",
    }


def _activation(directory: Path, profile: object, compiled: object) -> None:
    policy_hash = jev_policy_hash(profile.filters.jev)
    accepted = jev_accepted_profile_hash(compiled.profile_hash, policy_hash)
    finalized = append_event(directory, profile.identifier, _jev_event(
        "finalized", profile, compiled, policy_hash, accepted))
    signed = append_event(directory, profile.identifier, _jev_event(
        "signed", profile, compiled, policy_hash, accepted, refers_to=finalized))
    append_event(directory, profile.identifier, _jev_event(
        "activated", profile, compiled, policy_hash, accepted, refers_to=signed))


def _jev_row(posting: dict, profile: object, compiled: object, *, mode: str, reduced: dict,
             month: str = "2026-09", qualifier: str | None = None,
             revision: str | None = None, reason: str | None = None,
             input_version: str | None = None) -> dict:
    success = reduced["decision"] in {"prioritize", "review", "exclude"}
    request_key = REQUEST if success else None
    response_sha256 = RESPONSE if success else None
    version = qualifier if qualifier is not None else JEV_RELEASE.qualifier_version
    row = {
        "schema": JEV_SCHEMA,
        "qualifier_kind": JEV_KIND,
        "posting_key": posting["key"],
        "profile_id": profile.identifier,
        "posting_revision": revision if revision is not None else posting_revision(posting),
        "profile_hash": compiled.profile_hash,
        "policy_hash": jev_policy_hash(profile.filters.jev),
        "accepted_profile_hash": jev_accepted_profile_hash(
            compiled.profile_hash, jev_policy_hash(profile.filters.jev),
        ),
        "as_of_month": month,
        "input_version": input_version or "input-current",
        "qualifier_version": version,
        "qualifier_identity": dict(JEV_RELEASE.qualifier_identity),
        "assessment_key": "",
        "request_key": request_key,
        "state_sha256": STATE if success else None,
        "questions_sha256": JEV_RELEASE.questions_sha256 if success else None,
        "mode": mode,
        "decision": reduced["decision"],
        "fit_probability": reduced["fit_probability"],
        "fit_score": reduced["fit_score"],
        "exclusions": reduced["exclusions"],
        "primary_rule": reduced["primary_rule"],
        "review_flags": reduced["review_flags"],
        "diagnostic_flags": reduced["diagnostic_flags"],
        "response_validation": (
            {"passed": True, "version": "1", "error_codes": []} if success else None
        ),
        "response_sha256": response_sha256,
        "response_ref": f"jev/responses/{REQUEST}.json" if success else None,
        "model_returned": "jev-1.13.0" if success else None,
        "usage": {"input_tokens": 12, "output_tokens": 0} if success else None,
        "latency_ms": 15.0 if success else None,
        "decided_at": "2026-09-13T00:03:00+00:00",
        "cache_hit": False,
        "reason": reason,
    }
    row["assessment_key"] = jev_assessment_key(
        input_version_value=row["input_version"],
        qualifier_version_value=row["qualifier_version"],
        request_key=row["request_key"],
        response_sha256=row["response_sha256"],
    )
    return row


def _setup(tmp_path: Path, *, decision: str = "prioritize", active: bool = True,
           listing: str = "open", hard_verdict: str = "pass",
           jev: bool = True, stale_jev: bool = False, failed_jev: bool = False,
           track_event: dict | None = None,
           hard_facts: dict | None = None,
           posting_changes: dict | None = None) -> tuple[dict, sqlite3.Connection]:
    profile_dir = tmp_path / "profile"
    shutil.copytree(JEV / "profile", profile_dir)
    profile = load_profile(profile_dir)
    postings = tmp_path / "postings"
    postings.mkdir()
    posting = _posting()
    posting.update(posting_changes or {})
    posting["listing_status"] = listing
    (postings / "2026-09-13.jsonl").write_text(json.dumps(posting) + "\n")
    qualifications = tmp_path / "qualifications"
    compiled = compile_profile(profile, as_of_month="2026-09")
    if active:
        _activation(qualifications, profile, compiled)
    state = jev_promotion_state(promotion_events(qualifications), profile.identifier)
    version = current_filter_version(profile, state, JEV_RELEASE)
    row = {"posting_key": posting["key"], "stage": "hard_filter", "verdict": hard_verdict,
           "rule": "location" if hard_verdict == "kill" else None,
           "reason": "Location outside target" if hard_verdict == "kill" else "passed",
           "facts": {"location": "remote"},
           "profile_id": profile.identifier, "filters_version": version,
           "posting_version": posting_revision(posting), "decided_at": "2026-09-13T00:00:00+00:00"}
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    track = tmp_path / "track"
    track.mkdir()
    if track_event is not None:
        (track / "2026-09-13.jsonl").write_text(json.dumps(track_event) + "\n")
    if jev:
        _, versions = jev_view_bindings(profile, compiled, [posting], "2026-09")
        current_input = versions[posting["key"]]
        month = "2026-08" if stale_jev else "2026-09"
        revision = "old-revision" if stale_jev else posting_revision(posting)
        if failed_jev:
            promoted_reduced = _reduced("unassessed")
            promoted_reason = "transport_failed"
            promoted_input = current_input
        else:
            promoted_reduced = _reduced(decision)
            promoted_reason = None
            promoted_input = "input-stale" if stale_jev else current_input
        record = _jev_row(
            posting, profile, compiled, mode="promoted", reduced=promoted_reduced,
            month=month, revision=revision, reason=promoted_reason,
            input_version=promoted_input,
        )
        shadow = _jev_row(
            posting, profile, compiled, mode="shadow", reduced=_reduced("review"),
            month=month, revision=revision, input_version=promoted_input,
        )
        append_rows(qualifications, profile.identifier, [record, shadow], day="2026-09-13")
        # The reserved pair match.run would have stamped against this store.
        current_success = active and not (stale_jev or failed_jev)
        row["facts"] = {
            **row["facts"],
            "jev": record["assessment_key"] if current_success else "fallback",
            "jev_input": current_input,
        }
    if hard_facts is not None:
        row["facts"] = hard_facts
    (decisions / "2026-09-13.jsonl").write_text(json.dumps(row) + "\n")
    build_database(postings, tmp_path / "decisions", tmp_path / "view.db", track,
                   tmp_path / "runs.jsonl", profile=profile, qualifications_dir=qualifications,
                   as_of_month="2026-09")
    connection = sqlite3.connect(tmp_path / "view.db")
    connection.row_factory = sqlite3.Row
    return posting, connection


def test_jev_triage_ddl_matches_contracts() -> None:
    contracts = CONTRACTS.read_text()
    assert JEV_TRIAGE_DDL in contracts
    assert ASSESSMENTS_DDL in SCHEMA
    assert JEV_TRIAGE_DDL in SCHEMA


def test_view_materializes_jev_skip_reasons_from_selection_inputs(tmp_path: Path) -> None:
    samples = (
        ({"description_kind": "missing", "description_html": ""}, "missing"),
        ({"description_kind": "snippet"}, "snippet"),
    )
    for index, (changes, expected) in enumerate(samples):
        posting, db = _setup(tmp_path / str(index), jev=False, posting_changes=changes)
        try:
            assert db.execute("SELECT reason FROM jev_skip WHERE posting_key = ?", (posting["key"],)).fetchone()[0] == expected
        finally:
            db.close()
    _, closed = _setup(tmp_path / "closed", jev=False, listing="closed")
    try:
        assert closed.execute("SELECT count(*) FROM jev_skip").fetchone()[0] == 0
    finally:
        closed.close()


def test_release_selection_does_not_depend_on_current_result_count(tmp_path: Path) -> None:
    _, promoted = _setup(tmp_path / "promoted", failed_jev=True)
    assert promoted.execute("SELECT mode FROM jev_selection").fetchone()[0] == "promoted"
    assert promoted.execute("SELECT state FROM jev_triage WHERE mode = 'promoted'").fetchone()[0] == "unavailable"
    promoted.close()

    _, shadow = _setup(tmp_path / "shadow", active=False, jev=False)
    assert shadow.execute("SELECT mode FROM jev_selection").fetchone()[0] == "shadow"
    shadow.close()


def test_current_priority_keeps_resume_evidence_separate(tmp_path: Path) -> None:
    expected = _reduced("prioritize")
    posting, db = _setup(tmp_path, decision="prioritize")
    assessment = dict(db.execute("SELECT * FROM assessments").fetchone())
    assert assessment["status"] == "suitable"
    assert assessment["assessed_by"] == "deterministic"
    triage = dict(db.execute(
        "SELECT * FROM jev_triage WHERE posting_key = ? AND mode = 'promoted'",
        (posting["key"],),
    ).fetchone())
    assert triage["state"] == "current"
    assert triage["decision"] == "prioritize"
    assert triage["fit_probability"] == pytest.approx(expected["fit_probability"])
    assert triage["fit_score"] == pytest.approx(expected["fit_score"])
    assert triage["primary_rule"] is None
    db.close()


def test_current_review_and_exclude(tmp_path: Path) -> None:
    expected = _reduced("review")
    posting, db = _setup(tmp_path, decision="review")
    assessment = dict(db.execute("SELECT * FROM assessments").fetchone())
    assert assessment["status"] == "suitable"
    assert assessment["assessed_by"] == "deterministic"
    triage = dict(db.execute(
        "SELECT * FROM jev_triage WHERE posting_key = ? AND mode = 'promoted'",
        (posting["key"],),
    ).fetchone())
    assert json.loads(triage["review_flags"]) == expected["review_flags"]
    db.close()


def test_current_exclude(tmp_path: Path) -> None:
    expected = _reduced("exclude")
    posting, db = _setup(tmp_path, decision="exclude")
    assessment = dict(db.execute("SELECT * FROM assessments").fetchone())
    assert assessment["status"] == "suitable"
    assert assessment["assessed_by"] == "deterministic"
    triage = dict(db.execute(
        "SELECT * FROM jev_triage WHERE posting_key = ? AND mode = 'promoted'",
        (posting["key"],),
    ).fetchone())
    assert triage["primary_rule"] == expected["primary_rule"]
    assert json.loads(triage["exclusions"]) == expected["exclusions"]
    db.close()


def test_operational_kill_stays_deterministic(tmp_path: Path) -> None:
    _, db = _setup(tmp_path, hard_verdict="kill", decision="prioritize")
    assessment = dict(db.execute("SELECT * FROM assessments").fetchone())
    assert assessment["status"] == "not_suitable"
    assert assessment["assessed_by"] == "deterministic"
    db.close()


def test_stale_jev_uses_deterministic_fallback_and_stale_badge(tmp_path: Path) -> None:
    posting, db = _setup(tmp_path, stale_jev=True)
    assessment = dict(db.execute("SELECT * FROM assessments").fetchone())
    assert assessment["assessed_by"] == "deterministic"
    triage = dict(db.execute(
        "SELECT * FROM jev_triage WHERE posting_key = ? AND mode = 'promoted'",
        (posting["key"],),
    ).fetchone())
    assert triage["state"] == "stale"
    assert triage["decision"] == "prioritize"
    assert triage["fit_probability"] is not None
    db.close()


def test_current_failure_is_unavailable_not_old_success(tmp_path: Path) -> None:
    posting, db = _setup(tmp_path, failed_jev=True)
    triage = dict(db.execute(
        "SELECT * FROM jev_triage WHERE posting_key = ? AND mode = 'promoted'",
        (posting["key"],),
    ).fetchone())
    assert triage["state"] == "unavailable"
    assert triage["decision"] == "unassessed"
    assert triage["fit_probability"] is None
    assert triage["reason"] == "transport_failed"
    assessment = dict(db.execute("SELECT * FROM assessments").fetchone())
    assert assessment["assessed_by"] == "deterministic"
    db.close()


def test_verify_dashboard_refuses_jev_only_suitable(tmp_path: Path) -> None:
    posting, db = _setup(tmp_path, decision="prioritize")
    db.execute("UPDATE assessments SET status = 'suitable', assessed_by = 'jev'")
    latest = {posting["key"]: {"verdict": "pass", "filters_version": "x",
                               "posting_version": posting_revision(posting), "facts": {}}}
    with pytest.raises(ValueError, match="Jev cannot publish a suitable status"):
        verify_dashboard(db, [posting], latest, "x", jev_mode=True)
    db.close()


def test_source_coverage_and_manual_track_events_are_unchanged(tmp_path: Path) -> None:
    posting, db = _setup(tmp_path)
    db.close()
    track_path = tmp_path / "track" / "2026-09-13.jsonl"
    track_path.write_text(json.dumps({
        "posting_key": posting["key"], "event": "approve", "actor": "owner",
        "detail": None, "at": "2026-09-13T12:00:00+00:00",
    }) + "\n")
    profile = load_profile(tmp_path / "profile")
    build_database(
        tmp_path / "postings", tmp_path / "decisions", tmp_path / "view.db", tmp_path / "track",
        tmp_path / "runs.jsonl", profile=profile, qualifications_dir=tmp_path / "qualifications",
        as_of_month="2026-09",
    )
    with sqlite3.connect(tmp_path / "view.db") as connection:
        known, verified = connection.execute(
            "SELECT known_jobs, full_verified_details FROM source_health"
        ).fetchone()
        assert known == 1
        events = list(connection.execute("SELECT event FROM track_events"))
        assert events == [("approve",)]
        state = connection.execute("SELECT state FROM application_states").fetchone()[0]
        assert state == "approved"


def test_view_modules_make_no_network_request() -> None:
    root = Path(__file__).resolve().parents[2] / "src/venator/view"
    banned = ("urllib", "http.client", "httpx", "requests", "aiohttp", "typesafe")
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(
                        alias.name == name or alias.name.startswith(f"{name}.") for name in banned
                    ), path
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not any(
                    node.module == name or node.module.startswith(f"{name}.") for name in banned
                ), path


def test_materialize_placeholder_when_no_jev_rows() -> None:
    posting = {"key": "greenhouse:x:1"}
    rows = materialize_triage(
        [posting],
        [],
        profile_id="p",
        as_of_month="2026-09",
        posting_revisions={"greenhouse:x:1": "rev"},
        promoted_qualifier="jev:abc",
        shadow_qualifier=None,
    )
    assert len(rows) == 2
    assert {row[1] for row in rows} == {"promoted", "shadow"}
    assert all(row[2] == "unavailable" and row[3] == "unassessed" and row[4] is None for row in rows)


def test_materialize_ignores_a_result_from_another_profile() -> None:
    posting = {"key": "greenhouse:x:1"}
    foreign = {
        "schema": JEV_SCHEMA, "qualifier_kind": JEV_KIND,
        "posting_key": posting["key"], "profile_id": "other", "mode": "shadow",
        "decision": "prioritize", "response_validation": {"passed": True},
        "fit_probability": 0.8, "fit_score": 0.8, "assessment_key": "ak",
        "qualifier_version": "jev:abc", "input_version": "input",
        "as_of_month": "2026-09", "posting_revision": "rev",
    }
    rows = materialize_triage(
        [posting], [foreign], profile_id="p", as_of_month="2026-09",
        posting_revisions={posting["key"]: "rev"}, promoted_qualifier=None,
        shadow_qualifier="jev:abc", input_versions={posting["key"]: "input"},
    )
    assert all(row[2] == "unavailable" for row in rows)


def test_is_jev_row_uses_r1_store_kind() -> None:
    assert not is_jev_row({"schema": JEV_SCHEMA, "qualifier_kind": "checkpoint"})
    assert is_jev_row({"schema": JEV_SCHEMA, "qualifier_kind": JEV_KIND})


def test_a_policy_edit_leaves_the_old_triage_stale_not_current(tmp_path: Path) -> None:
    """``input_version`` binds the Profile and policy. A row written before an
    edit must not stay current after it, in either mode."""
    posting, db = _setup(tmp_path, decision="prioritize")
    before = {row["mode"]: row["state"] for row in db.execute("SELECT mode, state FROM jev_triage")}
    db.close()
    assert before == {"promoted": "current", "shadow": "current"}
    targeting = tmp_path / "profile" / "targeting.yaml"
    text = targeting.read_text(encoding="utf-8")
    assert "unmet_completed_degree: exclude" in text
    targeting.write_text(text.replace("unmet_completed_degree: exclude", "unmet_completed_degree: review"),
                         encoding="utf-8")
    profile = load_profile(tmp_path / "profile")
    build_database(tmp_path / "postings", tmp_path / "decisions", tmp_path / "view.db", tmp_path / "track",
                   tmp_path / "runs.jsonl", profile=profile, qualifications_dir=tmp_path / "qualifications",
                   as_of_month="2026-09")
    with sqlite3.connect(tmp_path / "view.db") as after_db:
        after = {mode: state for mode, state in after_db.execute("SELECT mode, state FROM jev_triage")}
    assert after == {"promoted": "stale", "shadow": "stale"}


def test_a_new_jev_answer_is_not_combined_with_an_old_hard_decision(tmp_path: Path) -> None:
    """The hard pass was decided on the deterministic fallback; a Jev success
    arrived afterwards. Until match.run refreshes it, the view says so."""
    _, versions_db = _setup(tmp_path / "bound", decision="prioritize")
    bound = dict(versions_db.execute("SELECT * FROM assessments").fetchone())
    versions_db.close()
    assert bound["assessed_by"] == "deterministic"
    _, db = _setup(tmp_path / "old", decision="prioritize",
                   hard_facts={"location": "remote", "jev": "fallback", "jev_input": "unavailable"})
    assessment = dict(db.execute("SELECT * FROM assessments").fetchone())
    db.close()
    assert assessment["assessed_by"] == "deterministic"
