"""Synthetic Greenhouse API/DOM questions; no employer requests."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from venator.answers.store import append, latest, match, pinned_current, question_key, row_hash
from venator.apply.classify import reserved
from venator.apply.form import greenhouse_questions, review_questions
from venator.match.store import filters_version


def test_greenhouse_api_field_names_and_buckets(tmp_path: Path):
    posting = {"key": "greenhouse:lilasciences:123", "board": "lilasciences", "company": "Lila Sciences"}
    payload = {"id": 123, "questions": [
        {"label": "Have you ever worked for Lila?", "fields": [{"name": "question_17", "type": "multi_value_single_select", "values": [{"label": "Yes"}, {"label": "No"}]}]},
        {"label": "Why Lila?", "fields": [{"name": "question_18", "type": "textarea", "maxlength": 100}]},
        {"label": "Will you need visa sponsorship?", "fields": [{"name": "question_19", "type": "multi_value_single_select", "values": [{"label": "Yes"}, {"label": "No"}]}]},
        {"label": "I consent to the privacy policy", "fields": [{"name": "question_20", "type": "input_text"}]},
    ]}
    def fetch(url: str) -> bytes:
        assert url == "https://boards-api.greenhouse.io/v1/boards/lilasciences/jobs/123?questions=true"
        return json.dumps(payload).encode()
    questions = greenhouse_questions(posting, fetch=fetch)
    assert [row["name"] for row in questions] == ["question_17", "question_18", "question_19", "question_20"]
    directory = tmp_path / "answers"
    row = append(directory, "fixture", text="Have you ever worked for Lila?", kind="select", answer="No",
                 board="lilasciences", company="Lila Sciences")
    profile = SimpleNamespace(identifier="fixture", resume={}, constraints={"work_authorization": {"requires_sponsorship": True}})
    reviewed, pins = review_questions(posting, profile, directory, questions)
    assert [item["bucket"] for item in reviewed] == ["library", "essay", "reserved", "reserved"]
    assert [item["answer"] for item in reviewed] == ["No", None, "Yes", None]
    assert pins == [{"id": row["id"], "question": row["question"], "scope": row["scope"], "hash": row_hash(row)}]
    assert pinned_current(directory, "fixture", pins)
    append(directory, "fixture", text="Have you ever worked for Lila?", kind="select", answer=None,
           board="lilasciences", company="Lila Sciences")
    assert not pinned_current(directory, "fixture", pins)


def test_scope_company_short_name_options_eeo_and_filters_version(tmp_path: Path):
    directory = tmp_path / "answers"
    constraints = tmp_path / "constraints.yaml"
    targeting = tmp_path / "targeting.yaml"
    constraints.write_text("work_authorization: {}\n")
    targeting.write_text("profile:\n  id: fixture\n")
    before = filters_version(constraints, targeting)
    append(directory, "fixture", text="Have you ever worked for Lila?", kind="select", answer="No",
           board="lilasciences", company="Lila Sciences")
    append(directory, "fixture", text="Have you ever worked for CVS?", kind="select", answer="Yes",
           board="cvs", company="CVS Health")
    append(directory, "fixture", text="Country", kind="select", answer="US", board="lilasciences", universal=True)
    append(directory, "fixture", text="Why Boston?", kind="text", answer="I want to work there.",
           board="lilasciences", universal=True)
    entries = latest(directory, "fixture")
    lookup = lambda text, board, company, options: match(entries, text=text, board=board, company=company,
                                                          kind="select", options=options)
    assert lookup("Have you ever worked for Lila?", "lilasciences", "Lila Sciences", ["Yes", "No"])["answer"] == "No"
    assert lookup("Have you ever worked for Kyverna?", "kyverna", "Kyverna Therapeutics", ["Yes", "No"]) is None
    assert lookup("Have you ever worked for CVS?", "cvs", "CVS Health", ["Yes", "No"])["answer"] == "Yes"
    assert lookup("Country", "unknown", "Unknown", ["US", "Canada"])["answer"] == "US"
    assert lookup("Country", "unknown", "Unknown", ["United States", "Canada"]) is None
    assert question_key("Why Lila Sciences?", "Lila Sciences") == question_key("Why Lila?", "Lila Sciences")
    with pytest.raises(ValueError, match="employer-specific"):
        append(directory, "fixture", text="Why Lila?", kind="textarea", answer="For their work.",
               board="lilasciences", company="Lila Sciences", universal=True)
    from venator.answers.store import promote
    board_row = append(directory, "fixture", text="How did you hear about us?", kind="text",
                       answer="Career fair", board="lilasciences")
    promoted = promote(directory, "fixture", board_row)
    assert promoted["scope"] == "any"
    assert latest(directory, "fixture")[(board_row["question"], "lilasciences")]["answer"] is None
    with pytest.raises(ValueError, match="employer-specific"):
        promote(directory, "fixture", entries[(question_key("Have you ever worked for Lila?", "Lila Sciences"), "lilasciences")])
    assert before == filters_version(constraints, targeting)
    with pytest.raises(ValueError, match="cannot be stored"):
        append(directory, "fixture", text="Will you need visa sponsorship?", kind="select", answer="No", board="cvs")
    append(directory, "fixture", text="Gender", kind="select", answer="Decline", board="cvs", eeo=True)
    assert match(latest(directory, "fixture"), text="Gender", board="cvs", company="CVS",
                 kind="select", options=["Decline"]) is not None
    assert match({("gender", "cvs"): {"answer": "Decline", "kind": "select"}},
                 text="Gender", board="cvs", company="CVS", kind="select", options=["Decline"]) is None


@pytest.mark.parametrize("label,category", [
    ("Will you require sponsorship now or in the future?", "sponsorship"),
    ("Are you legally authorized to work in the US?", "authorization"),
    ("Can you work here without visa sponsorship?", "authorization_sponsorship"),
    ("Voluntary Self Identify", "eeo"), ("I acknowledge the privacy policy", "consent"),
    ("I confirm this attestation", "consent"),
    ("Do you anticipate needing employer visa support at any point?", "sponsorship"),
    ("Do you have the legal right to work?", "authorization"),
    ("Verification code", "sign_in"),
])
def test_reserved_wording_never_reaches_general_answers(label: str, category: str):
    assert reserved(label) == category


def test_workday_questions_are_scoped_per_tenant_and_profile(tmp_path: Path):
    from venator.apply.form import remember_workday_form, workday_questions

    posting = {"key": "workday:acme.wd1~AcmeCareers:R123456", "company": "Acme Labs"}
    rows = [{"name": "source", "label": "How Did You Hear About Us?", "kind": "text", "required": True,
             "options": None, "maxlength": None}]
    memory = tmp_path / "forms"
    remember_workday_form(memory, posting, rows, "fixture")
    assert workday_questions(memory, posting, "fixture") == rows
    assert (memory / ".profile").read_text().strip() == "fixture"
    # Another requisition on the same tenant reads the same question set.
    assert workday_questions(memory, {**posting, "key": "workday:acme.wd1~AcmeCareers:R2"}, "fixture") == rows
    # Another tenant gets nothing.
    assert workday_questions(memory, {"key": "workday:other.wd1~Other:R1"}, "fixture") == []
    with pytest.raises(ValueError, match="another Profile"):
        workday_questions(memory, posting, "different")
    # An unchanged set is not appended twice.
    remember_workday_form(memory, posting, rows, "fixture")
    assert len(list(memory.glob("*.jsonl"))) == 1
    assert len((memory / next(iter(memory.glob("*.jsonl"))).name).read_text().splitlines()) == 1
    # A later wizard step's questions merge into the tenant's set, not replace it.
    later = {"name": "question_2", "label": "Do you require sponsorship?", "kind": "select",
             "required": True, "options": None, "maxlength": None}
    remember_workday_form(memory, posting, [later], "fixture")
    assert [row["name"] for row in workday_questions(memory, posting, "fixture")] == ["source", "question_2"]


def test_universal_eeo_rows_apply_on_every_tenant(tmp_path: Path):
    directory = tmp_path / "answers"
    row = append(directory, "fixture", text="Gender", kind="select", answer="Woman",
                 board="acme.wd1~AcmeCareers", universal=True, eeo=True)
    assert row["scope"] == "any" and row["eeo"] is True
    entries = latest(directory, "fixture")
    matched = match(entries, text="Gender", board="other.wd5~OtherSite", company="Other Corp",
                    kind="select", options=["Woman", "Man", "Decline"])
    assert matched is not None and matched["answer"] == "Woman"
    # Without an EEO row there is nothing to match, and a general row cannot be
    # stored for the same reserved question at all.
    with pytest.raises(ValueError, match="cannot be stored"):
        append(directory, "fixture", text="Gender", kind="select", answer="Woman",
               board="other.wd5~OtherSite")
