"""Workday fill: the Autofill field, per-step facts, and nothing that submits.

The fixture carries Workday automation ids and step order with made-up person
values. The tests are the only code that ever presses the fixture's footer
button, exactly as the person is the only one who presses Workday's. A drop-down choice happens in the field's own
listbox, which is not a form action.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from venator.answers.store import append, latest
from venator.browser.handoff import STATE_NAME, _install_confirmation_watch, _navigation_and_fill
from venator.browser.workday import (
    WorkdaySession,
    candidate_home_marker,
    fill_current_step,
    sign_in_page,
    workday_identity,
)

FIXTURES = Path(__file__).parent / "fixtures" / "workday"
APPLY_STEPS = FIXTURES / "apply-steps.html"
REPEAT_EMPLOYER = FIXTURES / "repeat-employer.html"
SIGN_IN = FIXTURES / "sign-in.html"
FORBIDDEN_BUTTONS = FIXTURES / "forbidden-buttons.html"

POSTING = {
    "key": "workday:fixture.wd1~FixtureSite:R123456",
    "source": "workday",
    "board": "fixture.wd1~FixtureSite",
    "company": "Fixture Labs",
    "title": "Research Associate",
}

RESUME = {
    "name": "Avery Example",
    "contact": {
        "email": "avery@example.test",
        "phone": "(555) 010-0100",
        "linkedin": "www.linkedin.com/in/avery-example",
    },
    "experience": [
        {
            "org": "Fixture Laboratory",
            "role": "Chemistry Lab Assistant",
            "location": "Chicago, IL",
            "dates": "September 2026 - Present",
            "bullets": ["Ran the introductory chemistry lab.", "Prepared samples for class."],
        },
        {
            "org": "Fixture Research Center",
            "role": "Research Technician",
            "location": "Madison, WI",
            "dates": "February 2026 - May 2026",
            "bullets": ["Catalogued samples under supervision."],
        },
    ],
    "education": [
        {"org": "Example University", "degree": "Bachelor of Science in Biochemistry",
         "gpa": "3.70/4.00", "primary": True},
    ],
}

CONTACT = {
    "legal_name": {"first": "Avery", "middle": "Quinn", "last": "Example"},
    "address": {
        "line1": "100 Example Avenue", "line2": "Suite 2", "city": "Chicago",
        "state": "Illinois", "postal_code": "60601", "country": "United States of America",
    },
    "phone_type": "Mobile",
    "languages": [
        {"language": "English", "proficiency": "Native or bilingual"},
        {"language": "Spanish", "proficiency": "Professional working proficiency"},
    ],
}

CONSTRAINTS = {
    "work_authorization": {"requires_sponsorship": True, "authorized_to_work": True},
}

#: The tailored version this Posting's Apply produced. Bullets are this
#: version's wording; a correction goes toward them, never back to the base.
PREPARED = {
    "name": "Avery Example",
    "contact": {},
    "experience": [
        {**RESUME["experience"][0],
         "bullets": ["Tailored: ran the introductory chemistry lab.", "Tailored: prepared samples."]},
        RESUME["experience"][1],
    ],
    "education": RESUME["education"],
}

#: Controls and labels Venator never presses, whatever a wrapper calls itself.
_FORBIDDEN_IDS = {"pageFooterNextButton", "pageFooterBackButton", "delete-file", "add-button"}
_FORBIDDEN_TEXT = {"submit", "save and continue", "continue", "back", "delete", "add", "add another"}


@pytest.fixture(autouse=True)
def workday_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VENATOR_HANDOFF_HEADLESS", "1")


def _request(tmp_path: Path, *, answers: list[dict] | None = None) -> dict:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 synthetic prepared resume")
    return {
        "posting": dict(POSTING),
        "resume_file": str(resume),
        "letter_file": None,
        "profile_resume": RESUME,
        "profile_constraints": CONSTRAINTS,
        "profile_contact": CONTACT,
        "answers": answers or [],
        "prepared_resume": PREPARED,
        "form_memory": str(tmp_path / "answers" / "forms"),
        "profile_id": "fixture",
        "allow_test_urls": True,
    }


def _library_rows(directory: Path) -> list[dict]:
    return list(latest(directory, "fixture").values())


def _walk_to(page, name: str) -> None:
    """The person pressing one footer button, never Venator."""
    assert page.locator("#active-step-label").inner_text() != name
    page.click("#next")
    page.wait_for_function(
        "document.getElementById('active-step-label').textContent === " + json.dumps(name)
    )


def _clicks(page) -> list[dict]:
    return list(page.evaluate("() => window.__fixtureClickDetails") or [])


def _forbidden_click(row: dict) -> bool:
    if row.get("id") in _FORBIDDEN_IDS or row.get("automation_id") in _FORBIDDEN_IDS:
        return True
    text = (row.get("text") or "").casefold().strip()
    return text in _FORBIDDEN_TEXT or text.startswith("delete ") or text.startswith("add another")


def test_workday_autofill_uploads_resume_and_fills_each_step_pressing_no_footer(tmp_path: Path) -> None:
    answers = tmp_path / "answers"
    append(answers, "fixture", text="How did you hear about us?", kind="text", answer="Job board",
           board=POSTING["board"], company=POSTING["company"])
    append(answers, "fixture", text="Did you work previously at Fixture Labs?", kind="select", answer="No",
           board=POSTING["board"], company=POSTING["company"])
    append(answers, "fixture", text="What is your notice period?", kind="select", answer="Two weeks",
           board=POSTING["board"], company=POSTING["company"])
    request = _request(tmp_path, answers=_library_rows(answers))
    page_url = "https://fixture.wd1.myworkdayjobs.com/en-US/FixtureSite/job/R123456/apply/autofillWithResume"
    posted = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        page = context.new_page()
        page.on("request", lambda item: posted.append(item.method) if item.method not in {"GET", "HEAD"} else None)
        # The fixture answers as this Posting's own tenant host, so a request the
        # fills caused would be visible at the network layer.
        page.route("**/*", lambda route: route.fulfill(body=APPLY_STEPS.read_text(), content_type="text/html"))
        request["posting"]["apply_url"] = page_url
        request["posting"]["url"] = page_url
        result, filled, _warnings = _navigation_and_fill(request, page)
        session = WorkdaySession(context, page, request, first_report=result.pop("_workday"))

        # Step 1: Autofill with Resume. The verified résumé goes into the file
        # field with set_input_files; nothing else on this step is pressed.
        session.tick()
        assert filled == session.total_filled == 1
        assert session.steps[0]["filled"] == 1
        assert any("Autofill field" in warning for warning in session.warnings)
        assert page.locator("#autofill-file").evaluate("el => el.files[0].name") == "resume.pdf"
        assert page.locator("#use-last-application").is_visible()
        assert page.evaluate("() => window.__fixtureSubmitEvents") == 0
        assert posted == []

        # Step 2: My Information. Confirmed facts fill blanks and correct the
        # parser's last name and city; each drop-down picks from its own listbox.
        _walk_to(page, "My Information")
        session.tick()
        assert page.locator("#name--legalName--firstName").input_value() == "Avery"
        assert page.locator("#name--legalName--lastName").input_value() == "Example"
        assert page.locator("#address--addressLine1").input_value() == "100 Example Avenue"
        assert page.locator("#address--addressLine2").input_value() == "Suite 2"
        assert page.locator("#address--city").input_value() == "Chicago"
        assert page.locator("#address--postalCode").input_value() == "60601"
        assert page.locator("#phoneNumber--phoneNumber").input_value() == "(555) 010-0100"
        assert page.locator("#source--source").input_value() == "Job board"
        assert page.locator("#previous-no").is_checked()
        assert page.locator("#country--country").inner_text() == "United States of America"
        assert page.locator("#address--countryRegion").inner_text() == "Illinois"
        assert page.locator("#phoneNumber--phoneType").inner_text() == "Mobile"
        report = session.steps[-1]
        corrected = {row["label"] for row in report["corrected"]}
        assert "Last Name" in corrected and "City" in corrected
        left = {row["label"] for row in report["left"]}
        assert "Country" not in left and "State" not in left
        assert any("Use My Last Application" in warning for warning in session.warnings)

        # Step 3: My Experience. Title qualifiers, the description and the dates
        # are corrected toward the prepared version, education gaps fill, the
        # fluent checkbox is checked only for the fluent language, and the
        # second file input is not touched. Degree fills from its own listbox;
        # English's Overall level has no exact option and is left for the person.
        _walk_to(page, "My Experience")
        session.tick()
        assert page.locator("#workExperience-10--jobTitle").input_value() == "Chemistry Lab Assistant"
        assert page.locator("#workExperience-10--roleDescription").input_value() == (
            "Tailored: ran the introductory chemistry lab.\nTailored: prepared samples."
        )
        assert "Ran the introductory chemistry lab." not in page.locator("#workExperience-10--roleDescription").input_value()
        assert page.locator("#workExperience-10--currentlyWorkHere").is_checked()
        assert not page.locator("#workExperience-11--currentlyWorkHere").is_checked()
        assert page.locator("#workExperience-10--startDate-dateSectionMonth-input").get_attribute("aria-valuenow") == "9"
        assert page.locator("#workExperience-10--startDate-dateSectionYear-input").get_attribute("aria-valuenow") == "2026"
        assert page.locator("#workExperience-11--startDate-dateSectionMonth-input").get_attribute("aria-valuenow") == "2"
        assert page.locator("#workExperience-11--endDate-dateSectionMonth-input").get_attribute("aria-valuenow") == "5"
        assert page.locator("#education-50--fieldOfStudy").input_value() == "Biochemistry"
        assert page.locator("#education-50--gradeAverage").input_value() == "3.70/4.00"
        assert page.locator("#education-50--degree").inner_text() == "Bachelor of Science in Biochemistry"
        assert page.locator("#language-31--native").is_checked()
        assert not page.locator("#language-32--native").is_checked()
        assert page.locator("#language-32--overall").inner_text() == "Professional working proficiency"
        assert page.locator("#experience-resume").evaluate("el => el.files.length") == 0
        assert page.locator("#socialNetworkAccounts--linkedInAccount").input_value() == (
            "www.linkedin.com/in/avery-example"
        )
        report = session.steps[-1]
        corrected = {row["label"] for row in report["corrected"]}
        assert any("Chemistry Lab Assistant" == row["to"] for row in report["corrected"])
        assert all(row["source"].startswith("prepared resume:") for row in report["corrected"])
        left = {row["label"]: row for row in report["left"]}
        assert "Degree" not in left
        assert left["English: Overall proficiency"]["reason"] == "no option matched exactly"

        # Step 4: Application Questions. Sponsorship and eligibility come from
        # the Profile's own statements; a drop-down question takes its reviewed
        # answer; the EEO field has no explicit EEO row.
        _walk_to(page, "Application Questions")
        session.tick()
        assert page.locator("#sponsor-yes").is_checked()
        assert not page.locator("#sponsor-no").is_checked()
        assert page.locator("#eligible-yes").is_checked()
        assert page.locator("#noticePeriod--noticePeriod").inner_text() == "Two weeks"
        assert page.locator("#gender--gender").input_value() == ""
        assert page.locator("#salary--salary").input_value() == ""

        # Steps 5 to 7: the wizard replaces its DOM; Venator keeps filling and
        # still presses no footer. Review's button reads Submit and is not pressed.
        _walk_to(page, "Voluntary Disclosures")
        session.tick()
        _walk_to(page, "Self Identify")
        session.tick()
        assert page.locator("#veteran--veteranStatus").inner_text() == "Select One"
        _walk_to(page, "Review")
        session.tick()
        assert page.locator("#next").inner_text() == "Submit"
        assert page.locator("#candidate-home").is_hidden()
        assert page.evaluate("() => window.__fixtureSubmitEvents") == 0
        assert posted == []
        assert result["submit_events"] == 0

        # Every non-footer click Venator made was inside a form field or its
        # popup listbox, and never on a Submit, Delete, Add or navigation control.
        person_clicks = [row for row in _clicks(page) if row.get("id") == "next"]
        assert len(person_clicks) == 6
        venator_clicks = [row for row in _clicks(page) if row.get("id") != "next"]
        assert venator_clicks
        assert all(row.get("in_field") for row in venator_clicks)
        assert not any(_forbidden_click(row) for row in venator_clicks)

        # The per-tenant memory keeps question metadata only, never a value.
        memory = sorted((tmp_path / "answers" / "forms").glob("*.jsonl"))
        assert memory
        stored = [json.loads(line) for line in memory[0].read_text().splitlines()]
        assert any(row["questions"] for row in stored)
        remembered = {row["name"]: row for row in stored[-1]["questions"]}
        assert remembered["candidateIsPreviousWorker"]["kind"] == "radio"
        assert remembered["candidateIsPreviousWorker"]["options"] == ["Yes", "No"]
        assert remembered["sponsorship"]["options"] == ["Yes", "No"]
        assert "Job board" not in memory[0].read_text()
        assert "(555) 010-0100" not in memory[0].read_text()
        context.close()
        browser.close()


def test_workday_session_preserves_manual_edits_on_the_same_step(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        page = context.new_page()
        page.goto(APPLY_STEPS.resolve().as_uri())
        page.evaluate("() => window.__showStep(1)")
        session = WorkdaySession(context, page, request)
        first = session.tick()
        assert first is not None and first["filled"] > 0
        count = len(_clicks(page))
        page.locator("#address--city").fill("Evanston")
        page.evaluate("() => document.getElementById('country--country').textContent = 'Canada'")
        assert session.tick() is None
        assert page.locator("#address--city").input_value() == "Evanston"
        assert page.locator("#country--country").inner_text() == "Canada"
        assert len(_clicks(page)) == count
        assert len(session.steps) == 1
        context.close()
        browser.close()


def test_workday_review_does_not_fill_retained_fields(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        page.goto(APPLY_STEPS.resolve().as_uri())
        page.evaluate("""() => {
          window.__showStep(6);
          document.querySelector('[data-step="1"]').hidden = false;
        }""")
        report = fill_current_step(page, request)
        assert report["filled"] == 0
        assert page.locator("#address--city").input_value() == "Madison"
        assert _clicks(page) == []
        browser.close()


def test_workday_combobox_pick_uses_the_fields_own_listbox(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["posting"]["apply_url"] = APPLY_STEPS.resolve().as_uri()
    request["posting"]["url"] = request["posting"]["apply_url"]
    posted = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        page.set_default_timeout(2_000)
        page.on("request", lambda item: posted.append(item.method) if item.method not in {"GET", "HEAD"} else None)
        page.goto(request["posting"]["apply_url"])
        page.evaluate("() => window.__showStep(1)")
        report = fill_current_step(page, request)
        assert page.locator("#country--country").inner_text() == "United States of America"
        assert page.locator("#address--countryRegion").inner_text() == "Illinois"
        assert page.locator("#phoneNumber--phoneType").inner_text() == "Mobile"
        assert page.locator("#country--value").input_value() == "united-states-of-america"
        filled = {row["label"] for row in report["fields"]}
        assert {"Country", "State", "Phone Device Type"} <= filled
        clicks = _clicks(page)
        assert clicks and all(row.get("in_field") for row in clicks)
        assert posted == []
        browser.close()


def test_workday_combobox_without_an_exact_option_is_left_alone(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["posting"]["apply_url"] = APPLY_STEPS.resolve().as_uri()
    request["posting"]["url"] = request["posting"]["apply_url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        page.set_default_timeout(2_000)
        page.goto(request["posting"]["apply_url"])
        page.evaluate("() => window.__showStep(2)")
        report = fill_current_step(page, request)
        # The real tenant writes "Native or bi-lingual proficiency"; the Profile
        # says "Native or bilingual", so the field is reported, never guessed at.
        assert page.locator("#language-31--overall").inner_text() == "Select One"
        assert page.locator("#language-32--overall").inner_text() == "Professional working proficiency"
        left = {row["label"]: row for row in report["left"]}
        assert "Spanish: Overall proficiency" not in left
        assert left["English: Overall proficiency"]["reason"] == "no option matched exactly"
        assert left["English: Overall proficiency"]["expected"] == "Native or bilingual"
        assert any("no option matches exactly" in warning for warning in report["warnings"])
        browser.close()


def test_workday_combobox_guard_refuses_footer_delete_and_add_buttons(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["posting"]["apply_url"] = FORBIDDEN_BUTTONS.resolve().as_uri()
    request["posting"]["url"] = request["posting"]["apply_url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        page.set_default_timeout(2_000)
        page.goto(request["posting"]["apply_url"])
        report = fill_current_step(page, request)
        assert report["filled"] == 0
        assert page.evaluate("() => window.__fixtureClicks") == []
        reasons = {row["label"]: row["reason"] for row in report["left"]}
        assert set(reasons) == {"Country", "State", "Phone Device Type"}
        assert all("not a drop-down" in reason for reason in reasons.values())
        browser.close()


def test_workday_date_spinbuttons_fill_by_keyboard(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["posting"]["apply_url"] = APPLY_STEPS.resolve().as_uri()
    request["posting"]["url"] = request["posting"]["apply_url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        page.set_default_timeout(2_000)
        page.goto(request["posting"]["apply_url"])
        page.evaluate("() => window.__showStep(2)")
        report = fill_current_step(page, request)
        values = {
            "workExperience-10--startDate-dateSectionMonth-input": "9",
            "workExperience-10--startDate-dateSectionYear-input": "2026",
            "workExperience-11--startDate-dateSectionMonth-input": "2",
            "workExperience-11--startDate-dateSectionYear-input": "2026",
            "workExperience-11--endDate-dateSectionMonth-input": "5",
            "workExperience-11--endDate-dateSectionYear-input": "2026",
        }
        for control_id, expected in values.items():
            assert page.locator(f"#{control_id}").get_attribute("aria-valuenow") == expected
        filled = {row["label"] for row in report["fields"]}
        assert any("startDate Month" in label for label in filled)
        assert any("startDate Year" in label for label in filled)
        assert any("endDate Month" in label for label in filled)
        assert all(row.get("in_field") for row in _clicks(page))
        browser.close()


def test_workday_selection_that_triggers_a_save_stops_filling(tmp_path: Path) -> None:
    request = _request(tmp_path)
    page_url = "https://fixture.wd1.myworkdayjobs.com/en-US/FixtureSite/job/R123456/apply/autofillWithResume"
    request["posting"]["apply_url"] = page_url
    request["posting"]["url"] = page_url
    posted = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        page = context.new_page()
        page.set_default_timeout(2_000)
        page.route("**/*", lambda route: route.fulfill(body=APPLY_STEPS.read_text(), content_type="text/html"))
        page.on("request", lambda item: posted.append(item.method) if item.method not in {"GET", "HEAD"} else None)
        page.goto(page_url)
        page.evaluate("() => { window.__showStep(1); window.__fixtureSaveOnPick = true; }")
        session = WorkdaySession(context, page, request)
        session.tick()
        assert session.failed is True
        assert page.locator("#country--country").inner_text() == "United States of America"
        assert page.locator("#address--countryRegion").inner_text() == "Select One"
        assert page.locator("#phoneNumber--phoneType").inner_text() == "Select One"
        assert any("employer save request" in warning for warning in session.warnings)
        assert "PUT" in posted
        context.close()
        browser.close()


def test_workday_first_step_save_keeps_the_session_stopped(tmp_path: Path) -> None:
    request = _request(tmp_path)
    page_url = "https://fixture.wd1.myworkdayjobs.com/en-US/FixtureSite/job/R123456/apply/autofillWithResume"
    request["posting"]["apply_url"] = page_url
    request["posting"]["url"] = page_url
    fixture = APPLY_STEPS.read_text().replace(
        "</script>", "window.__showStep(1); window.__fixtureSaveOnPick = true;</script>"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        page = context.new_page()
        page.route("**/*", lambda route: route.fulfill(body=fixture, content_type="text/html"))
        result, _filled, _warnings = _navigation_and_fill(request, page)
        session = WorkdaySession(context, page, request, first_report=result["_workday"])
        assert session.failed is True
        assert session.tick() is None
        assert page.locator("#address--countryRegion").inner_text() == "Select One"
        assert any("employer save request" in warning for warning in session.warnings)
        context.close()
        browser.close()


def test_workday_unknown_dates_do_not_clear_current_employment(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["prepared_resume"] = {**PREPARED, "experience": [
        {**PREPARED["experience"][0], "dates": "2026"}, PREPARED["experience"][1]
    ]}
    request["profile_contact"] = {**CONTACT, "languages": [
        {"language": "English", "proficiency": "Not fluent"}
    ]}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        page.goto(APPLY_STEPS.resolve().as_uri())
        page.evaluate("""() => {
          window.__showStep(2);
          document.getElementById('workExperience-10--currentlyWorkHere').checked = true;
        }""")
        fill_current_step(page, request)
        assert page.locator("#workExperience-10--currentlyWorkHere").is_checked()
        assert not page.locator("#language-31--native").is_checked()
        browser.close()


def test_workday_eeo_field_takes_only_an_explicit_eeo_answer(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["posting"]["apply_url"] = APPLY_STEPS.resolve().as_uri()
    request["posting"]["url"] = request["posting"]["apply_url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        page.set_default_timeout(2_000)
        page.goto(request["posting"]["apply_url"])
        page.evaluate("() => window.__showStep(3)")
        fill_current_step(page, request)
        assert page.locator("#gender--gender").input_value() == ""
        page.evaluate("() => window.__showStep(5)")
        fill_current_step(page, request)
        assert page.locator("#veteran--veteranStatus").inner_text() == "Select One"
        # An explicit EEO row is the only thing that may fill it. A universal
        # row explicitly supplied by the person applies on any tenant.
        answers = tmp_path / "answers"
        append(answers, "fixture", text="Gender", kind="text", answer="Woman", board=POSTING["board"],
               company=POSTING["company"], universal=True, eeo=True)
        append(answers, "fixture", text="Protected veteran status", kind="select",
               answer="I am not a protected veteran", board=POSTING["board"], company=POSTING["company"],
               universal=True, eeo=True)
        request["answers"] = _library_rows(answers)
        request["posting"] = {**POSTING, "board": "other.wd5~OtherSite", "company": "Other Corp"}
        page.evaluate("() => window.__showStep(3)")
        fill_current_step(page, request)
        assert page.locator("#gender--gender").input_value() == "Woman"
        page.evaluate("() => window.__showStep(5)")
        fill_current_step(page, request)
        assert page.locator("#veteran--veteranStatus").inner_text() == "I am not a protected veteran"
        # With the Profile statement present, the sponsorship question answers Yes;
        # without it, nothing is written at all — never a No.
        page.reload()
        page.evaluate("() => window.__showStep(3)")
        request["answers"] = []
        request["profile_constraints"] = {}
        fill_current_step(page, request)
        assert not page.locator("#sponsor-yes").is_checked()
        assert not page.locator("#sponsor-no").is_checked()
        request["profile_constraints"] = CONSTRAINTS
        fill_current_step(page, request)
        assert page.locator("#sponsor-yes").is_checked()
        browser.close()


def test_workday_repeat_employer_tells_the_person_to_remove_the_old_resume(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["posting"]["apply_url"] = REPEAT_EMPLOYER.resolve().as_uri()
    request["posting"]["url"] = request["posting"]["apply_url"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        page = context.new_page()
        page.set_default_timeout(2_000)
        page.goto(request["posting"]["apply_url"])
        session = WorkdaySession(context, page, request)
        session.tick()
        assert any("remove it yourself" in warning for warning in session.warnings)
        assert page.evaluate("() => window.__fixtureClicks") == []
        assert page.locator("input[type=file]").count() == 0
        # The person removes the old file; the input returns and the next pass uploads
        # this Posting's tailored version.
        page.click("#delete-old")
        assert page.evaluate("() => window.__fixtureClicks") == ["delete-old"]
        session.tick()
        assert page.locator("#repeat-file").evaluate("el => el.files[0].name") == "resume.pdf"
        assert session.resume_uploaded is True
        context.close()
        browser.close()


def test_workday_sign_in_page_gets_nothing_typed_or_captured(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["posting"]["apply_url"] = SIGN_IN.resolve().as_uri()
    request["posting"]["url"] = request["posting"]["apply_url"]
    state_path = tmp_path / STATE_NAME
    state = {"state": "ready", "token": "fixture", "pid": os.getpid()}
    state_path.write_text(json.dumps(state))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        page = context.new_page()
        page.goto(request["posting"]["apply_url"])
        assert sign_in_page(page.main_frame)
        report = fill_current_step(page, request)
        assert report["filled"] == 0
        assert any("sign-in" in warning for warning in report["warnings"])
        _install_confirmation_watch(context, request, state_path, state)
        page.locator("#login-email").fill("avery@example.test")
        page.locator("#login-password").fill("not a real secret")
        page.locator("#login-email").focus()
        page.wait_for_timeout(250)
        assert not json.loads(state_path.read_text()).get("candidates")
        context.close()
        browser.close()


def test_workday_captures_a_typed_value_before_the_step_is_replaced(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["posting"]["apply_url"] = APPLY_STEPS.resolve().as_uri()
    request["posting"]["url"] = request["posting"]["apply_url"]
    state_path = tmp_path / STATE_NAME
    state = {"state": "ready", "token": "fixture", "pid": os.getpid()}
    state_path.write_text(json.dumps(state))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        page = context.new_page()
        page.goto(request["posting"]["apply_url"])
        _install_confirmation_watch(context, request, state_path, state)
        # The person moves to My Information and answers a question Venator left
        # blank; the next step replaces the DOM.
        page.click("#next")
        page.wait_for_function("document.getElementById('active-step-label').textContent === 'My Information'")
        session = WorkdaySession(context, page, request)
        session.tick()
        page.locator("#source--source").fill("A colleague")
        assert page.locator("#source--source").input_value() == "A colleague"
        page.click("#next")
        page.wait_for_function("document.getElementById('active-step-label').textContent === 'My Experience'")
        page.wait_for_timeout(200)
        candidates = json.loads(state_path.read_text()).get("candidates", [])
        assert [(row["name"], row["value"]) for row in candidates] == [("source", "A colleague")]
        assert page.evaluate("() => window.__fixtureSubmitEvents") == 0
        context.close()
        browser.close()


def test_workday_applied_receipt_only_from_a_matching_candidate_home_row(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request["posting"]["apply_url"] = APPLY_STEPS.resolve().as_uri()
    request["posting"]["url"] = request["posting"]["apply_url"]
    state_path = tmp_path / STATE_NAME
    state = {"state": "ready", "token": "fixture", "pid": os.getpid()}
    state_path.write_text(json.dumps(state))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        page = context.new_page()
        page.goto(request["posting"]["apply_url"])
        inspect = _install_confirmation_watch(context, request, state_path, state)
        # A Candidate Home row is not a receipt before the person acts: the panel is
        # shown here without a person's click, and inspect leaves the state ready.
        page.evaluate("() => window.__showStep(6)")
        page.evaluate("() => document.getElementById('candidate-home').hidden = false")
        page.wait_for_timeout(50)
        inspect(page.main_frame)
        assert json.loads(state_path.read_text())["state"] == "ready"
        # A field press is not Submit and must not arm Applied detection.
        page.evaluate("() => window.__showStep(1)")
        page.click("#country--country")
        inspect(page.main_frame)
        assert json.loads(state_path.read_text())["state"] == "ready"
        # After the person's own Submit, the row bound to the title and the requisition
        # is the receipt; the generic completion page alone never is.
        page.evaluate("() => window.__showStep(6)")
        page.click("#next")
        page.wait_for_timeout(50)
        assert candidate_home_marker(page.main_frame, POSTING, allow_test_urls=True) is not None
        requisition_cell = page.locator('[data-automation-id="taskListRow"]').first.locator("td").first
        requisition_cell.evaluate("el => el.textContent = 'R1234567'")
        assert candidate_home_marker(page.main_frame, POSTING, allow_test_urls=True) is None
        requisition_cell.evaluate("el => el.textContent = 'R123456'")
        page.locator("#candidate-home").evaluate("el => el.hidden = true")
        assert candidate_home_marker(page.main_frame, POSTING, allow_test_urls=True) is None
        page.locator("#candidate-home").evaluate("el => el.hidden = false")
        status_node = page.locator('[data-automation-id="applicationStatus"]').first
        status_node.evaluate("el => el.textContent = 'Not Submitted'")
        assert candidate_home_marker(page.main_frame, POSTING, allow_test_urls=True) is None
        status_node.evaluate("el => el.textContent = 'Under Review'")
        marker = candidate_home_marker(page.main_frame, POSTING, allow_test_urls=True)
        assert marker["system"] == "workday"
        assert marker["board"] == POSTING["board"]
        assert marker["job_id"] == "R123456"
        assert marker["requisition"] == "R123456"
        assert marker["title"] == "Research Associate"
        assert candidate_home_marker(page.main_frame, {**POSTING, "key": "workday:fixture.wd1~FixtureSite:R999"}) is None
        assert candidate_home_marker(page.main_frame, {**POSTING, "title": "Another Opening"}) is None
        inspect(page.main_frame)
        receipt = json.loads(state_path.read_text())
        assert receipt["state"] == "applied"
        assert set(receipt["marker"]) == {"system", "board", "job_id", "host", "requisition", "title", "at"}
        assert "url" not in receipt and "token" not in receipt
        context.close()
        browser.close()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/R123456/apply", True),
        ("https://evilacme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/R123456/apply", False),
        ("https://other.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/R123456/apply", False),
        ("https://acme.wd1.myworkdayjobs.com.evil.test/en-US/AcmeCareers/job/R123456/apply", False),
        ("https://jobs.ashbyhq.com/acme/123/application", False),
    ],
    ids=["same-tenant", "lookalike-tenant", "other-tenant", "suffix-lookalike", "another-system"],
)
def test_workday_host_is_bound_to_the_postings_tenant(url: str, expected: bool) -> None:
    posting = {"key": "workday:acme.wd1~AcmeCareers:R123456"}
    from venator.browser.workday import workday_host_matches

    assert workday_host_matches(url, posting) is expected


def test_workday_document_identity_names_the_tenant_site_and_requisition() -> None:
    posting = {"key": "workday:acme.wd1~AcmeCareers:R123456"}
    assert workday_identity(
        "https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/Cambridge-MA/R123456/apply", posting
    )
    assert not workday_identity(
        "https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/Other_R0000001/apply", posting
    )
    assert not workday_identity(
        "https://acme.wd1.myworkdayjobs.com/en-US/OtherSite/job/R123456/apply", posting
    )
    assert not workday_identity(
        "https://other.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/R123456/apply", posting
    )
    assert not workday_identity(
        "https://acme.wd1.other.myworkdayjobs.com/en-US/AcmeCareers/job/R123456/apply", posting
    )
    assert not workday_identity(
        "https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareersOther/job/R123456/apply", posting
    )
    assert not workday_identity(
        "https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/Role_R1234567/apply", posting
    )
    assert workday_identity(
        "https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/Role_R123456-1/apply", posting
    )
