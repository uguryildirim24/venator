"""Exercise discovery, filtering and validated publication as one Refresh."""
import json
import sqlite3
import pytest

from venator.discover.adapters import PostingBatch, fetch_workday
from venator.discover.progress import get_source_progress
from venator.discover.run import _process_board
from venator.match.run import run as run_filters
from venator.profile import Profile
from venator.profile.schema import FilterPolicy
from venator.schedule.loop import run_loop
from venator.view import build


def test_refresh_resumes_details_and_publishes_evidence_without_manual_corrections(tmp_path, monkeypatch):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    profile = Profile(
        name="candidate", directory=profile_dir, resume={"skills": ["Python"]},
        filters=FilterPolicy(),
    )
    postings, decisions, track = (tmp_path / name for name in ("postings", "decisions", "track"))
    database, heartbeat = tmp_path / "view.db", tmp_path / "runs.jsonl"
    board = "example.wd1~Careers"
    jobs = [
        {"key": f"workday:{board}:{number}", "source": "workday", "board": board,
         "title": f"Python developer {number}", "location": location,
         "url": f"https://example.invalid/jobs/{number}", "opportunity_type": "job",
         "description_kind": "missing", "description_html": ""}
        for number, location in [(1, "Boston, MA"), (2, "Boston, MA"), (3, "Boston, MA"), (4, "Paris, France")]
    ]
    windows = iter([
        PostingBatch(jobs, complete=False, status="partial", next_offset=20, pages_fetched=1),
        PostingBatch([], complete=False, status="partial", next_offset=40, pages_fetched=1),
    ])
    offsets, details = [], []

    def window(*args, **kwargs):
        offsets.append(kwargs["start_offset"])
        return next(windows)

    def enrich(job):
        details.append(job["key"])
        text = "Python experience required."
        if job["key"] == jobs[1]["key"]:
            text += " Active nursing license required."
        return dict(job, description_html=text, description_kind="full")

    monkeypatch.setattr("venator.discover.run.fetch_workday_window", window)
    monkeypatch.setattr("venator.discover.run.ENRICHERS", {"workday": enrich})

    def discover():
        _process_board(postings, "workday", board, fetch_workday, detail_limit=2, listing_pages=1)

    def view():
        build.build_database(postings, decisions, database, track, heartbeat, profile=profile)

    actions = {"discover": discover, "filters": lambda: run_filters(postings, decisions, profile=profile), "jev": lambda: {}, "view": view}

    def statuses():
        with sqlite3.connect(database) as db:
            return dict(db.execute("SELECT posting_key, status FROM assessments"))

    assert run_loop(stage_callables=actions, heartbeat_path=heartbeat, repository=tmp_path) == 0
    assert list(statuses().values()) == ["suitable", "needs_review", "needs_review", "needs_review"]
    with sqlite3.connect(database) as db:
        credential_unknowns = db.execute("SELECT unknowns FROM assessments WHERE posting_key = ?", (jobs[1]["key"],)).fetchone()[0]
        assert "Credential or eligibility requirement needs review" in credential_unknowns
        assert db.execute("SELECT status, known_jobs, full_verified_details, needs_detail_check FROM source_health").fetchone() == ("partial", 4, 2, 2)

    # No listing in this window: the stored detail backlog still makes progress.
    assert run_loop(stage_callables=actions, heartbeat_path=heartbeat, repository=tmp_path) == 0
    assert offsets == [0, 20]
    assert details == [job["key"] for job in jobs]
    assert get_source_progress(postings, f"workday:{board}")["next_offset"] == 40
    assert list(statuses().values()) == ["suitable", "needs_review", "suitable", "suitable"]
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT status, known_jobs, full_verified_details, needs_detail_check FROM source_health").fetchone() == ("partial", 4, 4, 0)
    assert all(json.loads(line)["status"] == "ok" for line in heartbeat.read_text().splitlines())


@pytest.mark.parametrize("fault", ["missing_assessment", "false_recommendation", "wrong_coverage"])
def test_invalid_dashboard_fails_refresh_and_keeps_previous_database(tmp_path, monkeypatch, fault):
    postings, decisions, track = (tmp_path / name for name in ("postings", "decisions", "track"))
    postings.mkdir()
    (postings / "2026-09-07.jsonl").write_text(json.dumps({
        "key": "greenhouse:acme:1", "source": "greenhouse", "board": "acme",
        "title": "Unverified opening", "description_kind": "missing",
    }) + "\n")
    database, heartbeat = tmp_path / "view.db", tmp_path / "runs.jsonl"

    def view():
        build.build_database(postings, decisions, database, track, heartbeat)

    view()
    previous = database.read_bytes()
    verify = build.verify_dashboard

    def corrupt_then_verify(db, *args, **kwargs):
        if fault == "missing_assessment":
            db.execute("DELETE FROM assessments")
        elif fault == "false_recommendation":
            db.execute("UPDATE assessments SET status = 'suitable'")
        else:
            db.execute("UPDATE source_health SET full_verified_details = 50")
        verify(db, *args, **kwargs)

    monkeypatch.setattr(build, "verify_dashboard", corrupt_then_verify)
    assert run_loop(only=("view",), stage_callables={"view": view}, heartbeat_path=heartbeat, repository=tmp_path) == 1
    assert database.read_bytes() == previous
    assert not database.with_name(f".{database.name}.tmp").exists()
    failure = json.loads(heartbeat.read_text().splitlines()[-1])
    assert failure["stage"] == "view" and failure["status"] == "error"
    assert "Dashboard validation failed" in failure["error"]
