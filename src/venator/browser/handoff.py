"""Open a prepared application in a persistent, user-visible browser.

The dry-run driver in :mod:`venator.browser.fill` is intentionally isolated
from this module.  A handoff uses a dedicated persistent Playwright profile so
the user can keep a login between sessions, leaves the browser open, and never
intercepts or submits a request.  The caller waits only for a small ready
handshake from a detached worker; the worker owns the browser for the rest of
its lifetime.

Greenhouse, Ashby and Workday are assisted sources. Other sources open for manual completion.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page, sync_playwright

from venator.browser.fill import SkipField, _fill_control
from venator.browser.recon import _INVENTORY_SCRIPT, _form_score, detect_captcha
from venator.fill.mapping import FactSources
from venator.fill.plan import create_fill_plan
from venator.profile import Profile


SUPPORTED_SOURCES = frozenset({"greenhouse", "ashby", "workday"})
STARTUP_TIMEOUT = 60.0
ASSIST_TIMEOUT = 15.0
NAVIGATION_TIMEOUT_MS = 8_000
PROFILE_DIRECTORY_NAME = "browser-profile"
LOCK_NAME = ".handoff.lock"
STATE_NAME = ".handoff-session.json"


def handoff_state_path(directory: Path) -> Path:
    """Return the private session-state path for one application directory."""

    return Path(directory) / STATE_NAME


def _lock_path(directory: Path) -> Path:
    return Path(directory) / LOCK_NAME


def _write_json(path: Path, value: object) -> None:
    """Write a private JSON file atomically with user-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _pid_alive(value: object) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class HandoffBusy(RuntimeError):
    """A handoff browser already owns the dedicated profile."""


def _acquire_lock(directory: Path, token: str) -> Path:
    path = _lock_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = _read_json(path)
            if existing is None or _pid_alive(existing.get("pid")):
                raise HandoffBusy(
                    "A Venator application browser for this candidate is already open or starting. "
                    "Finish or close its windows, then retry this application; your login is retained."
                )
            # A crashed worker may leave its marker behind.  It is safe to
            # reclaim only a marker whose owner is no longer alive.
            with suppress(FileNotFoundError):
                path.unlink()
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps({"token": token, "pid": os.getpid(), "state": "starting"}) + "\n")
            output.flush()
            os.fsync(output.fileno())
        return path
    raise HandoffBusy("a Venator application browser is already starting")


def _release_lock(path: Path, token: str) -> None:
    current = _read_json(path)
    if current is not None and current.get("token") == token:
        with suppress(FileNotFoundError):
            path.unlink()


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _apply_url(posting: Mapping[str, Any], *, allow_test_urls: bool = False) -> str:
    value = posting.get("apply_url") or posting.get("url")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("posting has no application URL")
    value = value.strip()
    allowed_schemes = {"http", "https"}
    if allow_test_urls:
        allowed_schemes.add("file")
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in allowed_schemes or (parsed.scheme != "file" and not parsed.hostname):
        raise ValueError("application URL must use http or https with a hostname")
    return value


def _request_payload(
    posting: Mapping[str, Any],
    profile: Profile,
    resume_file: Path,
    directory: Path,
    letter_file: Path | None,
    debugging_port: int | None,
    questions: list[dict] | None = None,
    form_memory: Path | None = None,
    profile_id: str | None = None,
    answers: list[dict] | None = None,
    prepared_resume: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contact = profile.contact
    return {
        "posting": {
            "key": posting.get("key"),
            "source": posting.get("source"),
            "board": posting.get("board"),
            "company": posting.get("company"),
            "title": posting.get("title"),
            "apply_url": posting.get("apply_url"),
            "url": posting.get("url"),
        },
        "profile_resume": dict(profile.resume),
        "profile_constraints": dict(profile.constraints),
        "profile_contact": {
            "legal_name": {
                "first": contact.legal_name.first,
                "middle": contact.legal_name.middle,
                "last": contact.legal_name.last,
            },
            "address": {
                "line1": contact.address.line1,
                "line2": contact.address.line2,
                "city": contact.address.city,
                "state": contact.address.state,
                "postal_code": contact.address.postal_code,
                "country": contact.address.country,
            },
            "phone_type": contact.phone_type,
            "languages": [
                {"language": row.language, "proficiency": row.proficiency} for row in contact.languages
            ],
        },
        "profile_resume_source": profile.resume_path.as_posix(),
        "profile_constraints_source": profile.constraints_path.as_posix(),
        "resume_file": str(Path(resume_file)),
        "letter_file": str(letter_file) if letter_file else None,
        "directory": str(Path(directory)),
        "browser_directory": str(profile.directory.resolve() / ".venator-browser"),
        "debugging_port": debugging_port,
        "questions": questions or [],
        "answers": answers or [],
        "prepared_resume": dict(prepared_resume) if isinstance(prepared_resume, Mapping) else None,
        "form_memory": str(form_memory) if form_memory else None,
        "profile_id": profile_id,
    }


def _field_result(field: Mapping[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {
        "label": field.get("label"),
        "kind": field.get("kind"),
        "status": status,
        "reason": reason,
    }


def _trusted_url(url: str, *, allow_test_urls: bool = False) -> bool:
    parsed = urlparse(url)
    if allow_test_urls and parsed.scheme == "file":
        return True
    if parsed.scheme == "https" and parsed.netloc in {
        "boards.greenhouse.io", "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
        "jobs.ashbyhq.com",
    }:
        return True
    from venator.browser.workday import is_workday_host

    return parsed.scheme == "https" and is_workday_host(parsed.hostname)


def _trusted_document(url: str, posting: Mapping[str, Any], *, allow_test_urls: bool = False) -> bool:
    if not _trusted_url(url, allow_test_urls=allow_test_urls):
        return False
    parsed = urlparse(url)
    if allow_test_urls and parsed.scheme == "file":
        return True
    key = str(posting.get("key") or "").split(":")
    if len(key) != 3:
        return False
    parts = parsed.path.strip("/").split("/")
    if key[0] == "workday":
        from venator.browser.workday import workday_identity

        return workday_identity(url, posting)
    if key[0] == "ashby":
        return parsed.hostname == "jobs.ashbyhq.com" and parts in (
            [key[1], key[2]], [key[1], key[2], "application"]
        )
    if key[0] != "greenhouse" or parsed.hostname == "jobs.ashbyhq.com":
        return False
    query = parse_qs(parsed.query)
    if len(parts) == 3 and parts[1] == "jobs":
        return parts[0] == key[1] and parts[2] == key[2]
    return parsed.path == "/embed/job_app" and query.get("for") == [key[1]] and query.get("token") == [key[2]]


def _confirmation_host(url: str, posting: Mapping[str, Any], *, allow_test_urls: bool = False) -> str | None:
    """Only a confirmation for this exact Posting is a receipt."""
    key = str(posting.get("key") or "").split(":")
    if len(key) != 3 or key[0] != "greenhouse" or not _trusted_url(url, allow_test_urls=allow_test_urls):
        return None
    parsed = urlparse(url)
    if parsed.scheme == "file":
        # Synthetic fixtures use a named file, never a real employer marker.
        return parsed.hostname or "fixture"
    parts = parsed.path.strip("/").split("/")
    if parts == [key[1], "jobs", key[2], "confirmation"]:
        return parsed.hostname
    if parts == ["embed", "job_app", "confirmation"]:
        query = parse_qs(parsed.query)
        if query.get("for") == [key[1]] and query.get("token") == [key[2]]:
            return parsed.hostname
    return None


def _target(frame: Frame, field: Mapping[str, Any], expected_url: str) -> Locator:
    # Stay in the inventoried document, never re-resolve into another frame.
    if frame.url != expected_url:
        raise ValueError("application document changed; continue manually")
    selector = field.get("selector")
    located = frame.locator(selector) if isinstance(selector, str) and selector else frame.get_by_label(
        str(field.get("label") or ""), exact=True
    )
    if located.count() != 1:
        raise ValueError("field is missing or ambiguous")
    identity = field.get("name")
    if identity and located.get_attribute("name") != identity and located.get_attribute("id") != identity:
        raise ValueError("the question identity changed")
    return located


def _upload_file(frame: Frame, field: Mapping[str, Any], path: Path, expected_url: str) -> None:
    if not path.is_file():
        raise ValueError(f"prepared file is missing: {path.name}")
    locator = _target(frame, field, expected_url)
    metadata = locator.evaluate(
        "element => ({tag: element.tagName.toLowerCase(), type: element.type})"
    )
    if metadata != {"tag": "input", "type": "file"}:
        raise ValueError("selector did not resolve to a file input")
    locator.set_input_files(str(path))
    selected = locator.evaluate(
        "element => element.files && element.files.length ? element.files[0].name : ''"
    )
    if selected != path.name:
        raise ValueError("file did not remain selected in the form")


def _looks_like_resume(label: str) -> bool:
    normalized = " ".join(label.casefold().replace("/", " ").split())
    return "resume" in normalized or normalized in {"cv", "curriculum vitae"}


def _looks_like_letter(label: str) -> bool:
    normalized = " ".join(label.casefold().replace("/", " ").split())
    return "cover letter" in normalized or normalized in {"letter", "coverletter", "covering letter"}


def _warning_for_unmapped(field: Mapping[str, Any]) -> str:
    label = str(field.get("label") or "unknown field")
    reason = str(field.get("reason") or "no confirmed Profile fact")
    return f"{label} left blank: {reason}."


def _assist_form(
    page: Page,
    posting: Mapping[str, Any],
    profile_resume: Mapping[str, Any],
    profile_constraints: Mapping[str, Any],
    resume_file: Path,
    letter_file: Path | None,
    profile_resume_source: str,
    profile_constraints_source: str,
    *,
    questions: list[dict] | None = None,
    allow_test_urls: bool = False,
    form_memory: Path | None = None,
    profile_id: str | None = None,
) -> tuple[int, list[str], list[dict[str, Any]]]:
    warnings: list[str] = []
    results: list[dict[str, Any]] = []
    deadline = time.monotonic() + ASSIST_TIMEOUT
    try:
        # Recon's general helper follows Apply links and inspects every frame.
        # Production assistance only inventories an already trusted document.
        candidates = []
        for candidate in page.frames:
            if time.monotonic() >= deadline:
                break
            if not _trusted_document(candidate.url, posting, allow_test_urls=allow_test_urls):
                continue
            if candidate.locator("input[type=password]").count():
                continue
            forms = candidate.locator("form")
            for index in range(forms.count()):
                if time.monotonic() >= deadline:
                    break
                form = forms.nth(index)
                candidates.append((_form_score(form), candidate, form))
        if not candidates:
            raise ValueError("no application form on a confirmed Posting document")
        _score, frame, form = max(candidates, key=lambda item: item[0])
        expected_url = frame.url
        form_fields = form.evaluate(_INVENTORY_SCRIPT)
    except Exception as error:
        return 0, [f"The Posting form could not be inventoried: {str(error).strip()}"], results

    if posting.get("source") == "ashby" and form_memory is not None:
        from venator.apply.form import remember_ashby_form
        remember_ashby_form(form_memory, posting, form_fields, profile_id)

    form_spec = {"fields": form_fields}
    plan = create_fill_plan(
        str(posting.get("key") or ""),
        _apply_url(posting, allow_test_urls=allow_test_urls),
        form_spec,
        profile_resume,
        profile_constraints,
        resume_file=resume_file,
        sources=FactSources(profile_resume_source, profile_constraints_source),
    )
    fields_by_label = {
        str(field.get("label")): field
        for field in form_fields
        if isinstance(field, Mapping) and isinstance(field.get("label"), str)
        and sum(item.get("label") == field.get("label") for item in form_fields) == 1
    }
    names = {row.get("name") for row in questions or []}
    api_labels = {row.get("label") for row in questions or []}
    by_name = {name: [field for field in form_fields if field.get("name") == name]
               for name in names if isinstance(name, str)}
    filled = 0
    mapped_labels: set[str] = set()
    for field in plan.get("fields", []):
        if not isinstance(field, Mapping):
            continue
        label = str(field.get("label") or "")
        if label in api_labels:
            continue
        spec_field = fields_by_label.get(label)
        if spec_field is not None and spec_field.get("name") in names:
            continue
        if spec_field is None:
            warning = f"{label or 'Unknown field'} left blank: form field changed after inventory."
            warnings.append(warning)
            results.append(_field_result(field, "skipped", warning))
            continue
        mapped_labels.add(label)
        try:
            if time.monotonic() >= deadline:
                raise ValueError("assistance time limit reached; continue manually")
            if field.get("kind") == "file":
                _upload_file(frame, spec_field, resume_file, expected_url)
            else:
                locator = _target(frame, spec_field, expected_url)
                _fill_control(frame, locator, dict(field), dict(spec_field))
        except (SkipField, PlaywrightError, ValueError) as error:
            reason = str(error).strip() or "could not safely fill this field"
            warnings.append(f"{label} left blank: {reason}.")
            results.append(_field_result(field, "skipped", reason))
        else:
            filled += 1
            results.append(_field_result(field, "filled", "confirmed Profile value entered"))

    for question in questions or []:
        name = question.get("name")
        label = str(question.get("label") or name or "")
        answer = question.get("answer")
        if answer is None:
            continue
        controls = by_name.get(name, [])
        reason = None
        if len(controls) != 1 or sum(item.get("label") == label for item in form_fields) != 1:
            reason = "question identity missing or ambiguous"
        else:
            live = controls[0]
            if live.get("kind") != question.get("kind") or (question.get("options") is not None
                    and live.get("options") != question.get("options")):
                reason = "question choices changed since review"
            elif isinstance(answer, str) and live.get("maxlength") and len(answer) > live["maxlength"]:
                reason = "answer exceeds the field's character limit"
            else:
                try:
                    locator = _target(frame, live, expected_url)
                    _fill_control(frame, locator, {"label": label, "kind": live["kind"], "value": answer}, live)
                except (SkipField, PlaywrightError, ValueError) as error:
                    reason = str(error).strip() or "question could not be filled"
        if reason:
            warnings.append(f"{label} left blank: {reason}.")
            results.append(_field_result(question, "skipped", reason))
        else:
            filled += 1
            results.append(_field_result(question, "filled", "reviewed answer"))

    for field in plan.get("unmapped", []):
        if isinstance(field, Mapping) and field.get("label") not in api_labels:
            if fields_by_label.get(str(field.get("label") or ""), {}).get("kind") == "file" and (
                _looks_like_resume(str(field.get("label") or ""))
                or _looks_like_letter(str(field.get("label") or ""))
            ):
                continue
            warnings.append(_warning_for_unmapped(field))
            results.append(_field_result(field, "skipped", str(field.get("reason") or "unmapped")))

    letter_uploaded = False
    for spec_field in form_fields:
        if not isinstance(spec_field, Mapping) or spec_field.get("kind") != "file":
            continue
        label = str(spec_field.get("label") or "")
        if time.monotonic() >= deadline:
            warnings.append(f"{label} left blank: assistance time limit reached.")
            continue
        if _looks_like_resume(label):
            if label in mapped_labels:
                continue
            try:
                _upload_file(frame, spec_field, resume_file, expected_url)
            except (PlaywrightError, ValueError) as error:
                warnings.append(f"{label} left blank: {str(error).strip() or 'resume upload failed'}.")
            else:
                filled += 1
                mapped_labels.add(label)
                results.append(_field_result(spec_field, "filled", "prepared resume uploaded"))
        elif _looks_like_letter(label):
            if letter_file is None:
                warnings.append(f"{label} left blank: no current prepared letter is available.")
                continue
            try:
                _upload_file(frame, spec_field, letter_file, expected_url)
            except (PlaywrightError, ValueError) as error:
                warnings.append(f"{label} left blank: {str(error).strip() or 'letter upload failed'}.")
            else:
                filled += 1
                letter_uploaded = True
                results.append(_field_result(spec_field, "filled", "current prepared letter uploaded"))

    if letter_file is not None and not letter_uploaded:
        if not any(
            isinstance(field, Mapping)
            and field.get("kind") == "file"
            and _looks_like_letter(str(field.get("label") or ""))
            for field in form_fields
        ):
            warnings.append("A prepared letter is available, but this form has no recognizable letter upload.")
    return filled, warnings, results


def _source_message(source: str, assisted: bool, *, filled: int) -> str:
    if source == "workday" and assisted:
        return (f"Opened Workday in a persistent browser; {filled} confirmed field(s) filled on the first step. "
                "Venator fills the step that is on screen and presses no Save and Continue, Back, Add, Delete "
                "or Submit; press each Save and Continue yourself.")
    if assisted:
        return f"Opened the {source} application in a persistent browser; {filled} confirmed fields filled. Review and submit manually."
    return f"Opened the {source} application in a persistent browser for manual completion."


def _form_host_confirmed(page: Page, posting: Mapping[str, Any], *, allow_test_urls: bool = False) -> bool:
    return any(_trusted_document(frame.url, posting, allow_test_urls=allow_test_urls) for frame in page.frames)


def _login_page_detected(page: Page) -> bool:
    """Avoid entering Profile facts into an ATS sign-in form."""

    path = (urlparse(page.url).path or "").casefold()
    if any(token in path for token in ("/login", "/signin", "/sign-in", "/authenticate", "/auth/")):
        return True
    try:
        text = page.locator("body").inner_text(timeout=500).casefold()
    except Exception:
        return False
    return (
        ("sign in" in text or "log in" in text)
        and page.locator("input[type=password]").count() > 0
    )


def _open_browser(request: Mapping[str, Any]) -> tuple[Any, Any, Page, str]:
    """Launch a persistent context, preferring bundled Chromium then channels."""

    directory = Path(str(request["browser_directory"]))
    profile_directory = directory / PROFILE_DIRECTORY_NAME
    profile_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_directory.chmod(0o700)
    port = request.get("debugging_port")
    launch_errors: list[str] = []
    playwright = sync_playwright().start()
    context = None
    for channel in (None, "chrome", "msedge"):
        options: dict[str, Any] = {
            "user_data_dir": str(profile_directory),
            "headless": os.environ.get("VENATOR_HANDOFF_HEADLESS") == "1",
            "viewport": {"width": 1440, "height": 1000},
            "timeout": 10_000,
            "args": (["--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}"]
                     if request.get("allow_test_urls") and port else []),
        }
        if channel is not None:
            options["channel"] = channel
        try:
            context = playwright.chromium.launch_persistent_context(**options)
        except Exception as error:
            launch_errors.append(f"{channel or 'bundled Chromium'}: {str(error).strip()}")
            continue
        browser_name = channel or "bundled Chromium"
        break
    if context is None:
        playwright.stop()
        raise RuntimeError("could not launch a visible browser: " + " | ".join(launch_errors)[-900:])
    context.set_default_timeout(1_500)
    pages = context.pages
    page = next((candidate for candidate in pages if candidate.url in {"", "about:blank"}), None)
    if page is None:
        page = context.new_page()
    return playwright, context, page, browser_name


def _navigation_and_fill(request: Mapping[str, Any], page: Page) -> tuple[dict[str, Any], int, list[str]]:
    posting = request.get("posting")
    if not isinstance(posting, Mapping):
        raise ValueError("handoff request has no posting")
    source = str(posting.get("source") or "unknown").casefold()
    allow_test_urls = bool(request.get("allow_test_urls"))
    apply_url = _apply_url(posting, allow_test_urls=allow_test_urls)
    warnings: list[str] = []
    try:
        page.goto(apply_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        page.wait_for_timeout(400)
    except Exception as error:
        warnings.append(f"The application page did not finish loading: {str(error).strip() or 'navigation failed'}.")

    filled = 0
    details: list[dict[str, Any]] = []
    host_confirmed = _form_host_confirmed(page, posting, allow_test_urls=allow_test_urls)
    login_detected = _login_page_detected(page)
    assisted = source in SUPPORTED_SOURCES and host_confirmed and not login_detected
    if source in SUPPORTED_SOURCES and login_detected:
        warnings.append("The application requires a login; sign in manually before entering Profile facts.")
    if source in SUPPORTED_SOURCES and not host_confirmed:
        warnings.append("The navigated page is outside this Posting's form; no fields or uploads were attempted.")
    if assisted and source == "workday":
        from venator.browser import workday

        workday_first: dict[str, Any] = {}
        try:
            step = workday.fill_current_step(page, request)
            filled = int(step.get("filled") or 0)
            details = [field for field in step.get("fields") or [] if isinstance(field, Mapping)]
            warnings.extend(str(item) for item in step.get("warnings") or [])
            workday_first = step
        except Exception as error:
            warnings.append(f"Workday assistance stopped before the first step: {str(error).strip()}.")
    elif assisted:
        try:
            filled, assist_warnings, details = _assist_form(
                page,
                posting,
                request.get("profile_resume") if isinstance(request.get("profile_resume"), Mapping) else {},
                request.get("profile_constraints") if isinstance(request.get("profile_constraints"), Mapping) else {},
                Path(str(request["resume_file"])),
                Path(str(request["letter_file"])) if request.get("letter_file") else None,
                str(request.get("profile_resume_source") or "resume.yaml"),
                str(request.get("profile_constraints_source") or "constraints.yaml"),
                questions=request.get("questions") if isinstance(request.get("questions"), list) else [],
                allow_test_urls=allow_test_urls,
                form_memory=Path(str(request["form_memory"])) if request.get("form_memory") else None,
                profile_id=str(request["profile_id"]) if request.get("profile_id") else None,
            )
            warnings.extend(assist_warnings)
        except Exception as error:
            warnings.append(f"Form assistance stopped before all fields were filled: {str(error).strip()}.")
    else:
        warnings.append(
            "Complete this form manually with the prepared files."
        )

    try:
        captcha = detect_captcha(page)
    except Exception:
        captcha = "unknown"
    if captcha != "none":
        warnings.append(f"Captcha detected ({captcha}); complete it manually.")

    try:
        submit_events = int(page.evaluate("() => window.__venatorHandoffSubmitEvents || 0"))
    except Exception:
        submit_events = 0
    if submit_events:
        warnings.append("The page reported a submit event; review the form before continuing.")
    result = {
        "message": _source_message(source, assisted, filled=filled),
        "filled": filled,
        "warnings": list(dict.fromkeys(warnings)),
        "source": source,
        "submit_events": submit_events,
        "fields": details,
    }
    if source == "workday":
        result["_workday"] = workday_first if assisted else {}
    return result, filled, warnings


_WATCH_SCRIPT = r"""
(() => {
  if (window.__venatorWatchInstalled) return;
  window.__venatorWatchInstalled = true;
  const blank = new WeakSet();
  const inventory = () => {
    document.querySelectorAll('input, select, textarea').forEach(el => {
      if (!el.value && !el.checked && el.getClientRects().length) blank.add(el);
    });
  };
  inventory();
  // A wizard that replaces its DOM in place re-arms this after each step, so a
  // field the person answered by hand stays a candidate in later steps.
  window.__venatorHandoffBlankInventory = inventory;
  document.addEventListener('click', event => {
    if (!event.isTrusted) return;
    const button = event.target.closest('button');
    window.venatorHandoffEvent({event: 'action', submit: Boolean(button
      && button.getAttribute('data-automation-id') === 'pageFooterNextButton'
      && button.textContent.trim().toLowerCase() === 'submit')});
  }, true);
  document.addEventListener('keydown', event => {
    if (event.isTrusted) window.venatorHandoffEvent({event: 'action'});
  }, true);
  document.addEventListener('change', event => {
    const el = event.target;
    if (!event.isTrusted || !blank.has(el) || !el.matches('input, select, textarea')) return;
    if (el.type === 'file' || el.type === 'password' || el.type === 'hidden') return;
    const label = el.type === 'radio'
      ? el.closest('fieldset')?.querySelector('legend')?.textContent?.trim() || ''
      : el.labels?.[0]?.textContent?.trim() || el.getAttribute('aria-label') || el.name || '';
    window.venatorHandoffEvent({event: 'change', label, name: el.name || el.id,
      login: Boolean(document.querySelector('input[type=password]')) || /\/(login|signin|sign-in|verify)/i.test(location.pathname),
      kind: el.type || el.tagName.toLowerCase(), value: el.type === 'checkbox' ? el.checked : el.value});
  }, true);
})()
"""


def _install_confirmation_watch(context: Any, request: Mapping[str, Any], state_path: Path, state: dict) -> Any:
    """The worker writes a bounded receipt; the app is the sole Track writer.

    Returns the frame inspector so a worker that owns a polling loop can call
    it again; a wizard whose confirmation is a row in a swapped-in panel, like
    Workday's Candidate Home, has no navigation event to hang off.
    """
    from venator.apply.classify import reserved

    posting = request.get("posting") or {}
    if posting.get("source") not in SUPPORTED_SOURCES:
        return lambda _frame: None
    acted = False
    allow_test_urls = bool(request.get("allow_test_urls"))

    def inspect(frame: Frame) -> None:
        if not acted or (_read_json(state_path) or {}).get("state") == "applied":
            return
        source = posting.get("source")
        if source == "workday":
            from venator.browser.workday import candidate_home_marker

            marker = candidate_home_marker(frame, posting, allow_test_urls=allow_test_urls)
            if marker is None:
                # No URL or employer text is persisted in the receipt state.
                return
            _write_json(state_path, {"state": "applied", "pid": state["pid"], "marker": marker,
                                     "candidates": state.get("candidates", []),
                                     "warnings": state.get("warnings", [])})
            return
        if source != "greenhouse":
            return
        host = _confirmation_host(frame.url, posting, allow_test_urls=allow_test_urls)
        if host is None:
            return
        # A path alone is not proof if the employer served an error/validation page.
        try:
            body = frame.locator("body").inner_text(timeout=500).casefold()
        except Exception:
            return
        if not any(word in body for word in ("thank you", "application received", "application submitted")):
            return
        if any(word in body for word in ("error", "validation failed", "try again")):
            return
        key = str(posting["key"]).split(":")
        marker = {"system": "greenhouse", "board": key[1], "job_id": key[2],
                  "host": host, "at": time.time()}
        # No URL or employer text is persisted in the receipt state.
        _write_json(state_path, {"state": "applied", "pid": state["pid"], "marker": marker,
                                 "candidates": state.get("candidates", [])})

    def on_event(source: dict, payload: dict) -> None:
        nonlocal acted
        frame = source.get("frame")
        if frame is None or not _trusted_document(frame.url, posting, allow_test_urls=allow_test_urls):
            return
        if payload.get("event") == "action":
            if posting.get("source") != "workday" or payload.get("submit") is True:
                acted = True
            return
        if payload.get("event") != "change" or payload.get("login") is True:
            return
        label = str(payload.get("label") or "")[:300]
        name = str(payload.get("name") or "")[:200]
        kind = str(payload.get("kind") or "")
        if reserved(label, kind=kind) is not None or reserved(name, kind=kind) is not None or kind in {"file", "password", "checkbox"}:
            return
        value = payload.get("value")
        if not isinstance(value, (str, bool)) or isinstance(value, str) and len(value) > 1800:
            return
        candidates = state.setdefault("candidates", [])
        if not name or not label or any(item["name"] == name for item in candidates):
            return
        candidates.append({"name": name, "label": label, "value": value})
        if len(candidates) <= 50:
            current = _read_json(state_path) or {}
            if current.get("state") == "ready":
                _write_json(state_path, {**current, "candidates": candidates})

    context.expose_binding("venatorHandoffEvent", on_event)
    context.add_init_script(_WATCH_SCRIPT)

    def attach(page: Page) -> None:
        page.on("framenavigated", inspect)
        for frame in page.frames:
            try:
                frame.evaluate(_WATCH_SCRIPT)
            except PlaywrightError:
                pass
            inspect(frame)

    context.on("page", attach)
    for page in context.pages:
        attach(page)
    return inspect


def _worker_main(request_path: Path, ready_path: Path, lock_path: Path, state_path: Path) -> int:
    request = _read_json(request_path)
    token = (request or {}).get("token") if request else None
    if not isinstance(token, str) or not token:
        token = request_path.stem.rsplit("-", 1)[-1]
    context = None
    playwright = None
    try:
        if request is None:
            raise RuntimeError("handoff request could not be read")
        request_path.unlink(missing_ok=True)
        _write_json(lock_path, {"token": token, "pid": os.getpid(), "state": "starting"})
        playwright, context, page, browser_name = _open_browser(request)
        # Count without preventing any event.  This is observability only; the
        # production handoff intentionally leaves state-changing requests alone.
        page.add_init_script(
            """
            window.__venatorHandoffSubmitEvents = 0;
            document.addEventListener('submit', () => {
              window.__venatorHandoffSubmitEvents += 1;
            }, true);
            """
        )
        result, _filled, _warnings = _navigation_and_fill(request, page)
        workday_first = result.pop("_workday", {}) if isinstance(result.get("_workday"), Mapping) else {}
        state = {
            "state": "ready",
            "token": token,
            "pid": os.getpid(),
            "browser": browser_name,
            "debugging_port": request.get("debugging_port"),
            "filled": result.get("filled", 0),
            "warnings": result.get("warnings", []),
            "message": result.get("message"),
            "fields": result.get("fields", []),
        }
        _write_json(state_path, state)
        inspect = _install_confirmation_watch(context, request, state_path, state)
        source = str((request.get("posting") or {}).get("source") or "")
        session = None
        if source == "workday":
            from venator.browser import workday

            session = workday.WorkdaySession(context, page, request,
                                             first_report=workday_first or None)
            session.warnings = list(dict.fromkeys([*result.get("warnings", []), *session.warnings]))
        _write_json(ready_path, {"ready": True, **result})
        _write_json(lock_path, {"token": token, "pid": os.getpid(), "state": "ready"})
        # Keep the persistent context alive until the user closes its pages.
        while context.pages:
            try:
                context.pages[0].wait_for_timeout(500)
            except Exception:
                # Closing the tab we were waiting on must not close other tabs.
                if not context.pages:
                    break
            if session is not None and (_read_json(state_path) or {}).get("state") == "ready":
                session.tick()
                current = _read_json(state_path) or {}
                if (session.steps != (current.get("workday") or {}).get("steps", [])
                        or session.warnings != current.get("warnings", [])):
                    _write_json(state_path, {**current, "filled": session.total_filled,
                                             "warnings": session.warnings,
                                             "workday": {"steps": session.steps}})
            for open_page in context.pages:
                try:
                    inspect(open_page.main_frame)
                except Exception:
                    pass
        return 0
    except Exception as error:
        message = " ".join(str(error).split())[:900] or "browser handoff failed"
        _write_json(
            state_path,
            {"state": "failed", "token": token, "pid": os.getpid(), "message": message, "warnings": [message]},
        )
        _write_json(ready_path, {"ready": False, "message": message, "filled": 0, "warnings": [message]})
        return 1
    finally:
        if context is not None:
            with suppress(Exception):
                context.close()
        if playwright is not None:
            with suppress(Exception):
                playwright.stop()
        with suppress(OSError):
            request_path.unlink()
        _release_lock(lock_path, token)
        current = _read_json(state_path) or {}
        if current.get("token") == token and current.get("state") == "ready":
            _write_json(
                state_path,
                {**current, "state": "closed", "closed_at": time.time()},
            )


def _terminate_worker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        with suppress(OSError):
            process.terminate()
    with suppress(Exception):
        process.wait(timeout=1.0)
    if process.poll() is None:
        with suppress(OSError):
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        with suppress(Exception):
            process.wait(timeout=1.0)


def start_handoff(
    posting: dict,
    profile: Profile,
    resume_file: Path,
    directory: Path,
    *,
    letter_file: Path | None = None,
    allow_test_urls: bool = False,
    questions: list[dict] | None = None,
    form_memory: Path | None = None,
    answers: list[dict] | None = None,
    prepared_resume: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a detached visible handoff and return after its ready handshake.

    The returned mapping has the stable public shape ``message``, ``filled``
    and ``warnings``.  The persistent browser belongs to a separate worker, so
    returning from this function does not close the window or block the caller.
    """

    if not isinstance(posting, Mapping):
        raise ValueError("posting must be a mapping")
    _apply_url(posting, allow_test_urls=allow_test_urls)
    resume_file = Path(resume_file).resolve()
    if not resume_file.is_file():
        raise ValueError(f"prepared resume is missing: {resume_file.name}")
    if letter_file is not None and not Path(letter_file).is_file():
        raise ValueError("prepared letter is missing")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    browser_directory = profile.directory.resolve() / ".venator-browser"
    browser_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    browser_directory.chmod(0o700)
    request_path = directory / f".handoff-request-{token}.json"
    ready_path = directory / f".handoff-ready-{token}.json"
    state_path = handoff_state_path(directory)
    debugging_port = _allocate_port() if allow_test_urls else None
    request = _request_payload(
        posting,
        profile,
        resume_file,
        directory,
        Path(letter_file).resolve() if letter_file is not None else None,
        debugging_port,
        questions,
        form_memory,
        profile.identifier,
        answers,
        prepared_resume,
    )
    request["allow_test_urls"] = allow_test_urls
    request["token"] = token
    lock_path = _lock_path(browser_directory)
    command = [
        sys.executable,
        "-m",
        "venator.browser.handoff",
        "--worker",
        str(request_path),
        str(ready_path),
        str(lock_path),
        str(state_path),
    ]
    lock_path = _acquire_lock(browser_directory, token)
    try:
        _write_json(request_path, request)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=(os.name != "nt"),
        )
    except Exception:
        request_path.unlink(missing_ok=True)
        _release_lock(lock_path, token)
        raise

    deadline = time.monotonic() + STARTUP_TIMEOUT
    try:
        while time.monotonic() < deadline:
            result = _read_json(ready_path)
            if result is not None:
                ready_path.unlink(missing_ok=True)
                if result.get("ready") is not True:
                    raise RuntimeError(str(result.get("message") or "browser handoff failed"))
                return {
                    "message": str(result.get("message") or "Application browser is ready."),
                    "filled": int(result.get("filled") or 0),
                    "warnings": [str(item) for item in result.get("warnings", []) if item],
                }
            if process.poll() is not None:
                raise RuntimeError("browser handoff worker exited before the browser was ready")
            time.sleep(0.05)
        raise TimeoutError("browser handoff did not become ready before the startup timeout")
    except Exception:
        _terminate_worker(process)
        request_path.unlink(missing_ok=True)
        ready_path.unlink(missing_ok=True)
        _release_lock(lock_path, token)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", nargs=4, metavar=("REQUEST", "READY", "LOCK", "STATE"))
    args = parser.parse_args()
    if args.worker is None:
        parser.error("this module is started by the application handoff")
    return _worker_main(*(Path(item) for item in args.worker))


if __name__ == "__main__":
    raise SystemExit(main())
