"""A view may recommend only the exact job/profile version that was assessed."""
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from venator.discover.store import posting_revision
from venator.match.store import filters_version
from venator.profile import Profile
from venator.view.build import build_database


def test_refreshed_content_invalidates_old_decision_and_preserves_source_health(tmp_path: Path, monkeypatch) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    profile = Profile(name="candidate", directory=profile_dir, resume={"skills": ["Python"]})
    postings, decisions, track = [tmp_path / name for name in ("postings", "decisions", "track")]
    postings.mkdir()
    decisions.mkdir()
    job = {"key": "greenhouse:acme:1", "source": "greenhouse", "board": "acme", "title": "Python developer",
           "location": "Boston, MA", "description_html": "Python experience required", "description_kind": "full",
           "listing_status": "open", "last_verified_at": "2026-09-04T00:00:00Z", "opportunity_type": "job"}
    decision = {"posting_key": job["key"], "stage": "hard_filter", "verdict": "pass", "rule": None,
                "reason": "passed", "facts": {}, "decided_at": "2026-09-04T00:00:00Z",
                "posting_version": posting_revision(job), "filters_version": filters_version(profile.constraints_path, profile.targeting_path)}
    (decisions / "2026-09-04.jsonl").write_text(json.dumps(decision) + "\n")
    source_health = {"greenhouse:acme": {"status": "partial", "count": 1, "message": "one detail unavailable"}}
    (postings / "source-health.json").write_text(json.dumps(source_health))
    output = tmp_path / "venator.db"

    def rebuild(value: dict) -> tuple:
        (postings / "2026-09-04.jsonl").write_text(json.dumps(value) + "\n")
        build_database(postings, decisions, output, track, tmp_path / "runs.jsonl", profile=profile)
        with sqlite3.connect(output) as db:
            assert db.execute("SELECT status FROM source_health").fetchone() == ("partial",)
            return db.execute("SELECT status, unknowns FROM assessments").fetchone()

    assert rebuild(job)[0] == "suitable"
    from venator.view import build as view_module
    assess = view_module.assess_posting
    with monkeypatch.context() as patch:
        def unnecessary_reassessment(*args, **kwargs):
            raise AssertionError("An unchanged job should reuse its current assessment")
        patch.setattr(view_module, "assess_posting", unnecessary_reassessment)
        assert rebuild({**job, "last_verified_at": "2026-09-05T00:00:00Z"})[0] == "suitable"
    assert view_module.assess_posting is assess
    changed = rebuild({**job, "description_html": "Python and Rust experience required"})
    assert changed[0] == "unassessed"
    assert "current hard-filter decision is unavailable" in changed[1]
    assert rebuild({**job, "listing_status": "closed"})[0] == "not_suitable"

    # The same job and filter rules cannot reuse evidence from a former resume.
    profile = replace(profile, resume={"skills": ["Rust"]})
    assert rebuild(job)[0] == "needs_review"
    profile = replace(profile, resume={"skills": ["Python"]})
    assert rebuild(job)[0] == "suitable"
    # Verification updates deliberately do not change posting_revision.
    assert rebuild({**job, "verification_status": "unknown"})[0] == "needs_review"
    assert rebuild({**job, "verification_status": "verified"})[0] == "suitable"
    assert rebuild({**job, "last_verified_at": None})[0] == "needs_review"
