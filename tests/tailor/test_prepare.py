"""The preparation lane spends one selected completion and renders only facts."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pypdf import PdfReader

from venator.profile import Profile
from venator.tailor.prepare import prepare_application


def profile() -> Profile:
    return Profile(
        name="candidate",
        directory=Path("/no-profile-files"),
        resume={
            "sections": [
                {"key": "education", "title": "EDUCATION", "kind": "education"},
                {
                    "key": "experience",
                    "title": "EXPERIENCE",
                    "kind": "entries",
                    "heading": "org",
                    "subheading": "role",
                },
            ],
            "name": "Dana Okafor",
            "contact": {"email": "dana@example.test"},
            "education": [
                {
                    "id": "education:example-university",
                    "org": "Example University",
                    "location": "Example City, MA",
                    "date": "May 2027 (Expected)",
                    "degree": "Bachelor of Science in Biochemistry",
                }
            ],
            "experience": [
                {
                    "id": "experience:lab",
                    "org": "Example University",
                    "role": "Lab Technician",
                    "location": "Example City, MA",
                    "dates": "February 2026 - May 2026",
                    "bullets": [
                        {"id": "bullet:gcms", "text": "Operated GC-MS for sample analysis."},
                        {
                            "id": "bullet:draft",
                            "text": "Invented an unconfirmed accomplishment.",
                            "draft": True,
                        },
                    ],
                },
                {
                    "id": "experience:draft",
                    "org": "Draft Employer",
                    "role": "Draft Analyst",
                    "location": "Boston, MA",
                    "dates": "2025",
                    "draft": True,
                    "bullets": [{"id": "bullet:draft-entry", "text": "Draft work."}],
                },
                {
                    "id": "experience:unconfirmed",
                    "org": "Unconfirmed Employer",
                    "role": "Analyst",
                    "location": "Boston, MA",
                    "dates": "2025",
                    "confirmed": False,
                    "bullets": [{"id": "bullet:unconfirmed", "text": "Unconfirmed work."}],
                },
            ],
        },
    )


def posting(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "key": "source:board:1",
        "title": "Research Intern",
        "company": "Acme Labs",
        "description_html": "Analyze samples and document results.",
        "url": "https://example.test/job/1",
    }
    value.update(changes)
    return value


class Completion:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, prompt: str, **kwargs: object) -> object:
        self.calls.append((prompt, kwargs))
        if "GENERATED PASSAGES:\n" in prompt:
            passages = json.loads(prompt.rsplit("GENERATED PASSAGES:\n", 1)[1])
            return {"checks": [{"draft_id": item["draft_id"], "status": "supported", "reason": "Matches the cited fixture source."} for item in passages]}
        return self.reply


def selected(*, bullets: object = None, letter: bool = False) -> dict[str, object]:
    texts = {"bullet:gcms": "Operated GC-MS for sample analysis.", "bullet:draft": "Invented an unconfirmed accomplishment."}
    rewritten = [{"source_id": item, "text": texts.get(item, "Fixture source text.")} if isinstance(item, str) else item for item in (bullets or [])]
    return {
        "selected_entry_ids": ["education:example-university", "experience:lab"],
        "resume_bullets": rewritten,
        "letter_paragraphs": ([{"source_ids": ["experience:lab"], "text": "I am applying for the Research Intern role at Acme Labs, bringing my background as a Lab Technician."}] if letter else []),
    }


def test_two_same_provider_calls_and_preserved_source_text(tmp_path: Path) -> None:
    reply = selected(bullets=["bullet:gcms"])
    completion = Completion(json.dumps(reply))
    result = prepare_application(
        posting(), profile(), tmp_path / "application", provider="codex", completion=completion
    )

    assert len(completion.calls) == 2
    assert completion.calls[0][1]["environ"]["VENATOR_LLM_RUNTIME"] == "codex"
    assert "FULL JOB DATA" in completion.calls[0][0]
    assert "Analyze samples and document results." in completion.calls[0][0]
    assert result["prepared"] is True
    assert result["provider"] == "codex"
    assert result["letterText"] is None

    version = result["version"]
    assert isinstance(version, str) and version.isalnum()
    version_dir = tmp_path / "application" / "versions" / version
    resume_text = (version_dir / "resume.txt").read_text(encoding="utf-8")
    assert resume_text == result["resumeText"]
    assert "Operated GC-MS for sample analysis." in resume_text
    assert "Invented an unconfirmed accomplishment." not in resume_text
    assert "Draft Employer" not in resume_text
    assert "Unconfirmed Employer" not in resume_text
    assert not (version_dir / "letter.txt").exists()
    assert PdfReader(version_dir / "resume.pdf").pages


def test_unknown_source_id_is_refused_and_previous_manifest_survives(tmp_path: Path) -> None:
    directory = tmp_path / "application"
    first = prepare_application(
        posting(), profile(), directory, provider="claude", completion=Completion(selected())
    )
    manifest_path = directory / "manifest.json"
    before_manifest = manifest_path.read_bytes()
    before_version = (directory / "versions" / str(first["version"]) / "resume.txt").read_bytes()

    bad = Completion({**selected(), "selected_entry_ids": ["resume:not-real"]})
    with pytest.raises(ValueError, match="unknown resume source ID"):
        prepare_application(
            posting(description_html="changed"), profile(), directory, provider="claude", completion=bad
        )

    assert len(bad.calls) == 1
    assert manifest_path.read_bytes() == before_manifest
    assert (directory / "versions" / str(first["version"]) / "resume.txt").read_bytes() == before_version


@pytest.mark.parametrize(
    "reply",
    [
        ["experience:lab"],
        {"entries": ["education:example-university"], "bullets": []},
        {"selected_entry_ids": ["education:example-university"], "selected_bullet_ids": [], "rewrite": "no"},
        {"selected_entry_ids": "education:example-university", "selected_bullet_ids": []},
    ],
)
def test_selection_reply_must_match_the_grounded_draft_schema(
    tmp_path: Path, reply: object
) -> None:
    with pytest.raises(ValueError):
        prepare_application(
            posting(), profile(), tmp_path / "application", provider="claude", completion=Completion(reply)
        )


def test_model_cannot_drop_confirmed_education_when_the_profile_has_one(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="every confirmed education"):
        prepare_application(
            posting(),
            profile(),
            tmp_path / "application",
            provider="claude",
            completion=Completion({**selected(), "selected_entry_ids": ["experience:lab"]}),
        )


def test_changed_job_gets_an_immutable_new_version(tmp_path: Path) -> None:
    directory = tmp_path / "application"
    first = prepare_application(
        posting(), profile(), directory, provider="api", completion=Completion(selected())
    )
    second = prepare_application(
        posting(title="Chemistry Intern"),
        profile(),
        directory,
        provider="api",
        completion=Completion(selected(bullets=["bullet:gcms"])),
    )

    assert first["version"] != second["version"]
    assert (directory / "versions" / str(first["version"]) / "resume.pdf").is_file()
    assert (directory / "versions" / str(second["version"]) / "resume.pdf").is_file()
    assert json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["version"] == second["version"]


def test_optional_letter_uses_only_selected_facts_and_is_paginated(tmp_path: Path) -> None:
    long_profile = profile()
    resume = dict(long_profile.resume)
    experience = resume["experience"]
    assert isinstance(experience, list)
    source = experience[0]
    assert isinstance(source, dict)
    source["bullets"] = [
        {"id": f"bullet:{index}", "text": f"Confirmed laboratory fact {index}."}
        for index in range(70)
    ]
    long_profile = Profile(name=long_profile.name, directory=long_profile.directory, resume=resume)
    reply = selected(bullets=[{"source_id": bullet["id"], "text": bullet["text"]} for bullet in source["bullets"]], letter=True)
    reply["letter_paragraphs"].append({"source_ids": ["bullet:0"], "text": "Confirmed laboratory fact 0."})
    completion = Completion(reply)
    result = prepare_application(
        posting(), long_profile, tmp_path / "application", provider="claude", cover_letter=True, completion=completion
    )

    version_dir = tmp_path / "application" / "versions" / str(result["version"])
    letter = (version_dir / "letter.txt").read_text(encoding="utf-8")
    assert "Research Intern" in letter and "Acme Labs" in letter
    assert "Confirmed laboratory fact 0." in letter
    assert "Invented an unconfirmed accomplishment." not in letter
    assert result["letterText"] == letter
    assert (version_dir / "resume.pdf").is_file()
    assert len(PdfReader(version_dir / "resume.pdf").pages) > 1


def test_a_profile_without_education_can_still_prepare_confirmed_experience(tmp_path: Path) -> None:
    base = profile()
    resume = dict(base.resume)
    resume.pop("education")
    no_education = Profile(name=base.name, directory=base.directory, resume=resume)
    completion = Completion(
        {**selected(bullets=["bullet:gcms"]), "selected_entry_ids": ["experience:lab"]}
    )

    result = prepare_application(
        posting(), no_education, tmp_path / "application", provider="claude", completion=completion
    )

    resume_text = str(result["resumeText"])
    assert "EXPERIENCE" in resume_text
    assert "EDUCATION" not in resume_text
    assert "Operated GC-MS for sample analysis." in resume_text


def test_wrong_provider_does_not_fall_through_to_a_runtime(tmp_path: Path) -> None:
    completion = Completion(selected())
    with pytest.raises(ValueError, match="provider must be one of"):
        prepare_application(posting(), profile(), tmp_path / "application", provider="other", completion=completion)
    assert completion.calls == []


def test_same_inputs_reuse_the_complete_immutable_version(tmp_path: Path) -> None:
    directory = tmp_path / "application"
    first_completion = Completion(selected())
    first = prepare_application(posting(), profile(), directory, provider="codex", completion=first_completion)
    second_completion = Completion({"selected_entry_ids": ["resume:not-called"]})
    second = prepare_application(posting(), profile(), directory, provider="codex", completion=second_completion)

    assert second == first
    assert first_completion.calls and second_completion.calls == []


@pytest.mark.parametrize("entry_ids,bullet_ids", [
    (["education:example-university", "experience:draft"], []),
    (["education:example-university", "experience:unconfirmed"], []),
    (["education:example-university", "experience:lab"], ["bullet:draft"]),
])
def test_explicitly_withheld_sources_never_reach_the_prompt_or_export(tmp_path, entry_ids, bullet_ids):
    completion = Completion({**selected(bullets=bullet_ids), "selected_entry_ids": entry_ids})
    with pytest.raises(ValueError, match="unknown resume source ID"):
        prepare_application(posting(), profile(), tmp_path, provider="claude", completion=completion)
    prompt = completion.calls[0][0]
    for text in ("Draft Employer", "Unconfirmed Employer", "Invented an unconfirmed accomplishment", "bullet:draft"):
        assert text not in prompt
    assert not (tmp_path / "manifest.json").exists()


def test_draft_false_and_recursive_confirmed_contact_and_fields(tmp_path):
    candidate = profile()
    candidate.resume["experience"][0]["draft"] = False
    candidate.resume["experience"][0]["notes"] = [
        {"text": "Private draft note", "draft": True},
        {"text": "Confirmed extra fact", "draft": False},
    ]
    candidate.resume["contact"]["phone"] = {"text": "Secret draft phone", "confirmed": False}
    candidate.resume["contact"]["status"] = "confirmed"
    completion = Completion(selected(bullets=["bullet:gcms"], letter=True))
    result = prepare_application(posting(), candidate, tmp_path, provider="codex", cover_letter=True, completion=completion)
    assert "Confirmed extra fact" in result["resumeText"]
    for hidden in ("Private draft note", "Secret draft phone", "False", "True"):
        assert hidden not in result["resumeText"]
        assert hidden not in completion.calls[0][0]
    assert "dana@example.test" in result["resumeText"]
    assert result["letterText"].count("- ") <= 3


@pytest.mark.parametrize("mutation", ["draft-profile", "unconfirmed-contact", "empty-facts", "duplicate-id"])
def test_invalid_profile_is_rejected_before_model_work(tmp_path, mutation):
    candidate = profile()
    if mutation == "draft-profile":
        candidate = replace(candidate, resume={**candidate.resume, "draft": True})
    elif mutation == "unconfirmed-contact":
        candidate.resume["contact"]["confirmed"] = False
    elif mutation == "empty-facts":
        candidate = replace(candidate, resume={**candidate.resume, "education": [], "experience": []})
    else:
        candidate.resume["experience"][0]["bullets"][0]["id"] = "education:example-university"
    completion = Completion(selected())
    with pytest.raises(ValueError):
        prepare_application(posting(), candidate, tmp_path, provider="api", completion=completion)
    assert not completion.calls


def test_profile_stamp_matches_application_open_and_memory_edits_invalidate_cache(tmp_path):
    from venator.match.store import filters_version
    candidate = profile()
    directory = tmp_path / "application"
    first = prepare_application(posting(), candidate, directory, provider="codex", completion=Completion(selected()))
    assert first["profile_version"] == filters_version(candidate.constraints_path, candidate.targeting_path)
    candidate = replace(candidate, resume={**candidate.resume, "name": "Renamed Candidate"})
    completion = Completion(selected())
    second = prepare_application(posting(), candidate, directory, provider="codex", completion=completion)
    assert len(completion.calls) == 2
    assert second["version"] != first["version"]
    assert "Renamed Candidate" in second["resumeText"]


def test_damaged_pdf_does_not_count_as_a_valid_reusable_manifest(tmp_path):
    first = prepare_application(posting(), profile(), tmp_path, provider="codex", completion=Completion(selected()))
    (tmp_path / "versions" / first["version"] / "resume.pdf").write_bytes(b"damaged")
    completion = Completion(selected())
    second = prepare_application(posting(), profile(), tmp_path, provider="codex", completion=completion)
    assert len(completion.calls) == 2
    assert second["version"] != first["version"]
    assert PdfReader(tmp_path / "versions" / second["version"] / "resume.pdf").pages


def test_runtime_type_error_is_not_retried_or_fallen_back(tmp_path):
    calls = []
    def broken(*args, **kwargs):
        calls.append(kwargs["environ"]["VENATOR_LLM_RUNTIME"])
        raise TypeError("synthetic provider error")
    with pytest.raises(TypeError, match="synthetic provider error"):
        prepare_application(posting(), profile(), tmp_path, provider="api", completion=broken)
    assert calls == ["api"]


def test_pdf_preserves_unicode_and_every_long_page_stays_in_bounds(tmp_path):
    import pdfplumber
    candidate = profile()
    candidate = replace(candidate, resume={**candidate.resume, "name": "Zoë García"})
    bullets = [
        {"id": f"long:{index}", "text": f"Verified fact {index}: measured 5 µL samples and α signals. " + "Laboratory" * 25}
        for index in range(40)
    ]
    candidate.resume["experience"][0]["bullets"] = bullets
    completion = Completion(selected(bullets=[{"source_id": bullet["id"], "text": bullet["text"]} for bullet in bullets]))
    result = prepare_application(posting(), candidate, tmp_path, provider="claude", completion=completion)
    path = tmp_path / "versions" / result["version"] / "resume.pdf"
    with pdfplumber.open(path) as document:
        assert len(document.pages) >= 3
        text = "\n".join(page.extract_text() for page in document.pages)
        assert "Zoë García" in text
        assert "5 µL samples and α signals" in text
        assert "Verified fact 39:" in text
        for page in document.pages:
            assert all(35 <= char["x0"] <= char["x1"] <= page.width - 35 for char in page.chars)
            assert all(35 <= char["top"] <= char["bottom"] <= page.height - 35 for char in page.chars)


def test_unrenderable_unicode_preserves_previous_manifest(tmp_path):
    first = prepare_application(posting(), profile(), tmp_path, provider="claude", completion=Completion(selected()))
    before = (tmp_path / "manifest.json").read_bytes()
    candidate = profile()
    candidate = replace(candidate, resume={**candidate.resume, "name": "Candidate \U0001f9ea"})
    with pytest.raises(ValueError, match="font cannot render"):
        prepare_application(posting(), candidate, tmp_path, provider="claude", completion=Completion(selected()))
    assert (tmp_path / "manifest.json").read_bytes() == before
    assert (tmp_path / "versions" / first["version"] / "resume.pdf").is_file()
    assert not list((tmp_path / "versions").glob(".*.tmp"))


@pytest.mark.parametrize("marker", [{"owner_confirmed": False}, {"confirmed": "false"}, {"draft": "true"}, {"status": "pending"}, {"include": False}])
def test_all_explicit_withholding_markers_exclude_sources(tmp_path, marker):
    candidate = profile()
    candidate.resume["experience"][0].update(marker)
    completion = Completion(selected())
    with pytest.raises(ValueError, match="unknown resume source ID"):
        prepare_application(posting(), candidate, tmp_path, provider="api", completion=completion)
    assert "experience:lab" not in completion.calls[0][0]


def test_portable_embedded_font_fallback_keeps_unicode(tmp_path, monkeypatch):
    import venator.tailor.prepare as prepare
    def unavailable_fonts():
        raise FileNotFoundError("synthetic missing system fonts")
    monkeypatch.setattr(prepare, "register_fonts", unavailable_fonts)
    candidate = replace(profile(), resume={**profile().resume, "name": "Zoë García"})
    result = prepare_application(posting(), candidate, tmp_path, provider="claude", completion=Completion(selected()))
    document = PdfReader(tmp_path / "versions" / result["version"] / "resume.pdf")
    assert "Zoë García" in document.pages[0].extract_text()


def test_duplicate_json_keys_are_not_a_valid_draft_selection(tmp_path):
    reply = '{"selected_entry_ids": [], "selected_entry_ids": ["education:example-university"], "selected_bullet_ids": []}'
    with pytest.raises(ValueError, match="duplicate key"):
        prepare_application(posting(), profile(), tmp_path, provider="claude", completion=Completion(reply))


def test_source_font_failure_happens_before_provider_call(tmp_path):
    candidate = replace(profile(), resume={**profile().resume, "name": "Candidate \U0001f9ea"})
    completion = Completion(selected())
    with pytest.raises(ValueError, match="font cannot render"):
        prepare_application(posting(), candidate, tmp_path, provider="claude", completion=completion)
    assert completion.calls == []


def test_scientific_superscripts_render_raised_without_changing_source_text(tmp_path):
    import pdfplumber
    candidate = profile()
    original = "Calculated culture density (~3.3 × 10⁷ CFU/mL)."
    candidate.resume["experience"][0]["bullets"][0]["text"] = original
    completion = Completion(selected(bullets=[{"source_id": "bullet:gcms", "text": original}]))
    result = prepare_application(posting(), candidate, tmp_path, provider="claude", completion=completion)
    assert original in result["resumeText"]
    with pdfplumber.open(tmp_path / "versions" / result["version"] / "resume.pdf") as document:
        chars = document.pages[0].chars
        marker = next(i for i in range(len(chars)-2) if ''.join(c['text'] for c in chars[i:i+3]) == '107')
        base, exponent = chars[marker], chars[marker+2]
        assert exponent['size'] < base['size']
        assert exponent['top'] < base['top']
