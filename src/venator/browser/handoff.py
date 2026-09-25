"""Open a prepared application in a persistent, user-visible browser.

The dry-run driver in :mod:`venator.browser.fill` is intentionally isolated
from this module.  A handoff uses a dedicated persistent Playwright profile so
the user can keep a login between sessions, leaves the browser open, and never
intercepts or submits a request.  The caller waits only for a small ready
handshake from a detached worker; the worker owns the browser for the rest of
its lifetime.

Greenhouse is the first assisted source.  Other sources are opened visibly and
reported as manual/download fallbacks until a source-specific flow is proved.
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


SUPPORTED_SOURCE = "greenhouse"
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


def _letter_file(directory: Path) -> Path | None:
    """Find the optional letter in the manifest's current document version."""

    directory = Path(directory)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, Mapping):
        return None
    version = manifest.get("version")
    if not isinstance(version, str) or not version.isalnum():
        return None
    path = directory / "versions" / version / "letter.txt"
    return path if path.is_file() else None


def _request_payload(
    posting: Mapping[str, Any],
    profile: Profile,
    resume_file: Path,
    directory: Path,
    letter_file: Path | None,
    debugging_port: int | None,
) -> dict[str, Any]:
    return {
        "posting": {
            "key": posting.get("key"),
            "source": posting.get("source"),
            "board": posting.get("board"),
            "title": posting.get("title"),
            "apply_url": posting.get("apply_url"),
            "url": posting.get("url"),
        },
        "profile_resume": dict(profile.resume),
        "profile_constraints": dict(profile.constraints),
        "profile_resume_source": profile.resume_path.as_posix(),
        "profile_constraints_source": profile.constraints_path.as_posix(),
        "resume_file": str(Path(resume_file)),
        "letter_file": str(letter_file) if letter_file else None,
        "directory": str(Path(directory)),
        "browser_directory": str(profile.directory.resolve() / ".venator-browser"),
        "debugging_port": debugging_port,
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
    return parsed.scheme == "https" and parsed.netloc in {
        "boards.greenhouse.io", "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
    }


def _trusted_document(url: str, posting: Mapping[str, Any], *, allow_test_urls: bool = False) -> bool:
    if not _trusted_url(url, allow_test_urls=allow_test_urls):
        return False
    parsed = urlparse(url)
    if allow_test_urls and parsed.scheme == "file":
        return True
    key = str(posting.get("key") or "").split(":")
    if len(key) != 3 or key[0] != "greenhouse":
        return False
    parts = parsed.path.strip("/").split("/")
    query = parse_qs(parsed.query)
    if len(parts) == 3 and parts[1] == "jobs":
        return parts[0] == key[1] and parts[2] == key[2]
    return parsed.path == "/embed/job_app" and query.get("for") == [key[1]] and query.get("token") == [key[2]]


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


def _assist_greenhouse(
    page: Page,
    posting: Mapping[str, Any],
    profile_resume: Mapping[str, Any],
    profile_constraints: Mapping[str, Any],
    resume_file: Path,
    letter_file: Path | None,
    profile_resume_source: str,
    profile_constraints_source: str,
    *,
    allow_test_urls: bool = False,
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
            raise ValueError("no application form on a confirmed Greenhouse document")
        _score, frame, form = max(candidates, key=lambda item: item[0])
        expected_url = frame.url
        form_fields = form.evaluate(_INVENTORY_SCRIPT)
    except Exception as error:
        return 0, [f"The Greenhouse form could not be inventoried: {str(error).strip()}"], results

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
    }
    filled = 0
    mapped_labels: set[str] = set()
    for field in plan.get("fields", []):
        if not isinstance(field, Mapping):
            continue
        label = str(field.get("label") or "")
        spec_field = fields_by_label.get(label)
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

    for field in plan.get("unmapped", []):
        if isinstance(field, Mapping):
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
    if assisted:
        return f"Opened the {source} application in a persistent browser; {filled} confirmed fields filled. Review and submit manually."
    return f"Opened the {source} application in a persistent browser for manual completion."


def _greenhouse_form_host_confirmed(page: Page, *, allow_test_urls: bool = False) -> bool:
    return any(_trusted_url(frame.url, allow_test_urls=allow_test_urls) for frame in page.frames)


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
    host_confirmed = _greenhouse_form_host_confirmed(
        page,
        allow_test_urls=allow_test_urls,
    )
    login_detected = _login_page_detected(page)
    assisted = source == SUPPORTED_SOURCE and host_confirmed and not login_detected
    if source == SUPPORTED_SOURCE and login_detected:
        warnings.append("The application requires a login; sign in manually before entering Profile facts.")
    if source == SUPPORTED_SOURCE and not host_confirmed:
        warnings.append(
            "The navigated page is outside the Greenhouse host; no automatic fields or uploads were attempted."
        )
    if assisted:
        try:
            filled, assist_warnings, details = _assist_greenhouse(
                page,
                posting,
                request.get("profile_resume") if isinstance(request.get("profile_resume"), Mapping) else {},
                request.get("profile_constraints") if isinstance(request.get("profile_constraints"), Mapping) else {},
                Path(str(request["resume_file"])),
                Path(str(request["letter_file"])) if request.get("letter_file") else None,
                str(request.get("profile_resume_source") or "resume.yaml"),
                str(request.get("profile_constraints_source") or "constraints.yaml"),
                allow_test_urls=allow_test_urls,
            )
            warnings.extend(assist_warnings)
        except Exception as error:
            warnings.append(f"Greenhouse assistance stopped before all fields were filled: {str(error).strip()}.")
    else:
        warnings.append(
            "Automatic assistance is currently limited to Greenhouse; download the prepared files and complete this form manually."
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
        "url": page.url,
        "submit_events": submit_events,
        "fields": details,
    }
    return result, filled, warnings


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
        state = {
            "state": "ready",
            "token": token,
            "pid": os.getpid(),
            "browser": browser_name,
            "debugging_port": request.get("debugging_port"),
            "url": result.get("url"),
            "filled": result.get("filled", 0),
            "warnings": result.get("warnings", []),
            "message": result.get("message"),
            "fields": result.get("fields", []),
        }
        _write_json(state_path, state)
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
    allow_test_urls: bool = False,
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
        _letter_file(directory),
        debugging_port,
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
