from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from venator.browser.handoff import (
    HandoffBusy,
    STATE_NAME,
    _navigation_and_fill,
    _install_confirmation_watch,
    _confirmation_host,
    _trusted_document,
    start_handoff,
)
from venator.profile import Profile


FIXTURE = Path(__file__).parent / "fixtures" / "application-form.html"


def read_handoff_state(directory: Path) -> dict[str, object] | None:
    try:
        value = json.loads((directory / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


@pytest.fixture(autouse=True)
def handoff_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VENATOR_HANDOFF_HEADLESS", "1")


def _profile(tmp_path: Path) -> Profile:
    return Profile(
        name="fixture",
        directory=tmp_path / "profile",
        resume={
            "name": "Test Person",
            "contact": {"email": "test@example.com"},
        },
        constraints={},
    )


def _stop_handoff(directory: Path) -> None:
    state = read_handoff_state(directory) or {}
    pid = state.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return
    # The worker starts a new session, so ending its process group also closes
    # the browser it owns if the test fails before it can connect over CDP.
    try:
        os.killpg(pid, signal.SIGTERM)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return
        time.sleep(0.05)


def test_handoff_keeps_a_visible_browser_open_and_fills_confirmed_fields(tmp_path: Path) -> None:
    application_dir = tmp_path / "application"
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"synthetic resume")

    try:
        result = start_handoff(
            {
                "key": "greenhouse:fixture:123",
                "source": "greenhouse",
                "board": "fixture",
                "title": "Fixture scientist",
                "url": FIXTURE.resolve().as_uri(),
            },
            _profile(tmp_path),
            resume_file,
            application_dir,
            # A file URI is accepted only through this explicit synthetic-test
            # seam; production posting records accept http(s) URLs only.
            allow_test_urls=True,
        )
        assert result["filled"] == 2
        assert any("Notes" in warning for warning in result["warnings"])

        state = read_handoff_state(application_dir)
        assert state is not None
        assert state["state"] == "ready"
        assert isinstance(state["pid"], int)
        os.kill(state["pid"], 0)

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{state['debugging_port']}"
            )
            page = browser.contexts[0].pages[0]
            assert not page.is_closed()
            assert page.locator("#full_name").input_value() == "Test Person"
            assert page.locator("#resume").evaluate("element => element.files[0].name") == "resume.pdf"
            assert page.locator("#notes").input_value() == ""
            assert page.locator("label[for=notes]").is_visible()
            assert page.evaluate("() => window.fixtureSubmitEvents || 0") == 0
            page.locator("#discipline").select_option("lab")
            page.get_by_label("Email", exact=True).check()
            page.locator("#submit-control").click()
            assert page.evaluate("() => window.fixtureSubmitEvents") == 1
            assert page.evaluate("() => window.__venatorHandoffSubmitEvents") == 1
            # Closing one tab must leave another user's tab open.
            remaining = browser.contexts[0].new_page()
            page.close()
            remaining.wait_for_timeout(700)
            assert not remaining.is_closed()
            for open_page in list(browser.contexts[0].pages):
                open_page.close()
            browser.close()
    finally:
        _stop_handoff(application_dir)


def test_second_handoff_reports_a_locked_dedicated_profile(tmp_path: Path) -> None:
    application_dir = tmp_path / "application"
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"synthetic resume")
    posting = {
        "key": "greenhouse:fixture:123",
        "source": "greenhouse",
        "board": "fixture",
        "title": "Fixture scientist",
        "url": FIXTURE.resolve().as_uri(),
    }
    try:
        start_handoff(posting, _profile(tmp_path), resume_file, application_dir, allow_test_urls=True)
        with pytest.raises(HandoffBusy, match="already open"):
            start_handoff(posting, _profile(tmp_path), resume_file, tmp_path / "different-application", allow_test_urls=True)
        state = read_handoff_state(application_dir)
        assert state is not None
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{state['debugging_port']}")
            browser.contexts[0].add_cookies([{
                "name": "fixture_login", "value": "retained", "domain": "example.invalid", "path": "/",
                "expires": time.time() + 3600,
            }])
            for open_page in list(browser.contexts[0].pages):
                open_page.close()
            browser.close()
        for _ in range(60):
            if (read_handoff_state(application_dir) or {}).get("state") == "closed":
                break
            time.sleep(0.05)
        assert (read_handoff_state(application_dir) or {}).get("state") == "closed"
        next_directory = tmp_path / "different-application"
        start_handoff(posting, _profile(tmp_path), resume_file, next_directory, allow_test_urls=True)
        state = read_handoff_state(next_directory)
        assert state is not None
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{state['debugging_port']}")
            cookies = browser.contexts[0].cookies("https://example.invalid/")
            assert any(cookie["name"] == "fixture_login" and cookie["value"] == "retained" for cookie in cookies)
            for open_page in list(browser.contexts[0].pages):
                open_page.close()
            browser.close()
        assert not (application_dir / "browser-profile").exists()
        assert (tmp_path / "profile" / ".venator-browser").stat().st_mode & 0o777 == 0o700
    finally:
        _stop_handoff(application_dir)
        _stop_handoff(tmp_path / "different-application")


def test_unsupported_source_opens_manual_fallback_without_uploading(tmp_path: Path) -> None:
    application_dir = tmp_path / "application"
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"synthetic resume")
    try:
        result = start_handoff(
            {
                "key": "lever:fixture:123",
                "source": "lever",
                "board": "fixture",
                "title": "Fixture scientist",
                "url": FIXTURE.resolve().as_uri(),
            },
            _profile(tmp_path),
            resume_file,
            application_dir,
            allow_test_urls=True,
        )
        assert result["filled"] == 0
        assert any("Complete this form manually" in warning for warning in result["warnings"])
        state = read_handoff_state(application_dir)
        assert state is not None
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{state['debugging_port']}"
            )
            page = browser.contexts[0].pages[0]
            assert page.locator("#full_name").input_value() == ""
            assert page.locator("#resume").evaluate("element => element.files.length") == 0
            browser.close()
    finally:
        _stop_handoff(application_dir)


def test_ashby_saved_form_fills_reviewed_answers_and_files_without_touching_captcha(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from venator.answers.store import append
    from venator.apply.form import ashby_questions, remember_ashby_form, review_questions
    from venator.browser.recon import _INVENTORY_SCRIPT

    posting = {"key": "ashby:lilasciences:123", "source": "ashby", "board": "lilasciences",
               "company": "Lila Sciences", "url": "https://jobs.ashbyhq.com/lilasciences/123/application"}
    memory = tmp_path / "memory"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"fixture resume")
    letter = tmp_path / "letter.pdf"
    letter.write_bytes(b"fixture letter")
    html = (FIXTURE.parent / "phase3-ashby-form.html").read_text()
    append(tmp_path / "answers", "fixture", text="Have you worked for Lila?", kind="select",
           answer="No", board="lilasciences", company="Lila Sciences")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        posts = []
        def route(request):
            if request.request.method != "GET":
                posts.append(request.request.method)
            request.fulfill(body=html, content_type="text/html")
        page.route("**/*", route)
        page.goto(posting["url"])
        fields = page.locator("form").evaluate(_INVENTORY_SCRIPT)
        remember_ashby_form(memory, posting, fields, "fixture")
        assert [row["name"] for row in ashby_questions(memory, posting, "fixture")] == [
            "full_name", "experience", "notes", "resume", "letter"]
        assert ashby_questions(memory, {**posting, "key": "ashby:another:123"}, "fixture") == []
        with pytest.raises(ValueError, match="another Profile"):
            remember_ashby_form(memory, posting, fields, "different")
        assert (memory / ".profile").read_text().strip() == "fixture"
        profile = SimpleNamespace(identifier="fixture", resume={"name": "Test Person"}, constraints={})
        reviewed, pins = review_questions(posting, profile, tmp_path / "answers",
                                          ashby_questions(memory, posting, "fixture"))
        assert pins and next(row for row in reviewed if row["name"] == "experience")["answer"] == "No"
        result, filled, _ = _navigation_and_fill({"posting": posting, "resume_file": str(resume),
            "letter_file": str(letter), "profile_resume": profile.resume, "profile_constraints": {},
            "questions": reviewed, "form_memory": str(memory), "profile_id": "fixture"}, page)
        assert filled == 4
        assert page.locator("#full_name").input_value() == "Test Person"
        assert page.locator("#experience").input_value() == "No"
        assert page.locator("#notes").input_value() == ""
        assert page.locator("#resume").evaluate("el => el.files[0].name") == "resume.pdf"
        assert page.locator("#letter").evaluate("el => el.files[0].name") == "letter.pdf"
        assert page.locator(".g-recaptcha iframe").count() == 1
        assert not posts
        assert result["submit_events"] == 0
        browser.close()


@pytest.mark.parametrize("destination", ["https://other.test/lilasciences/123/application",
    "https://boards.greenhouse.io/lilasciences/jobs/123",
    "https://jobs.ashbyhq.com/another/123/application",
    "https://jobs.ashbyhq.com/lilasciences/456/application",
    "http://jobs.ashbyhq.com/lilasciences/123/application"])
def test_ashby_other_hosts_and_postings_get_no_fields_or_files(destination: str, tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"fixture resume")
    html = (FIXTURE.parent / "phase3-ashby-form.html").read_text()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        page.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html"))
        result, filled, _ = _navigation_and_fill({"posting": {"key": "ashby:lilasciences:123",
            "source": "ashby", "url": destination}, "resume_file": str(resume),
            "profile_resume": {"name": "Test Person"}, "profile_constraints": {}}, page)
        assert filled == 0
        assert page.locator("#full_name").input_value() == ""
        assert page.locator("#resume").evaluate("el => el.files.length") == 0
        assert result["submit_events"] == 0
        browser.close()


def test_production_form_can_send_manual_post_with_current_documents(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"synthetic prepared resume")
    letter = tmp_path / "letter.pdf"
    letter.write_text("synthetic current letter")
    url = "https://boards.greenhouse.io/fixture/jobs/123"
    html = FIXTURE.read_text().replace(
        '<form id="application-form">',
        '<form id="application-form" method="post" action="/submitted" enctype="multipart/form-data">'
        '<label for="letter">Cover Letter</label><input id="letter" name="letter" type="file">',
    ).replace("event.preventDefault();", "")
    posts = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        page.set_default_timeout(2_000)
        def fixture_route(route):
            if route.request.method == "POST":
                posts.append(route.request.post_data)
                route.fulfill(body="Submitted locally")
            else:
                route.fulfill(body=html, content_type="text/html")
        # All traffic is fulfilled locally, including the synthetic ATS hostname.
        page.route("**/*", fixture_route)
        result, filled, _warnings = _navigation_and_fill({
            "posting": {"key": "greenhouse:fixture:123", "source": "greenhouse", "url": url},
            "resume_file": str(resume), "letter_file": str(letter),
            "profile_resume": {"name": "Test Person"}, "profile_constraints": {},
        }, page)
        assert filled == 3
        assert not posts
        assert page.locator("#notes").input_value() == ""
        assert not any("Cover Letter left blank" in warning for warning in result["warnings"])
        assert page.locator("#resume").evaluate("el => el.files[0].text()") == "synthetic prepared resume"
        assert page.locator("#letter").evaluate("el => el.files[0].text()") == "synthetic current letter"
        page.locator("#discipline").select_option("lab")
        page.get_by_label("Email", exact=True).check()
        page.locator("#submit-control").click()
        page.wait_for_url("**/submitted")
        assert len(posts) == 1
        # Chromium's request inspection omits binary multipart file bodies;
        # their browser File contents were checked before this actual POST.
        assert 'filename="resume.pdf"' in posts[0]
        assert 'filename="letter.pdf"' in posts[0]
        browser.close()


@pytest.mark.parametrize("mode", ["redirect", "apply-link", "wrong-job", "untrusted-parent"])
def test_assistance_cannot_escape_the_confirmed_document(tmp_path: Path, mode: str) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"synthetic prepared resume")
    url = "https://boards.greenhouse.io/fixture/jobs/123"
    evil = "https://example.invalid/application"
    html = FIXTURE.read_text()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        page.set_default_timeout(2_000)
        def fixture_route(route):
            requested = route.request.url
            if requested == url and mode == "redirect":
                route.fulfill(body=f"<script>location.replace('{evil}')</script>", content_type="text/html")
            elif requested == url and mode == "wrong-job":
                route.fulfill(body=f"<script>location.replace('{url.replace("123", "456")}')</script>", content_type="text/html")
            elif requested == url and mode == "apply-link":
                route.fulfill(body=f'<a href="{evil}">Apply</a>', content_type="text/html")
            elif requested == evil and mode == "untrusted-parent":
                route.fulfill(body=html + f'<iframe src="{url}"></iframe>', content_type="text/html")
            else:
                route.fulfill(body=html, content_type="text/html")
        page.route("**/*", fixture_route)
        _result, filled, _warnings = _navigation_and_fill({
            "posting": {"key": "greenhouse:fixture:123", "source": "greenhouse",
                        "url": evil if mode == "untrusted-parent" else url},
            "resume_file": str(resume), "profile_resume": {"name": "Test Person"},
        }, page)
        if mode == "untrusted-parent":
            assert filled == 2
            assert page.locator("#full_name").input_value() == ""
            assert page.locator("#resume").evaluate("el => el.files.length") == 0
            assert page.frames[1].locator("#full_name").input_value() == "Test Person"
        else:
            assert filled == 0
            if mode == "apply-link":
                assert page.url == url
            else:
                assert page.locator("#resume").evaluate("el => el.files.length") == 0
        browser.close()


@pytest.mark.parametrize("url", [
    "http://boards.greenhouse.io/fixture/jobs/123",
    "https://greenhouse.io.evil.test/fixture/jobs/123",
    "https://unrelated.greenhouse.io/fixture/jobs/123",
    "https://boards.greenhouse.io/another/jobs/123",
    "https://boards.greenhouse.io/fixture/jobs/456",
], ids=["http", "lookalike", "unsupported-host", "other-board", "other-job"])
def test_document_identity_rejects_lookalikes_and_other_jobs(url: str) -> None:
    assert not _trusted_document(url, {"key": "greenhouse:fixture:123"})


@pytest.mark.parametrize("embedded", [False, True], ids=["hosted", "embedded"])
@pytest.mark.parametrize("drift", ["none", "duplicate", "missing", "options"])
def test_api_questions_join_live_greenhouse_controls_without_guessing(embedded: bool, drift: str, tmp_path: Path) -> None:
    from venator.answers.store import append
    from venator.apply.form import greenhouse_questions, review_questions
    from types import SimpleNamespace
    api = FIXTURE.parent / "phase2-greenhouse-questions.json"
    html = (FIXTURE.parent / "phase2-greenhouse-form.html").read_text()
    if drift == "duplicate":
        html = html.replace('<button type="submit">', '<label for="another">Have you ever worked for Lila?</label><input id="another" name="another"><button type="submit">')
    if drift == "missing":
        html = html.replace('name="question_17"', 'name="changed_question"')
    if drift == "options":
        html = html.replace('<option>No</option>', '<option>Not sure</option>', 1)
    posting = {"key": "greenhouse:lilasciences:123", "source": "greenhouse", "board": "lilasciences",
               "company": "Lila Sciences", "url": "https://boards.greenhouse.io/lilasciences/jobs/123"}
    profile = SimpleNamespace(identifier="fixture", resume={}, constraints={"work_authorization": {"requires_sponsorship": True}})
    append(tmp_path / "answers", "fixture", text="Have you ever worked for Lila?", kind="select", answer="No",
           board="lilasciences", company="Lila Sciences")
    questions = greenhouse_questions(posting, fetch=lambda url: api.read_bytes())
    reviewed, _pins = review_questions(posting, profile, tmp_path / "answers", questions)
    assert [row["answer"] for row in reviewed] == ["No", "Yes", None]
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"synthetic")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(offline=True)
        destination = "https://job-boards.greenhouse.io/embed/job_app?for=lilasciences&token=123" if embedded else posting["url"]
        if embedded:
            posting["url"] = "https://lila.example.test/apply"
        page.route("**/*", lambda route: route.fulfill(body=html if route.request.url == destination
                    else f'<html><body><iframe src="{destination}"></iframe></body></html>', content_type="text/html"))
        result, filled, _warnings = _navigation_and_fill({"posting": posting, "resume_file": str(resume),
            "profile_resume": {}, "profile_constraints": dict(profile.constraints), "questions": reviewed}, page)
        frame = page.frames[1] if embedded else page.main_frame
        assert frame.locator("#question_19").is_checked() is False
        if drift == "none":
            assert filled == 2
            assert frame.locator("#question_17").input_value() == "No"
            assert frame.locator("#question_18").input_value() == "Yes"
        else:
            assert filled == 1
            assert frame.locator("#question_17").input_value() == ""
            assert any("Have you ever worked for Lila? left blank" in warning for warning in result["warnings"])
        browser.close()


def test_confirmation_watch_waits_for_owner_action_and_never_clicks(tmp_path: Path) -> None:
    posting = {"key": "greenhouse:fixture:123", "source": "greenhouse", "board": "fixture"}
    url = "https://boards.greenhouse.io/fixture/jobs/123"
    confirmation = url + "/confirmation"
    state_path = tmp_path / "session.json"
    state = {"state": "ready", "token": "fixture", "pid": os.getpid()}
    state_path.write_text(json.dumps(state))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        context.route("**/*", lambda route: route.fulfill(
            body="<body>Thank you. Application received.</body>" if route.request.url == confirmation
            else '<body><button id="go" onclick="location.href=\'/fixture/jobs/123/confirmation\'">Continue</button></body>',
            content_type="text/html"))
        page = context.new_page()
        page.goto(url)
        _install_confirmation_watch(context, {"posting": posting}, state_path, state)
        assert (json.loads(state_path.read_text()))["state"] == "ready"
        page.goto(confirmation)  # A URL match alone before a person acts is not a receipt.
        assert json.loads(state_path.read_text())["state"] == "ready"
        page.goto(url)
        page.locator("#go").click()  # Only the test's human surrogate clicks.
        page.wait_for_url(confirmation)
        page.wait_for_timeout(300)
        receipt = json.loads(state_path.read_text())
        assert receipt["state"] == "applied"
        assert receipt["marker"]["board"] == "fixture"
        assert receipt["marker"]["job_id"] == "123"
        assert set(receipt["marker"]) == {"system", "board", "job_id", "host", "at"}
        assert "url" not in receipt and "token" not in receipt and "token" not in receipt["marker"]
        context.close()
        browser.close()


@pytest.mark.parametrize("mode", ["embed", "new-tab", "error", "other-job"])
def test_confirmation_watch_frames_tabs_and_error_pages(mode: str, tmp_path: Path) -> None:
    posting = {"key": "greenhouse:fixture:123", "source": "greenhouse"}
    hosted = "https://boards.greenhouse.io/fixture/jobs/123"
    embed = "https://job-boards.greenhouse.io/embed/job_app?for=fixture&token=123"
    confirmation = ("https://job-boards.greenhouse.io/embed/job_app/confirmation?for=fixture&token=123"
                    if mode == "embed" else hosted + "/confirmation")
    if mode == "other-job":
        confirmation = "https://boards.greenhouse.io/fixture/jobs/456/confirmation"
    entry = "https://employer.example.test/apply" if mode == "embed" else hosted
    state_path = tmp_path / "state.json"
    state = {"state": "ready", "token": "fixture", "pid": os.getpid()}
    state_path.write_text(json.dumps(state))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        def route(request):
            url = request.request.url
            if url == entry and mode == "embed":
                html = f'<iframe src="{embed}"></iframe>'
            elif url == confirmation:
                html = "<body>Error: validation failed</body>" if mode == "error" else "<body>Thank you, application received.</body>"
            elif mode == "new-tab":
                html = f'<a href="{confirmation}" target="_blank">Open</a>'
            else:
                html = f'<button onclick="location.href=\'{confirmation}\'">Continue</button>'
            request.fulfill(body=html, content_type="text/html")
        context.route("**/*", route)
        page = context.new_page()
        page.goto(entry)
        _install_confirmation_watch(context, {"posting": posting}, state_path, state)
        control = page.frames[1] if mode == "embed" else page.main_frame
        if mode == "new-tab":
            with page.expect_popup() as popup:
                control.locator("a").click()
            popup.value.wait_for_load_state()
        else:
            control.locator("button").click()
            page.wait_for_timeout(300)
        for _ in range(20):
            if json.loads(state_path.read_text())["state"] == "applied":
                break
            page.wait_for_timeout(50)
        assert json.loads(state_path.read_text())["state"] == ("ready" if mode in {"error", "other-job"} else "applied")
        context.close()
        browser.close()


def test_capture_only_blank_non_reserved_fields_without_submit(tmp_path: Path) -> None:
    url = "https://boards.greenhouse.io/fixture/jobs/123"
    fields = ['<label for="notes">Lab notes</label><input id="notes" name="notes">']
    for name, label, kind in [("sponsor", "Visa sponsorship", "text"),
                              ("consent", "Privacy consent", "checkbox"), ("gender", "Gender", "text"),
                              ("code", "Verification code", "text"), ("file", "Resume", "file")]:
        fields.append(f'<label for="{name}">{label}</label><input id="{name}" name="{name}" type="{kind}">')
    state_path = tmp_path / "state.json"
    state = {"state": "ready", "token": "fixture", "pid": os.getpid()}
    state_path.write_text(json.dumps(state))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        context.route("**/*", lambda route: route.fulfill(body="<form>" + "".join(fields) + "</form>", content_type="text/html"))
        page = context.new_page()
        page.goto(url)
        _install_confirmation_watch(context, {"posting": {"key": "greenhouse:fixture:123", "source": "greenhouse"}}, state_path, state)
        for name in ("notes", "sponsor", "gender", "code"):
            page.locator(f"#{name}").fill("fixture value")
        page.locator("#consent").check()
        page.locator("#notes").focus()
        page.wait_for_timeout(300)
        candidates = json.loads(state_path.read_text()).get("candidates", [])
        assert [row["name"] for row in candidates] == ["notes"]
        assert json.loads(state_path.read_text())["state"] == "ready"
        context.close()
        browser.close()


def test_sign_in_password_and_file_changes_never_become_answers(tmp_path: Path) -> None:
    url = "https://boards.greenhouse.io/fixture/jobs/123"
    html = ('<body>Sign in<form><label for="login">Email</label><input id="login" name="login">'
            '<label for="password">Password</label><input id="password" name="password" type="password">'
            '<label for="resume">Resume</label><input id="resume" name="resume" type="file"></form></body>')
    state_path = tmp_path / "session.json"
    state = {"state": "ready", "token": "fixture", "pid": os.getpid()}
    state_path.write_text(json.dumps(state))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        context.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html"))
        page = context.new_page()
        page.goto(url)
        _install_confirmation_watch(context, {"posting": {"key": "greenhouse:fixture:123", "source": "greenhouse"}}, state_path, state)
        page.locator("#login").fill("email@example.test")
        page.locator("#password").fill("private fixture value")
        page.locator("#resume").set_input_files({"name": "fixture.pdf", "mimeType": "application/pdf", "buffer": b"test"})
        page.locator("#login").focus()
        page.wait_for_timeout(250)
        assert not json.loads(state_path.read_text()).get("candidates")
        context.close()
        browser.close()


def test_confirmation_rejects_other_job_error_and_untrusted_host() -> None:
    posting = {"key": "greenhouse:fixture:123"}
    assert _confirmation_host("https://boards.greenhouse.io/fixture/jobs/456/confirmation", posting) is None
    assert _confirmation_host("https://evil.test/fixture/jobs/123/confirmation", posting) is None
    assert _confirmation_host("https://job-boards.greenhouse.io/embed/job_app/confirmation?for=fixture&token=123", posting)
    assert _confirmation_host("https://job-boards.greenhouse.io/embed/job_app/confirmation?for=fixture&token=456", posting) is None


def test_failed_worker_preserves_ready_error_until_caller_reads_it(tmp_path: Path, monkeypatch) -> None:
    import json
    import venator.browser.handoff as handoff

    token = "fixture-token"
    request = tmp_path / "request.json"
    ready = tmp_path / "ready.json"
    lock = tmp_path / "lock.json"
    state = tmp_path / "state.json"
    request.write_text(json.dumps({"token": token}))
    def fail_launch(_request):
        raise RuntimeError("synthetic launch failure")
    monkeypatch.setattr(handoff, "_open_browser", fail_launch)
    assert handoff._worker_main(request, ready, lock, state) == 1
    assert json.loads(ready.read_text())["message"] == "synthetic launch failure"
    assert not request.exists()
    assert not lock.exists()
