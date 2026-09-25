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
                "key": "ashby:fixture:123",
                "source": "ashby",
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
        assert any("limited to Greenhouse" in warning for warning in result["warnings"])
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


def test_production_form_can_send_manual_post_with_current_documents(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"synthetic prepared resume")
    letter = tmp_path / "letter.txt"
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
        assert 'filename="letter.txt"' in posts[0]
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
