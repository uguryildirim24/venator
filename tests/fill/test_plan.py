from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from venator.fill.plan import (
    STANDARD_FIELDS,
    create_fill_plan,
    load_form_spec,
    plan_posting,
    sanitize_posting_key,
    standard_form_spec,
    validate_form_spec,
)

RESUME = {
    "name": 'Alex Example',
    "contact": {
        "location": "Example City, MA",
        "phone": "555-010-1234",
        "email": "alex@example.test",
        "linkedin": "www.linkedin.com/in/alex-example",
    },
    "education": [
        {
            "org": "Example University",
            "date": "May 2027 (Expected)",
            "degree": "Bachelor of Science in Biochemistry",
            "gpa": "3.86/4.00",
        }
    ],
}
CONSTRAINTS = {
    "work_authorization": {
        "status": "F-1 international student",
        "requires_sponsorship": True,
    },
    "option_aliases": {
        "school": {"Example University": ["Example College"]},
        "degree": {"Bachelor of Science in Biochemistry": ["Bachelor's Degree"]},
        "sponsorship": {"Yes": ["Yes, but not until the future"]},
    },
}
POSTING_KEY = "source:board:123"
POSTING_URL = "https://example.test/apply/123"


def form_spec(fields: list[dict], *, apply_url: str = "https://captured.test/form") -> dict:
    return {
        "posting_key": POSTING_KEY,
        "apply_url": apply_url,
        "captured_at": "2026-08-18T12:00:00+00:00",
        "captcha": "none",
        "fields": fields,
    }


def text_field(label: str) -> dict:
    return {
        "label": label,
        "kind": "text",
        "required": True,
        "selector": f"#{label.casefold().replace(' ', '-')}",
        "options": None,
    }


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def test_standard_fallback_includes_complete_greenhouse_set(tmp_path: Path) -> None:
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"resume")
    spec = standard_form_spec(POSTING_KEY, POSTING_URL)

    plan = create_fill_plan(
        POSTING_KEY,
        POSTING_URL,
        spec,
        RESUME,
        CONSTRAINTS,
        resume_file=resume_file,
    )

    labels = {field["label"] for field in plan["fields"]} | {
        field["label"] for field in plan["unmapped"]
    }
    assert labels == {field["label"] for field in STANDARD_FIELDS}
    assert len(plan["fields"]) == 11
    assert plan["unmapped"] == [
        {
            "label": "Are you legally authorized to work in the United States?",
            "reason": (
                "owner has not stated constraints.yaml:work_authorization.authorized_to_work, "
                "so there is no binary authorization answer to give — it is left for the "
                "Owner, never answered 'No'"
            ),
        }
    ]
    sponsorship = next(
        field for field in plan["fields"] if "sponsorship" in field["label"].casefold()
    )
    assert sponsorship["value"] == "Yes"


def test_fill_plan_has_exact_contract_shape_and_posting_apply_url() -> None:
    spec = form_spec([text_field("Email"), text_field("Unknown Question")])

    plan = create_fill_plan(POSTING_KEY, POSTING_URL, spec, RESUME, CONSTRAINTS)

    assert set(plan) == {"posting_key", "apply_url", "never_submit", "fields", "unmapped"}
    assert plan["posting_key"] == POSTING_KEY
    assert plan["apply_url"] == POSTING_URL
    assert plan["apply_url"] != spec["apply_url"]
    assert plan["never_submit"] is True
    assert plan["fields"] == [
        {
            "label": "Email",
            "kind": "text",
            "value": "alex@example.test",
            "source": "resume.yaml:contact.email",
        }
    ]
    assert plan["unmapped"] == [
        {"label": "Unknown Question", "reason": "no Profile fact covers this"}
    ]


def test_plan_posting_is_byte_deterministic_and_sanitizes_output_path(tmp_path: Path) -> None:
    postings_dir = tmp_path / "postings"
    applications_dir = tmp_path / "applications"
    out_dir = tmp_path / "fill"
    resume_path = tmp_path / "resume.yaml"
    constraints_path = tmp_path / "constraints.yaml"
    form_spec_path = tmp_path / "form-spec.json"
    write_jsonl(
        postings_dir / "2026-08-18.jsonl",
        [{"key": POSTING_KEY, "url": POSTING_URL}],
    )
    resume_path.write_text(yaml.safe_dump(RESUME, sort_keys=False), encoding="utf-8")
    constraints_path.write_text(yaml.safe_dump(CONSTRAINTS, sort_keys=False), encoding="utf-8")
    form_spec_path.write_text(
        json.dumps(form_spec([text_field("First Name"), text_field("Email")])),
        encoding="utf-8",
    )

    first, first_path = plan_posting(
        POSTING_KEY,
        form_spec_path=form_spec_path,
        out_dir=out_dir,
        postings_dir=postings_dir,
        resume_path=resume_path,
        constraints_path=constraints_path,
        applications_dir=applications_dir,
    )
    first_bytes = first_path.read_bytes()
    second, second_path = plan_posting(
        POSTING_KEY,
        form_spec_path=form_spec_path,
        out_dir=out_dir,
        postings_dir=postings_dir,
        resume_path=resume_path,
        constraints_path=constraints_path,
        applications_dir=applications_dir,
    )

    assert first == second
    assert first_path == second_path == out_dir / "source-board-123" / "plan.json"
    assert second_path.read_bytes() == first_bytes
    assert sanitize_posting_key(POSTING_KEY) == "source-board-123"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec: spec.pop("fields"),
        lambda spec: spec.update(posting_key="other:key"),
        lambda spec: spec.update(captcha="solved"),
        lambda spec: spec["fields"][0].update(kind="date"),
        lambda spec: spec["fields"][0].update(options=["unexpected"]),
    ],
)
def test_form_spec_validation_rejects_contract_violations(mutate) -> None:
    spec = form_spec([text_field("Email")])
    mutate(spec)

    with pytest.raises(ValueError):
        validate_form_spec(spec, POSTING_KEY)


def test_load_form_spec_reports_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "form-spec.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid FormSpec JSON"):
        load_form_spec(path, POSTING_KEY)
