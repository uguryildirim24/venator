"""Application actions use a fixture Install and the completion seam, never an employer."""
from __future__ import annotations

import argparse
import base64
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pypdf import PdfReader

from venator import applications
from venator.discover.store import append_observations


@pytest.fixture
def application_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    install = tmp_path / "install"
    profile_dir = install / "profiles" / "candidate"
    profile_dir.mkdir(parents=True)
    (profile_dir / "resume.yaml").write_text(yaml.safe_dump({
        "name": "Test Candidate", "contact": {"email": "candidate@example.test"},
        "experience": [{"id": "lab", "org": "Fixture Laboratory", "role": "Technician",
                        "bullets": [{"id": "lab:fact", "text": "Measured synthetic samples."}]}],
    }))
    (profile_dir / "targeting.yaml").write_text("profile:\n  name: candidate\n  id: candidate-test\n")
    monkeypatch.setenv("VENATOR_HOME", str(install))
    posting = {
        "key": "greenhouse:fixture:1", "source": "greenhouse", "board": "fixture", "external_id": "1",
        "title": "Fixture Technician", "company": "Fixture Laboratory", "description_html": "Measure samples.",
        "description_kind": "full", "listing_status": "open", "verification_status": "verified",
        "url": "https://boards.greenhouse.io/fixture/jobs/1", "discovered_at": "2026-09-04T00:00:00Z",
    }
    append_observations(install / "data" / "postings", [posting])
    run = SimpleNamespace(install=install, profile_dir=profile_dir, refreshed=posting.copy(), model_calls=[], browser_calls=[], refresh_calls=[])

    def exact_refresh(value):
        run.refresh_calls.append(value.copy())
        return run.refreshed.copy()

    def model(prompt, **kwargs):
        run.model_calls.append((prompt, kwargs))
        if "GENERATED PASSAGES:" in prompt:
            passages = json.loads(prompt.split("GENERATED PASSAGES:\n", 1)[1])
            return json.dumps({"checks": [{"draft_id": row["draft_id"], "status": "supported", "reason": "Fixture source."}
                                          for row in passages]})
        return json.dumps({"selected_entry_ids": ["lab"],
                           "resume_bullets": [{"source_id": "lab:fact", "text": "Measured synthetic samples."}],
                           "letter_paragraphs": [{"source_ids": ["lab:fact"], "text": "I measured synthetic samples."},
                                                 {"source_ids": ["lab:fact"], "text": "My samples were measured."}]})

    def browser(posting, profile, resume_file, directory, *, letter_file, questions, form_memory, answers=None, prepared_resume=None):
        assert form_memory == install / "data" / "answers" / "forms"
        # Workday corrections read the tailored version this Posting prepared,
        # never the base resume.yaml wording.
        assert prepared_resume is not None and "experience" in prepared_resume
        run.browser_calls.append((posting, resume_file, letter_file))
        assert questions and questions[0]["name"] == "cover_letter"
        assert resume_file.read_bytes().startswith(b"%PDF")
        assert letter_file.read_bytes().startswith(b"%PDF")
        assert resume_file.parent == letter_file.parent
        return {"message": "Fixture browser ready.", "filled": 2, "warnings": []}

    monkeypatch.setattr("venator.apply.form.greenhouse_questions", lambda posting: [
        {"name": "cover_letter", "label": "Cover Letter", "kind": "file", "options": None, "maxlength": None},
    ])
    monkeypatch.setattr("venator.discover.refresh.refresh_posting", exact_refresh)
    monkeypatch.setattr("venator.llm.complete", model)
    monkeypatch.setattr("venator.browser.handoff.start_handoff", browser)

    def args(action, *, file=None, edits=None, version=None):
        return argparse.Namespace(action=action, key=posting["key"], profile="candidate", profile_dir=None,
                                  file=file, edits=json.dumps(edits or []), version=version)

    run.args = args
    run.perform = lambda action, **options: applications.perform(args(action, **options))
    run.directory = applications.application_directory({"data_dir": install / "data"}, "candidate-test", posting["key"])
    return run


def test_apply_returns_hashed_resume_and_letter_and_fill_uses_current_version(application_run):
    run = application_run
    record = run.perform("apply")
    assert len(run.model_calls) == 2
    assert run.model_calls[0][1]["environ"]["VENATOR_LLM_RUNTIME"] == "claude"
    assert run.perform("status")["version"] == record["version"]
    for name in ("resume.pdf", "letter.pdf"):
        data = base64.b64decode(run.perform("file", file=name)["base64"])
        assert data.startswith(b"%PDF")
        assert record["file_hashes"][name]
    assert run.perform("fill")["filled"] == 2
    assert run.browser_calls[-1][1].parent.name == record["version"]
    assert run.browser_calls[-1][2].name == "letter.pdf"
    assert len(run.refresh_calls) == 1  # Fill doesn't refetch after Apply.


def test_ashby_employer_form_memory_does_not_suppress_current_posting_letter(application_run):
    from venator.apply.form import remember_ashby_form

    run = application_run
    posting = {**run.refreshed, "key": "ashby:fixture:2", "source": "ashby", "external_id": "2",
               "url": "https://jobs.ashbyhq.com/fixture/2/application"}
    append_observations(run.install / "data" / "postings", [posting])
    run.refreshed = posting
    remember_ashby_form(run.install / "data" / "answers" / "forms", posting, [
        {"name": "full_name", "label": "Full Name", "kind": "text", "required": True,
         "options": None, "maxlength": None},
    ], "candidate-test")
    args = run.args("apply")
    args.key = posting["key"]
    record = applications.perform(args)
    assert record["formQuestions"][0]["name"] == "full_name"
    assert "letter.pdf" in record["file_hashes"]


def test_owner_edits_version_without_completion_and_next_apply_keeps_it(application_run):
    run = application_run
    original = run.perform("apply")
    edited = run.perform("edit", version=original["version"], edits=[
        {"draft_id": "resume:0", "text": "Measured edited samples."},
        {"draft_id": "letter:0", "text": "I measured edited samples."},
    ])
    assert edited["version"] != original["version"]
    assert edited["edited_by"] == "owner"
    assert edited["edited_from"] == original["version"]
    assert len(run.model_calls) == 2
    assert all(row["edited_by"] == "owner" and "review_status" not in row for row in edited["draftProvenance"][:2])
    assert edited["draftProvenance"][2]["review_status"] == "supported"
    assert {row["draft_id"] for row in edited["factualityReview"]["checks"]} == {"letter:1"}
    assert "Measured edited samples." in edited["resumeText"]
    assert "I measured edited samples." in edited["letterText"]
    assert run.perform("status")["version"] == edited["version"]
    for name in ("resume.pdf", "letter.pdf"):
        pdf = base64.b64decode(run.perform("file", file=name)["base64"])
        assert "edited samples" in PdfReader(BytesIO(pdf)).pages[0].extract_text()
    run.perform("fill")
    assert run.browser_calls[-1][1].parent.name == edited["version"]
    assert run.perform("apply")["version"] == edited["version"]
    assert len(run.model_calls) == 2


def test_tampered_letter_refuses_fill_before_upload(application_run):
    run = application_run
    record = run.perform("apply")
    (run.directory / "versions" / record["version"] / "letter.pdf").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed or is damaged"):
        run.perform("fill")
    assert run.browser_calls == []
    assert run.perform("status")["prepared"] is False


def test_edit_failure_does_not_replace_reviewed_version(application_run):
    run = application_run
    record = run.perform("apply")
    with pytest.raises(ValueError, match="paragraph"):
        run.perform("edit", version=record["version"], edits=[{"draft_id": "resume:0", "text": "long " * 400}])
    assert run.perform("status")["version"] == record["version"]
    assert len(run.model_calls) == 2


def test_apply_refresh_failure_and_retry(application_run):
    run = application_run
    run.refreshed["listing_status"] = "closed"
    with pytest.raises(ValueError, match="closed"):
        run.perform("apply")
    assert run.model_calls == []
    assert run.browser_calls == []
    run.refreshed["listing_status"] = "open"
    assert run.perform("apply")["prepared"] is True


def test_retracting_a_reviewed_answer_or_statement_refuses_fill(application_run, monkeypatch):
    from venator.answers.store import append, retract
    run = application_run
    answers = run.install / "data" / "answers"
    question = append(answers, "candidate-test", text="What is your laboratory preference?", kind="text",
                      answer="Bench work", board="fixture")
    statement = append(answers, "candidate-test", text="Motivation", kind="statement",
                       answer="I want to work in a lab.", board="any", universal=True)
    monkeypatch.setattr("venator.apply.form.greenhouse_questions", lambda posting: [
        {"name": "cover_letter", "label": "Cover Letter", "kind": "file", "options": None, "maxlength": None},
        {"name": "question_1", "label": question["text"], "kind": "text", "options": None, "maxlength": None},
    ])
    record = run.perform("apply")
    assert record["answerPins"][0]["id"] == question["id"]
    assert record["statementPins"][0]["id"] == statement["id"]
    retract(answers, "candidate-test", question)
    assert run.perform("status")["prepared"] is False
    with pytest.raises(ValueError, match="answer or statement changed"):
        run.perform("fill")
    assert run.browser_calls == []
    # The independent statement pin also detects a change.
    append(answers, "candidate-test", text=question["text"], kind="text", answer="Bench work", board="fixture")
    run.perform("apply")
    retract(answers, "candidate-test", statement)
    with pytest.raises(ValueError, match="answer or statement changed"):
        run.perform("fill")
    refreshed = run.perform("apply")
    assert refreshed["statementPins"] == []
    assert run.perform("fill")["filled"] == 2


def test_confirmation_status_dedupes_after_later_events(application_run):
    from venator.track.store import load_events
    run = application_run
    run.perform("apply")
    marker = {"system": "greenhouse", "board": "fixture", "job_id": "1", "host": "boards.greenhouse.io", "at": 1}
    (run.directory / ".handoff-session.json").write_text(json.dumps({"state": "applied", "marker": marker}))
    assert run.perform("status")["applied"] is True
    assert run.perform("status")["applied"] is True
    events = load_events(run.install / "data" / "track")
    assert sum(row["event"] == "submit" for row in events) == 1
    run.perform("dismiss")
    run.perform("status")
    from venator.track.record import record_event
    record_event("outcome", run.refreshed["key"], "interview", identifier="candidate-test",
                 postings_dir=run.install / "data" / "postings", decisions_dir=run.install / "data" / "decisions",
                 track_dir=run.install / "data" / "track")
    run.perform("status")
    assert sum(row["event"] == "submit" for row in load_events(run.install / "data" / "track")) == 1


def test_manual_applied_precedes_confirmation_without_duplicate(application_run):
    from venator.track.store import load_events
    run = application_run
    run.perform("apply")
    run.perform("applied")
    (run.directory / ".handoff-session.json").write_text(json.dumps({"state": "applied", "marker": {
        "system": "greenhouse", "board": "fixture", "job_id": "1", "host": "boards.greenhouse.io", "at": 1}}))
    run.perform("status")
    assert sum(row["event"] == "submit" for row in load_events(run.install / "data" / "track")) == 1


def test_changed_profile_refuses_fill(application_run):
    run = application_run
    run.perform("apply")
    with (run.profile_dir / "resume.yaml").open("a") as stream:
        stream.write("summary: Changed confirmed facts.\n")
    with pytest.raises(ValueError, match="job or your profile changed"):
        run.perform("fill")
    assert run.browser_calls == []


def test_workday_confirmation_needs_the_candidate_home_binding(application_run):
    from venator.track.store import load_events

    run = application_run
    posting = {**run.refreshed, "key": "workday:fixture.wd1~FixtureSite:R123456", "source": "workday",
               "board": "fixture.wd1~FixtureSite", "external_id": "R123456", "company": "Fixture Laboratory",
               "url": "https://fixture.wd1.myworkdayjobs.com/en-US/FixtureSite/job/R123456"}
    append_observations(run.install / "data" / "postings", [posting])
    directory = applications.application_directory({"data_dir": run.install / "data"}, "candidate-test", posting["key"])
    directory.mkdir(parents=True, exist_ok=True)

    def status(marker):
        (directory / ".handoff-session.json").write_text(json.dumps({"state": "applied", "marker": marker}))
        return applications.perform(argparse.Namespace(action="status", key=posting["key"], profile="candidate",
                                                       profile_dir=None, file=None, edits="[]", version=None))

    base = {"system": "workday", "board": posting["board"], "job_id": "R123456",
            "host": "fixture.wd1.myworkdayjobs.com", "at": 1}
    # A generic completion marker without the requisition and title is not a receipt.
    assert status(base)["applied"] is False
    assert status({**base, "requisition": "R123456"})["applied"] is False
    assert status({**base, "requisition": "R000000", "title": "Fixture Technician"})["applied"] is False
    assert status({**base, "requisition": "R123456", "title": "Fixture Technician"})["applied"] is True
    events = load_events(run.install / "data" / "track")
    assert sum(row["event"] == "submit" and row["posting_key"] == posting["key"] for row in events) == 1
