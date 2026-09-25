from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.browser.fill import execute_fill, load_plan
from venator.browser.recon import recon_form

POSTING_KEY = "greenhouse:fixture:123"
FIXTURE = Path(__file__).parent / "fixtures" / "application-form.html"


def write_plan(tmp_path: Path, plan: dict) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def fixture_plan() -> dict:
    return {
        "posting_key": POSTING_KEY,
        "apply_url": FIXTURE.resolve().as_uri(),
        "never_submit": True,
        "fields": [
            {
                "label": "Full Name",
                "kind": "text",
                "value": "Test Person",
                "source": "fixture:name",
            },
            {
                "label": "Notes",
                "kind": "textarea",
                "value": "Dry-run note",
                "source": "fixture:notes",
            },
            {
                "label": "Discipline",
                "kind": "select",
                "value": "Data",
                "source": "fixture:discipline",
            },
            {
                "label": "Contact Preference",
                "kind": "radio",
                "value": "Phone",
                "source": "fixture:contact",
            },
            {
                "label": "Product Updates",
                "kind": "checkbox",
                "value": True,
                "source": "fixture:updates",
            },
            {
                "label": "Resume",
                "kind": "file",
                "value": "/never/read/resume.pdf",
                "source": "fixture:resume",
            },
        ],
        "unmapped": [
            {"label": "Custom Question", "reason": "no fixture fact covers this"}
        ],
    }


def test_recon_inventories_all_contract_field_kinds(tmp_path: Path) -> None:
    spec = recon_form(POSTING_KEY, FIXTURE.resolve().as_uri(), tmp_path)

    fields = {field["label"]: field for field in spec["fields"]}
    assert spec["posting_key"] == POSTING_KEY
    assert spec["captcha"] == "none"
    assert set(fields) == {
        "Full Name",
        "Notes",
        "Discipline",
        "Contact Preference",
        "Product Updates",
        "Resume",
    }
    assert fields["Full Name"]["kind"] == "text"
    assert fields["Full Name"]["required"] is True
    assert fields["Notes"]["kind"] == "textarea"
    assert fields["Notes"]["required"] is False
    assert fields["Discipline"]["kind"] == "select"
    assert fields["Discipline"]["options"] == ["Lab", "Data"]
    assert fields["Contact Preference"]["kind"] == "radio"
    assert fields["Contact Preference"]["options"] == ["Email", "Phone"]
    assert fields["Product Updates"]["kind"] == "checkbox"
    assert fields["Product Updates"]["options"] is None
    assert fields["Resume"]["kind"] == "file"
    assert fields["Resume"]["required"] is True
    output = tmp_path / POSTING_KEY.replace(":", "-")
    assert (output / "form-spec.json").exists()
    assert (output / "blank.png").stat().st_size > 0


def test_fill_executes_plan_accounts_for_every_entry_and_never_submits(
    tmp_path: Path,
) -> None:
    plan = fixture_plan()
    report = execute_fill(POSTING_KEY, write_plan(tmp_path, plan), tmp_path / "out")

    statuses = {(field["label"], field["origin"]): field for field in report["fields"]}
    assert statuses[("Full Name", "plan")]["status"] == "filled"
    assert statuses[("Notes", "plan")]["status"] == "filled"
    assert statuses[("Discipline", "plan")]["status"] == "filled"
    assert statuses[("Contact Preference", "plan")]["status"] == "filled"
    assert statuses[("Product Updates", "plan")]["status"] == "filled"
    assert statuses[("Resume", "plan")] == {
        "label": "Resume",
        "kind": "file",
        "source": "fixture:resume",
        "status": "skipped",
        "reason": "file staging deferred",
        "origin": "plan",
    }
    assert statuses[("Custom Question", "unmapped")]["status"] == "skipped"
    assert len(report["fields"]) == len(plan["fields"]) + len(plan["unmapped"])
    assert report["never_submit"] is True
    assert report["submit_events"] == 0
    assert report["blocked_state_changing_requests"] == 0
    output = tmp_path / "out" / POSTING_KEY.replace(":", "-")
    assert (output / "fill-report.json").exists()
    assert (output / "filled.png").stat().st_size > 0


def test_unsafe_submit_selector_is_failed_without_activation(tmp_path: Path) -> None:
    plan = fixture_plan()
    plan["fields"] = [
        {
            "label": "Unsafe",
            "kind": "checkbox",
            "value": True,
            "source": "fixture:unsafe",
            "selector": "#submit-control",
        }
    ]
    plan["unmapped"] = []

    report = execute_fill(POSTING_KEY, write_plan(tmp_path, plan), tmp_path / "out")

    assert report["fields"][0]["status"] == "failed"
    assert "unsafe non-fill control" in report["fields"][0]["reason"]
    assert report["submit_events"] == 0


def test_non_exact_select_value_is_skipped_without_guessing(tmp_path: Path) -> None:
    plan = fixture_plan()
    plan["fields"] = [
        {
            "label": "Discipline",
            "kind": "select",
            "value": "data",
            "source": "fixture:wrong-case-option",
        }
    ]
    plan["unmapped"] = []

    report = execute_fill(POSTING_KEY, write_plan(tmp_path, plan), tmp_path / "out")

    assert report["fields"][0]["status"] == "skipped"
    assert report["fields"][0]["reason"] == "value is not an exact FormSpec option"
    assert report["submit_events"] == 0


@pytest.mark.parametrize("never_submit", [None, False, 1, "true"])
def test_plan_without_literal_never_submit_is_refused(
    tmp_path: Path,
    never_submit: object,
) -> None:
    plan = fixture_plan()
    if never_submit is None:
        plan.pop("never_submit")
    else:
        plan["never_submit"] = never_submit

    with pytest.raises(ValueError, match="literal true"):
        load_plan(write_plan(tmp_path, plan))
