"""The application's actual CLI composes stores and history, without a model."""
import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from venator import applications
from venator.discover.store import append_observations
from venator.match.store import load_postings


def test_application_cli_reads_preparation_status_and_records_manual_application(tmp_path: Path) -> None:
    install = tmp_path / "install"
    profile = install / "profiles" / "candidate"
    profile.mkdir(parents=True)
    (profile / "targeting.yaml").write_text("profile:\n  name: candidate\n  id: candidate-test\n")
    postings = install / "data" / "postings"
    postings.mkdir(parents=True)
    (postings / "2026-09-04.jsonl").write_text(json.dumps({
        "key": "greenhouse:acme:1", "source": "greenhouse", "board": "acme", "title": "Role",
        "url": "https://example.test/job", "discovered_at": "2026-09-04T00:00:00Z",
    }) + "\n")
    environment = {**os.environ, "VENATOR_HOME": str(install)}

    def invoke(action: str) -> dict:
        result = subprocess.run([sys.executable, "-m", "venator.applications", action, "greenhouse:acme:1", "--profile", "candidate"],
                                cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    assert invoke("status")["prepared"] is False
    assert "applied" in invoke("applied")["message"]
    events = [json.loads(line) for path in (install / "data" / "track").glob("*.jsonl") for line in path.read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["actor"] == "owner"
    assert events[0]["event"] == "submit"
    assert (install / "build" / "venator.db").is_file()


@pytest.fixture
def application_run(tmp_path, monkeypatch):
    """Real installed Profile, stores, PDF preparation, tracking and projection."""
    monkeypatch.chdir(tmp_path)
    install = tmp_path / "install"
    profile_dir = install / "profiles" / "candidate"
    profile_dir.mkdir(parents=True)
    resume = {
        "name": "Test Candidate", "contact": {"email": "candidate@example.test"},
        "experience": [{"id": "lab", "org": "Fixture Laboratory", "role": "Technician",
                        "bullets": [{"id": "lab:fact", "text": "Measured synthetic samples."}]}],
    }
    (profile_dir / "resume.yaml").write_text(yaml.safe_dump(resume))
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
        print("synthetic provider log")
        if "GENERATED PASSAGES:" in prompt:
            passages = json.loads(prompt.split("GENERATED PASSAGES:\n", 1)[1])
            return json.dumps({"checks": [{"draft_id": row["draft_id"], "status": "supported", "reason": "Supported by fixture original."} for row in passages]})
        return json.dumps({"selected_entry_ids": ["lab"],
                           "resume_bullets": [{"source_id": "lab:fact", "text": "Measured synthetic samples."}],
                           "letter_paragraphs": [] if "No cover letter was requested" in prompt else
                           [{"source_ids": ["lab:fact"], "text": "I measured synthetic samples."}]})
    def browser(posting, profile, resume_file, directory):
        run.browser_calls.append((posting.copy(), profile, resume_file, directory))
        assert resume_file.read_bytes().startswith(b"%PDF")
        assert (resume_file.parent / "resume.txt").read_text().find("Measured synthetic samples.") >= 0
        return {"message": "Synthetic browser ready; submit manually.", "filled": 2, "warnings": []}
    monkeypatch.setattr("venator.discover.refresh.refresh_posting", exact_refresh)
    monkeypatch.setattr("venator.llm.complete", model)
    monkeypatch.setattr("venator.browser.handoff.start_handoff", browser)
    def arguments(action, **kwargs):
        return argparse.Namespace(action=action, key=posting["key"], profile="candidate", profile_dir=None, provider="codex", cover_letter=False, file=None, **kwargs)
    run.args = arguments
    run.perform = lambda action: applications.perform(arguments(action))
    return run


def test_real_prepare_download_and_handoff_use_the_current_immutable_files(application_run):
    run = application_run
    args = run.args("prepare")
    args.cover_letter = True
    prepared = applications.perform(args)
    assert len(run.model_calls) == 2
    assert run.model_calls[0][1]["environ"]["VENATOR_LLM_RUNTIME"] == "codex"
    assert run.perform("status")["version"] == prepared["version"]
    downloaded = run.args("file")
    downloaded.file = "resume.pdf"
    pdf = base64.b64decode(applications.perform(downloaded)["base64"])
    assert pdf.startswith(b"%PDF")
    result = run.perform("handoff")
    assert result["filled"] == 2
    assert len(run.browser_calls) == 1
    refreshed, _profile, resume_file, directory = run.browser_calls[0]
    assert refreshed["key"] == run.refreshed["key"]
    assert resume_file == directory / "versions" / prepared["version"] / "resume.pdf"
    assert (resume_file.parent / "letter.txt").read_text() == prepared["letterText"]
    assert resume_file.read_bytes() == pdf
    assert applications.perform(args)["version"] == prepared["version"]
    assert len(run.model_calls) == 2
    events = [json.loads(line) for path in (run.install / "data" / "track").glob("*.jsonl") for line in path.read_text().splitlines()]
    assert all(event["event"] == "prepare" for event in events)
    assert (run.install / "build" / "venator.db").is_file()


@pytest.mark.parametrize("action", ["prepare", "handoff"])
@pytest.mark.parametrize("change", [
    {"listing_status": "closed"},
    {"verification_status": "unknown"},
    {"listing_status": "unknown"},
    {"description_kind": "snippet"},
    {"description_html": ""},
])
def test_unverified_or_closed_refresh_stops_before_provider_or_browser(application_run, action, change):
    run = application_run
    run.refreshed.update(change)
    with pytest.raises(ValueError, match="closed|could not be verified"):
        run.perform(action)
    assert run.model_calls == []
    assert run.browser_calls == []
    assert (run.install / "build" / "venator.db").is_file()
    latest = load_postings(run.install / "data" / "postings")[0]
    assert latest["key"] == run.refreshed["key"]
    if change.get("listing_status") == "closed":
        assert latest["listing_status"] == "closed"


def test_wrong_refresh_identity_never_enters_this_store_or_preparation(application_run):
    run = application_run
    run.refreshed["key"] = "greenhouse:fixture:2"
    with pytest.raises(ValueError, match="different job"):
        run.perform("prepare")
    assert not run.model_calls
    assert [posting["key"] for posting in load_postings(run.install / "data" / "postings")] == ["greenhouse:fixture:1"]


@pytest.mark.parametrize("change", ["job", "profile"])
def test_changed_facts_require_new_documents_before_handoff(application_run, change):
    run = application_run
    run.perform("prepare")
    if change == "job":
        run.refreshed["description_html"] = "A materially different role."
    else:
        with (run.profile_dir / "resume.yaml").open("a") as stream:
            stream.write("\nsummary: Changed confirmed facts.\n")
    with pytest.raises(ValueError, match="job or your profile changed"):
        run.perform("handoff")
    assert not run.browser_calls
    assert len(run.model_calls) == 2
    status = run.perform("status")
    assert status["prepared"] is False
    assert "changed" in status["message"]


@pytest.mark.parametrize("damage", ["resume.pdf", "letter.txt", "preview", "revision", "extra-letter"])
def test_handoff_refuses_damaged_or_inconsistent_current_artifacts(application_run, damage):
    run = application_run
    args = run.args("prepare")
    args.cover_letter = damage == "letter.txt"
    record = applications.perform(args)
    directory = next((run.install / "data" / "applications").iterdir())
    version = directory / "versions" / record["version"]
    if damage in {"resume.pdf", "letter.txt"}:
        (version / damage).write_bytes(b"altered after preparation")
    elif damage == "extra-letter":
        (version / "letter.txt").write_text("Old unintended letter")
    else:
        record["resumeText" if damage == "preview" else "preparation_revision"] = "altered"
        (directory / "manifest.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="damaged|disagree|fresh documents|unexpected letter"):
        run.perform("handoff")
    assert not run.browser_calls
    assert run.perform("status")["prepared"] is False


def test_download_refuses_tampered_files_and_symlinked_artifacts(application_run, tmp_path):
    run = application_run
    record = run.perform("prepare")
    directory = next((run.install / "data" / "applications").iterdir())
    path = directory / "versions" / record["version"] / "resume.pdf"
    original = path.read_bytes()
    path.write_bytes(b"tampered")
    args = run.args("file")
    args.file = "resume.pdf"
    with pytest.raises(ValueError, match="damaged"):
        applications.perform(args)
    path.unlink()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(original)
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="location changed"):
        applications.perform(args)


def test_cli_returns_one_json_result_and_sends_provider_logs_to_stderr(application_run, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["venator.applications", "prepare", "greenhouse:fixture:1", "--profile", "candidate", "--provider", "api"])
    assert applications.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["prepared"] is True
    assert "synthetic provider log" in output.err
    assert application_run.model_calls[0][1]["environ"]["VENATOR_LLM_RUNTIME"] == "api"
    monkeypatch.setattr(sys, "argv", ["venator.applications", "prepare", "greenhouse:fixture:1", "--profile", "candidate"])
    assert applications.main() == 1
    assert json.loads(capsys.readouterr().out) == {"error": "Choose a provider for preparation."}


def test_provider_failure_preserves_documents_and_projects_refreshed_job(application_run, monkeypatch):
    import sqlite3
    run = application_run
    prepared = run.perform("prepare")
    directory = next((run.install / "data" / "applications").iterdir())
    original_manifest = (directory / "manifest.json").read_bytes()
    run.refreshed["description_html"] = "New verified employer wording."
    def fail_once(*_args, **_kwargs):
        raise RuntimeError("synthetic provider unavailable")
    monkeypatch.setattr("venator.llm.complete", fail_once)
    with pytest.raises(RuntimeError, match="synthetic provider unavailable"):
        run.perform("prepare")
    assert (directory / "manifest.json").read_bytes() == original_manifest
    assert (directory / "versions" / prepared["version"] / "resume.pdf").is_file()
    with sqlite3.connect(run.install / "build" / "venator.db") as database:
        assert database.execute("SELECT description_html FROM postings").fetchone()[0] == "New verified employer wording."


def test_profile_changed_during_refresh_stops_before_model(application_run, monkeypatch):
    run = application_run
    def changed_profile(_posting):
        with (run.profile_dir / "resume.yaml").open("a") as stream:
            stream.write("summary: Edited while refresh was in flight.\n")
        return run.refreshed.copy()
    monkeypatch.setattr("venator.discover.refresh.refresh_posting", changed_profile)
    with pytest.raises(ValueError, match="profile changed during verification"):
        run.perform("prepare")
    assert not run.model_calls


def test_status_handles_malformed_manifest_without_claiming_documents_are_ready(application_run):
    run = application_run
    run.perform("prepare")
    directory = next((run.install / "data" / "applications").iterdir())
    (directory / "manifest.json").write_text("{malformed")
    status = run.perform("status")
    assert status["prepared"] is False
    assert "unreadable" in status["message"]
