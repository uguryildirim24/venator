import json

from venator.discover.store import posting_revision
from venator.match.run import run
from venator.match.store import filters_version, load_latest_decisions
from venator.profile import load_profile


def test_job_content_change_rechecks_even_when_profile_did_not_change(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "targeting.yaml").write_text("profile:\n  name: test\n", encoding="utf-8")
    profile = load_profile(profile_dir)
    postings = tmp_path / "postings"
    postings.mkdir()
    decisions = tmp_path / "decisions"
    path = postings / "2026-09-05.jsonl"
    job = {"key": "lever:example:one", "source": "lever", "board": "example", "title": "Lab intern", "location": "Boston, MA", "description_html": "Open to undergraduates."}
    path.write_text(json.dumps(job) + "\n", encoding="utf-8")
    assert run(postings, decisions, profile=profile)["processed"] == 1
    assert run(postings, decisions, profile=profile)["processed"] == 0
    changed = {**job, "description_html": "A completed doctorate is required."}
    path.write_text(json.dumps(job) + "\n" + json.dumps(changed) + "\n", encoding="utf-8")
    assert run(postings, decisions, profile=profile)["processed"] == 1
    latest = load_latest_decisions(decisions)[(job["key"], "hard_filter")]
    assert latest["posting_version"] == posting_revision(changed)
    assert run(postings, decisions, profile=profile)["processed"] == 0


def test_verification_time_does_not_change_content_revision_but_closure_does():
    job = {"key": "test:one", "listing_status": "open", "last_verified_at": "2026-09-04T12:00:00Z"}
    assert posting_revision(job) == posting_revision({**job, "last_verified_at": "2026-09-05T12:00:00Z"})
    assert posting_revision(job) != posting_revision({**job, "listing_status": "closed"})


def test_profile_resume_change_invalidates_existing_eligibility(tmp_path):
    constraints = tmp_path / "constraints.yaml"
    targeting = tmp_path / "targeting.yaml"
    resume = tmp_path / "resume.yaml"
    constraints.write_text("{}\n", encoding="utf-8")
    targeting.write_text("{}\n", encoding="utf-8")
    resume.write_text("education: []\n", encoding="utf-8")
    before = filters_version(constraints, targeting)
    resume.write_text("education:\n  - degree: Bachelor of Science\n    date: May 2027\n", encoding="utf-8")
    assert filters_version(constraints, targeting) != before
