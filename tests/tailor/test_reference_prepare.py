"""Reference layout preparation is fact-preserving and provider-free on reuse."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfReader

# Tests are not a Python package in pytest's import mode. Load the existing
# synthetic fixtures directly without changing sys.path or renderer-owned files.
from importlib.util import module_from_spec, spec_from_file_location

_spec = spec_from_file_location("reference_fixtures", Path(__file__).parents[1] / "resume/test_reference.py")
_reference_fixtures = module_from_spec(_spec)
_spec.loader.exec_module(_reference_fixtures)
FONT_DIR = _reference_fixtures.FONT_DIR
resume = _reference_fixtures.resume
source = _reference_fixtures.source
from venator.profile import Profile
from venator.resume.reference import ReferenceLayoutError, capture_reference
from venator.tailor.prepare import _input_version, prepare_application

JOB = {"key": "test:lab:1", "title": "Student Assistant", "company": "Example Laboratory",
       "description_html": "<p>Assist with samples and document results.</p>",
       "url": "https://example.test/jobs/1"}


class Completion:
    """A deterministic two-pass provider seam; no external service is used."""
    def __init__(self, *, reverse_and_omit: bool = False):
        self.calls = []
        self.reverse_and_omit = reverse_and_omit

    def __call__(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if "GENERATED PASSAGES:\n" in prompt:
            passages = json.loads(prompt.rsplit("GENERATED PASSAGES:\n", 1)[1])
            return {"checks": [{"draft_id": row["draft_id"], "status": "supported", "reason": "Exact synthetic source."} for row in passages]}
        sources = json.loads(prompt.rsplit("CONFIRMED RESUME SOURCES:\n", 1)[1])
        selected = list(sources)
        if self.reverse_and_omit:
            selected = [row for row in sources if row["retain"] or row == sources[-1]][::-1]
        bullets = [{"source_id": bullet["id"], "text": bullet["text"]}
                   for row in selected for bullet in row["bullets"]]
        if self.reverse_and_omit:
            bullets.reverse()
        return {"selected_entry_ids": [row["entry_id"] for row in selected],
                "resume_bullets": bullets, "letter_paragraphs": []}


def candidate(tmp_path, resume):
    directory = tmp_path / "profile"
    directory.mkdir()
    return Profile(name="synthetic", directory=directory, resume=resume)


def install_reference(source, profile):
    directory = profile.directory / "resume-reference"
    capture_reference(source, directory, font_directory=FONT_DIR)
    return directory


def change_layout(reference):
    path = reference / "layout.json"
    metadata = json.loads(path.read_text())
    metadata["layout"]["rule_width"] += 0.1
    path.write_text(json.dumps(metadata))


def fail_completion(*args, **kwargs):
    pytest.fail("layout-only reuse or preflight must not call a provider")


def test_checked_basic_draft_can_change_to_reference_layout_without_provider(source, resume, tmp_path):
    profile = candidate(tmp_path, resume)
    output = tmp_path / "application"
    completion = Completion()
    first = prepare_application(JOB, profile, output, provider="codex", completion=completion)
    assert len(completion.calls) == 2
    install_reference(source, profile)
    second = prepare_application(JOB, profile, output, provider="codex", completion=fail_completion)
    assert second["layout"] == "uploaded_reference"
    assert second["version"] != first["version"]
    assert second["facts_version"] == first["facts_version"]
    assert second["draftProvenance"] == first["draftProvenance"]
    assert second["factualityReview"] == first["factualityReview"]
    pdf = output / "versions" / second["version"] / "resume.pdf"
    assert "Assisted with 12 samples." in PdfReader(pdf).pages[0].extract_text()
    assert any("without another AI request" in change for change in second["changes"])


def test_reference_restores_all_confirmed_entries_and_bullet_order(source, resume, tmp_path):
    resume["experience"][0]["bullets"].append("Documented sample results.")
    extra = copy.deepcopy(resume["experience"][0])
    extra.update(org="Second Laboratory", dates="2025", bullets=["Prepared sample notes.", "Reviewed sample records."])
    resume["experience"].append(extra)
    profile = candidate(tmp_path, resume)
    completion = Completion(reverse_and_omit=True)
    output = tmp_path / "application"
    initial = prepare_application(JOB, profile, output, provider="codex", completion=completion)
    assert "Assisted with 12 samples." not in initial["resumeText"]
    install_reference(source, profile)
    manifest = prepare_application(JOB, profile, output, provider="codex", completion=fail_completion)
    rendered = manifest["renderedResume"]
    assert [section["key"] for section in rendered["sections"]] == ["education", "experience"]
    assert [entry["org"] for entry in rendered["experience"]] == ["Example Laboratory", "Second Laboratory"]
    assert [entry["bullets"] for entry in rendered["experience"]] == [entry["bullets"] for entry in resume["experience"]]
    assert len(completion.calls) == 2


@pytest.mark.parametrize("change", ["candidate_fact", "job", "provider"])
def test_changed_facts_job_or_provider_cannot_reuse_previous_writing(source, resume, tmp_path, change):
    profile = candidate(tmp_path, resume)
    output = tmp_path / "application"
    first = prepare_application(JOB, profile, output, provider="codex", completion=Completion())
    reference = install_reference(source, profile)
    job, provider = JOB, "codex"
    if change == "candidate_fact":
        updated = copy.deepcopy(resume)
        updated["experience"][0]["bullets"] = ["Assisted with 14 samples."]
        profile = replace(profile, resume=updated)
    elif change == "job":
        job = {**JOB, "description_html": "<p>Analyze samples and calibrate equipment.</p>"}
    else:
        provider = "claude"
    completion = Completion()
    second = prepare_application(job, profile, output, provider=provider, completion=completion)
    assert len(completion.calls) == 2
    assert second["version"] != first["version"]
    assert not any("without another AI request" in change for change in second["changes"])
    assert reference.is_dir()


@pytest.mark.parametrize("tamper", ["original", "source_id", "final", "remove_provenance_and_review"])
def test_tampered_provenance_cannot_be_reused(source, resume, tmp_path, tamper):
    profile = candidate(tmp_path, resume)
    output = tmp_path / "application"
    first = prepare_application(JOB, profile, output, provider="codex", completion=Completion())
    manifest = copy.deepcopy(first)
    row = manifest["draftProvenance"][0]
    if tamper == "original":
        row["original"] = "Invented source facts."
    elif tamper == "source_id":
        row["source_ids"] = ["missing:source"]
    elif tamper == "final":
        row["final"] = "Different final text."
    else:
        manifest["draftProvenance"] = []
        manifest["factualityReview"]["checks"] = []
    (output / "manifest.json").write_text(json.dumps(manifest))
    install_reference(source, profile)
    completion = Completion()
    prepare_application(JOB, profile, output, provider="codex", completion=completion)
    assert len(completion.calls) == 2


@pytest.mark.parametrize("damage", ["missing_metadata", "missing_font", "changed_font", "invalid_metadata"])
def test_invalid_reference_assets_fail_before_provider_calls(source, resume, tmp_path, damage):
    profile = candidate(tmp_path, resume)
    reference = install_reference(source, profile)
    if damage == "missing_metadata":
        (reference / "layout.json").unlink()
    elif damage == "missing_font":
        (reference / "bold.ttf").unlink()
    elif damage == "changed_font":
        (reference / "bold.ttf").write_bytes(b"tampered font")
    else:
        (reference / "layout.json").write_text("not valid JSON")
    with pytest.raises((ReferenceLayoutError, OSError, json.JSONDecodeError)):
        prepare_application(JOB, profile, tmp_path / "application", provider="codex", completion=fail_completion)


def test_layout_hash_invalidates_version_but_reuses_same_fact_writing(source, resume, tmp_path):
    profile = candidate(tmp_path, resume)
    reference = install_reference(source, profile)
    output = tmp_path / "application"
    first = prepare_application(JOB, profile, output, provider="codex", completion=Completion())
    facts_version = _input_version(profile, profile.resume, include_layout=False)
    change_layout(reference)
    assert _input_version(profile, profile.resume) != first["input_version"]
    assert _input_version(profile, profile.resume, include_layout=False) == facts_version
    second = prepare_application(JOB, profile, output, provider="codex", completion=fail_completion)
    assert second["version"] != first["version"]
    assert second["input_version"] != first["input_version"]
    assert second["resumeText"] == first["resumeText"]


def test_same_layout_cached_manifest_does_not_bypass_provenance_validation(source, resume, tmp_path):
    profile = candidate(tmp_path, resume)
    install_reference(source, profile)
    output = tmp_path / "application"
    initial = prepare_application(JOB, profile, output, provider="codex", completion=Completion())
    initial["draftProvenance"][0]["original"] = "Tampered source facts."
    (output / "manifest.json").write_text(json.dumps(initial))
    completion = Completion()
    prepare_application(JOB, profile, output, provider="codex", completion=completion)
    assert len(completion.calls) == 2
