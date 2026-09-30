"""Fill the Workday wizard step that is on screen, without form actions.

Workday's apply wizard replaces the DOM in place: the URL stays
``/apply/autofillWithResume`` and Save and Continue and Submit share the
automation id ``pageFooterNextButton``. Step identity therefore comes from the
``progressBarActiveStep`` label plus the shape of the DOM, never from the URL or
an id alone.

This module writes only to a field's own control, and only when the field is
blank or when a confirmed Profile fact or answer-library row disagrees with what
is there:

* text and textarea fields, through the ordinary fill;
* checkbox and radio inputs, by a DOM click on the input itself — React records
  checkbox and radio changes on click, and this is not a mouse press on a
  button, a link, or navigation;
* month and year date spinbuttons, typed in by keyboard;
* the Autofill with Resume file input, through ``set_input_files``;
* a field's own combobox: the button inside that field's ``formField-*``
  container is pressed to open its listbox, and the option whose visible text
  matches exactly is chosen. A drop-down choice changes that field, not the
  form.

Add, Delete, navigation, and every step's Save and Continue and Submit are
never pressed. A combobox whose options hold no exact match, and a missing or
forbidden control, are left for the Owner and reported.

A value the Owner types is captured by the handoff watch before the next step
replaces the DOM; this module only re-arms the blank-field inventory after it
fills a step, so a field the Owner answered by hand stays a candidate and a
field Venator filled does not.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Page

from venator.answers.store import match
from venator.apply.classify import reserved
from venator.fill.mapping import map_field


AUTOFILL_STEP = "Autofill with Resume"
REVIEW_STEP = "Review"
RESUME_FILE_AUTOMATION_ID = "file-upload-input-ref"
AUTOFILL_PAGE_AUTOMATION_ID = "applyFlowAutoFillPage"
ACTIVE_STEP_SELECTOR = '[data-automation-id="progressBarActiveStep"]'
CANDIDATE_ROW_SELECTOR = '[data-automation-id="taskListRow"]'
CANDIDATE_TITLE_SELECTOR = '[data-automation-id="applicationTitle"]'
WORKDAY_HOST_SUFFIX = ".myworkdayjobs.com"
USE_LAST_APPLICATION = "use my last application"
INVENTORY_TIMEOUT_MS = 2_000
MAX_REPORTED_FIELDS = 80
#: A selection is given this long to show an employer request before the next one.
COMBOBOX_SETTLE_MS = 120

#: Controls Venator never presses, whatever their wrapper is called.
_FORBIDDEN_CLICK_IDS = frozenset({"pageFooterNextButton", "pageFooterBackButton", "delete-file", "add-button"})
_FORBIDDEN_CLICK_TEXT = re.compile(
    r"^(submit|save and continue|continue|back|delete|add|add another)(?:\b|$)",
    re.IGNORECASE,
)

#: Wrappers whose single text control comes from one confirmed Profile fact.
_SIMPLE_TEXT = {
    "formField-legalName--firstName": "legal_name.first",
    "formField-legalName--middleName": "legal_name.middle",
    "formField-legalName--lastName": "legal_name.last",
    "formField-addressLine1": "address.line1",
    "formField-addressLine2": "address.line2",
    "formField-city": "address.city",
    "formField-postalCode": "address.postal_code",
    "formField-phoneNumber": "contact.phone",
    "formField-linkedInAccount": "contact.linkedin",
}

#: Wrappers rendered as a button plus a popup listbox. The option whose visible
#: text matches the confirmed fact exactly is chosen from the field's own
#: listbox; a fact with no exact option is reported and left alone.
_COMBOBOX_FACTS = {
    "formField-country": ("address.country", "Country"),
    "formField-countryRegion": ("address.state", "State"),
    "formField-phoneType": ("phone_type", "Phone Device Type"),
    "formField-degree": ("education.degree", "Degree"),
}

_FLUENT_LEVEL = re.compile(r"(?:native(?: or bi-?lingual)?|bi-?lingual|fluent)(?: proficiency)?", re.IGNORECASE)
_ROW_PATTERN = re.compile(r"^(workExperience|education|language)-([A-Za-z0-9]+)--(.+)$")
_MONTH = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_DATE_RANGE = re.compile(r"^\s*(?P<start>[^-–]+?)\s*[-–]\s*(?P<end>.+?)\s*$")


class WorkdayError(ValueError):
    """A step could not be assisted without guessing."""


@dataclass
class FieldPlan:
    """One control and the confirmed value it should hold, if any."""

    label: str
    kind: str
    control_id: str
    value: str | bool | None
    source: str | None
    reason: str | None = None
    maxlength: int | None = None
    options: tuple[str, ...] = ()
    option_ids: tuple[str, ...] = ()


@dataclass
class StepResult:
    """What one pass over one wizard step did."""

    step: str | None
    signature: str
    filled: int = 0
    corrected: list[dict[str, Any]] = field(default_factory=list)
    left: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fields: list[dict[str, Any]] = field(default_factory=list)
    use_last_application: bool = False
    resume_uploaded: bool = False
    stopped: bool = False

    def report(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "signature": self.signature,
            "filled": self.filled,
            "corrected": self.corrected,
            "left": self.left,
            "warnings": self.warnings,
            "fields": self.fields,
            "resume_uploaded": self.resume_uploaded,
            "stopped": self.stopped,
        }


# --------------------------------------------------------------------- hosts


def is_workday_host(host: str | None) -> bool:
    """A tenant career host, never the bare suffix and never a lookalike."""
    value = (host or "").casefold()
    return value.endswith(WORKDAY_HOST_SUFFIX) and value != WORKDAY_HOST_SUFFIX.lstrip(".")


def workday_host_matches(url: str, posting: Mapping[str, Any]) -> bool:
    """Whether one URL is on this Posting's tenant host, path aside.

    Candidate Home lives on the same tenant host but on a path that names no
    requisition, so receipt detection trusts the host while step filling also
    demands the site and the requisition in the path.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not is_workday_host(parsed.hostname):
        return False
    key = str(posting.get("key") or "").split(":")
    if len(key) != 3 or key[0] != "workday" or not key[1]:
        return False
    tenant = key[1].partition("~")[0].casefold()
    host = (parsed.hostname or "").casefold()
    return host == f"{tenant}{WORKDAY_HOST_SUFFIX}"


def workday_identity(url: str, posting: Mapping[str, Any], *, allow_test_urls: bool = False) -> bool:
    """Whether one URL is this Posting's Workday document.

    The board names the tenant (``acme.wd1``) and the site
    (``AcmeCareers``). A document is trusted only when the host is that
    tenant's ``*.myworkdayjobs.com`` host, the path names the site, and the
    requisition appears in the path. Synthetic fixtures use a ``file:`` document
    only through the explicit test seam.
    """
    parsed = urlparse(url)
    if allow_test_urls and parsed.scheme == "file":
        return True
    if not workday_host_matches(url, posting):
        return False
    key = str(posting.get("key") or "").split(":")
    _, separator, site = key[1].partition("~")
    parts = parsed.path.strip("/").split("/")
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    if not separator or not site or len(parts) < 3 or parts[:2] != [site, "job"]:
        return False
    job_parts = parts[2:parts.index("apply")] if "apply" in parts[2:] else parts[2:]
    return any(re.search(r"(?:^|_)" + re.escape(key[2]) + r"(?:-\d+)?$", part) for part in job_parts)


def workday_frame(page: Page, posting: Mapping[str, Any], *, allow_test_urls: bool = False) -> Frame | None:
    """The frame holding this Posting's wizard, if it is on screen at all."""
    for frame in page.frames:
        if not workday_identity(frame.url, posting, allow_test_urls=allow_test_urls):
            continue
        return frame
    return None


def sign_in_page(frame: Frame) -> bool:
    """Sign-in and verification pages get nothing typed, ever."""
    path = (urlparse(frame.url).path or "").casefold()
    if any(token in path for token in ("/login", "/signin", "/sign-in", "/authenticate", "/verify")):
        return True
    try:
        return frame.locator("input[type=password]").count() > 0
    except PlaywrightError:
        return False


# ----------------------------------------------------------------- inventory


_INVENTORY_SCRIPT = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").replace(/\s*\*+\s*$/, "").trim();
  const labelFor = (element) => {
    if (element.id) {
      const explicit = document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
      if (explicit) return clean(explicit.textContent);
    }
    const wrapped = element.closest("label");
    if (wrapped) return clean(wrapped.textContent);
    return clean(element.getAttribute("aria-label"));
  };
  const wrappers = Array.from(document.querySelectorAll('[data-automation-id^="formField-"]'))
    .filter((wrapper) => wrapper.getClientRects().length > 0 || wrapper.querySelector('input[type="file"]'));
  return wrappers.map((wrapper) => {
    const automationId = wrapper.getAttribute("data-automation-id") || "";
    const labelNode = wrapper.querySelector("label");
    const button = wrapper.querySelector("button[aria-label]");
    const controls = Array.from(wrapper.querySelectorAll("input, textarea, select"))
      .filter((element) => (element.getAttribute("type") || "").toLowerCase() !== "hidden"
        && element.getAttribute("aria-hidden") !== "true")
      .map((element) => ({
        tag: element.tagName.toLowerCase(),
        type: (element.getAttribute("type") || "text").toLowerCase(),
        id: element.id || null,
        name: element.getAttribute("name") || null,
        automation_id: element.getAttribute("data-automation-id") || null,
        label: labelFor(element),
        value: element.value == null ? "" : String(element.value),
        checked: element.checked === true,
        files: element.files ? element.files.length : 0,
        required: element.getAttribute("aria-required") === "true" || element.required === true,
        maxlength: element.maxLength > 0 ? element.maxLength : null,
        role: (element.getAttribute("role") || "").toLowerCase(),
        aria_valuenow: element.getAttribute("aria-valuenow"),
        aria_valuetext: element.getAttribute("aria-valuetext"),
        options: element.tagName.toLowerCase() === "select"
          ? Array.from(element.options).filter((o) => !o.disabled && o.value !== "").map((o) => o.textContent.trim())
          : null,
        visible: element.getClientRects().length > 0,
      }));
    return {
      wrapper: automationId,
      label: clean(labelNode ? labelNode.textContent : ""),
      visible: wrapper.getClientRects().length > 0,
      button: button ? {
        text: clean(button.textContent),
        aria: clean(button.getAttribute("aria-label")),
        id: button.id || null,
        automation_id: button.getAttribute("data-automation-id") || null,
        haspopup: (button.getAttribute("aria-haspopup") || "").toLowerCase(),
        role: (button.getAttribute("role") || "").toLowerCase(),
        expanded: button.getAttribute("aria-expanded") === "true",
      } : null,
      controls: controls,
    };
  });
}
"""


def active_step(frame: Frame) -> str | None:
    """The visible wizard step, from ``progressBarActiveStep``'s own label."""
    try:
        return frame.evaluate(
            """() => {
              const step = document.querySelector('[data-automation-id="progressBarActiveStep"]');
              if (!step) return null;
              const labels = Array.from(step.querySelectorAll("label")).map((node) => node.textContent.trim()).filter(Boolean);
              return labels.length ? labels[labels.length - 1] : step.textContent.trim();
            }"""
        )
    except PlaywrightError:
        return None


def _step_signature(step: str | None, wrappers: Sequence[Mapping[str, Any]]) -> str:
    """Step name plus DOM shape. The URL never participates."""
    first = str(wrappers[0].get("wrapper") or "") if wrappers else ""
    count = sum(len(item.get("controls") or []) for item in wrappers)
    return f"{step or 'unknown'}|{len(wrappers)}|{first}|{count}"


def _blank_inventory(frame: Frame) -> None:
    try:
        frame.evaluate("() => { if (window.__venatorHandoffBlankInventory) window.__venatorHandoffBlankInventory(); }")
    except PlaywrightError:
        return


def _use_last_application(frame: Frame) -> bool:
    try:
        return USE_LAST_APPLICATION in (frame.locator("body").inner_text(timeout=500) or "").casefold()
    except PlaywrightError:
        return False


class _NetworkWatch:
    """Count non-GET requests one fill pass caused. Observability only.

    The handoff never intercepts or blocks a request; this only watches whether
    the employer answered a selection with a save, in which case filling stops
    and says so.
    """

    def __init__(self, page: Page) -> None:
        self.count = 0
        self._page = page
        self._handler = self._record
        page.on("request", self._handler)

    def _record(self, request: Any) -> None:
        method = str(getattr(request, "method", "") or "").upper()
        if method and method not in {"GET", "HEAD", "OPTIONS"}:
            self.count += 1

    def close(self) -> None:
        with suppress(Exception):
            self._page.remove_listener("request", self._handler)


def _combobox_button(wrapper: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The wrapper's own drop-down button, when it is one."""
    button = wrapper.get("button")
    if not isinstance(button, Mapping):
        return None
    if str(button.get("haspopup") or "") == "listbox" or str(button.get("role") or "") == "combobox":
        return button
    return None


def _forbidden_click(element_id: str | None, automation_id: str | None, text: str | None) -> bool:
    """Whether pressing this control could submit, navigate, or change rows."""
    if (element_id or "") in _FORBIDDEN_CLICK_IDS or (automation_id or "") in _FORBIDDEN_CLICK_IDS:
        return True
    return bool(_FORBIDDEN_CLICK_TEXT.match(" ".join((text or "").split())))


def _record_left(step: StepResult, plan: FieldPlan, reason: str, current: str | None = None) -> None:
    entry: dict[str, Any] = {"label": plan.label, "reason": reason, "source": plan.source}
    if current:
        entry["current"] = current
    if isinstance(plan.value, str) and plan.value:
        entry["expected"] = plan.value
    step.left.append(entry)


# --------------------------------------------------------------- fact lookup


def _contact(request: Mapping[str, Any]) -> Mapping[str, Any]:
    value = request.get("profile_contact")
    return value if isinstance(value, Mapping) else {}


def _resume(request: Mapping[str, Any]) -> Mapping[str, Any]:
    value = request.get("profile_resume")
    return value if isinstance(value, Mapping) else {}


def _fact(request: Mapping[str, Any], path: str) -> tuple[str, str] | None:
    """One confirmed Profile value, with the file and dotted path that holds it."""
    contact = _contact(request)
    resume = _resume(request)
    if path == "legal_name.first":
        value = (contact.get("legal_name") or {}).get("first")
    elif path == "legal_name.middle":
        value = (contact.get("legal_name") or {}).get("middle")
    elif path == "legal_name.last":
        value = (contact.get("legal_name") or {}).get("last")
    elif path.startswith("address."):
        value = (contact.get("address") or {}).get(path.split(".", 1)[1])
    elif path == "phone_type":
        value = contact.get("phone_type")
    elif path == "contact.phone":
        value = (resume.get("contact") or {}).get("phone")
    elif path == "contact.linkedin":
        value = (resume.get("contact") or {}).get("linkedin")
    else:
        value = None
    if not isinstance(value, str) or not value.strip():
        return None
    if path.startswith("legal_name."):
        source = "resume.yaml:contact.legal_name"
    elif path.startswith("address."):
        source = "resume.yaml:contact.address"
    elif path == "phone_type":
        source = "resume.yaml:contact.phone_type"
    else:
        source = f"resume.yaml:{path}"
    return value.strip(), source


def _experience(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The tailored entries this Posting's prepared version actually lists.

    A correction goes toward the file that will be uploaded, never back to the
    base résumé's wording; the base is read only when no prepared version was
    passed at all.
    """
    prepared = request.get("prepared_resume")
    source = prepared if isinstance(prepared, Mapping) and isinstance(prepared.get("experience"), list) else _resume(request)
    value = source.get("experience")
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _education(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    prepared = request.get("prepared_resume")
    source = prepared if isinstance(prepared, Mapping) and isinstance(prepared.get("education"), list) else _resume(request)
    value = source.get("education")
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _fact_source(request: Mapping[str, Any], path: str) -> str:
    """Name the document a correction came from: the tailored version when present."""
    if isinstance(request.get("prepared_resume"), Mapping):
        return f"prepared resume:{path}"
    return f"resume.yaml:{path}"


def _languages(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = _contact(request).get("languages")
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _bullets(entry: Mapping[str, Any]) -> str:
    bullets = entry.get("bullets")
    if not isinstance(bullets, list):
        return ""
    lines: list[str] = []
    for item in bullets:
        if isinstance(item, Mapping):
            text = item.get("text") or item.get("content") or item.get("value")
        else:
            text = item
        if isinstance(text, str) and text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


def _field_of_study(degree: object) -> str | None:
    """The field a confirmed degree names, when it names one itself."""
    if not isinstance(degree, str):
        return None
    match_ = re.search(r"\bin\s+(.+)$", degree.strip(), re.IGNORECASE)
    if match_ is None:
        return None
    value = match_.group(1).strip(" ,.")
    return value or None


def _dates(entry: Mapping[str, Any]) -> tuple[tuple[int, int] | None, tuple[int, int] | None, bool]:
    """Read ``dates`` into start/end month-year and a Present flag.

    Returns ``(None, None, False)`` when the dates cannot be read; an unread
    date is an unknown, never an inferred one.
    """
    value = entry.get("dates")
    if not isinstance(value, str):
        return None, None, False
    range_ = _DATE_RANGE.match(value)
    if range_ is None:
        return None, None, False
    current = range_.group("end").strip().casefold() == "present"

    def parse(part: str) -> tuple[int, int] | None:
        words = re.findall(r"[A-Za-z]+|\d+", part)
        month = year = None
        for word in words:
            lowered = word.lower()
            if lowered in _MONTH:
                month = _MONTH[lowered]
            elif word.isdigit() and len(word) == 4:
                year = int(word)
        return (month, year) if month is not None and year is not None else None

    return parse(range_.group("start")), None if current else parse(range_.group("end")), current


# ------------------------------------------------------------------ writing


def _control(frame: Frame, control_id: str) -> Any:
    locator = frame.locator(f'[id="{control_id}"]')
    if locator.count() != 1:
        raise WorkdayError("control is missing or ambiguous")
    return locator


def _write_text(frame: Frame, control_id: str, value: str, maxlength: int | None) -> None:
    if maxlength is not None and len(value) > maxlength:
        raise WorkdayError("value exceeds the field's character limit")
    locator = _control(frame, control_id)
    locator.fill(value)
    if locator.input_value() != value:
        raise WorkdayError("entered value did not remain in the field")


def _write_checked(frame: Frame, control_id: str, checked: bool) -> None:
    """React records checkbox and radio changes on click; click the input only."""
    locator = _control(frame, control_id)
    locator.evaluate(
        "(element, want) => { if (element.checked !== want) element.click(); }",
        checked,
    )
    if locator.is_checked() is not checked:
        raise WorkdayError("checkbox or radio did not take the value")


def _write_spinbutton(frame: Frame, control_id: str, value: str) -> None:
    """Type a month or year into its spinbutton and confirm it took."""
    locator = _control(frame, control_id)
    metadata = locator.evaluate(
        "element => ({tag: element.tagName.toLowerCase(),"
        " role: (element.getAttribute('role') || '').toLowerCase(),"
        " hidden: element.getAttribute('aria-hidden') === 'true'})"
    )
    if metadata != {"tag": "input", "role": "spinbutton", "hidden": False}:
        raise WorkdayError("the date control is not a visible spinbutton")
    locator.click()
    locator.press("ControlOrMeta+a")
    locator.press_sequentially(value, delay=10)
    current = str(locator.get_attribute("aria-valuenow") or "")
    if current.lstrip("0") != value.lstrip("0"):
        locator.press("Enter")
        current = str(locator.get_attribute("aria-valuenow") or "")
    if current.lstrip("0") != value.lstrip("0"):
        raise WorkdayError("entered date did not remain in the field")


def _upload(frame: Frame, control_id: str, path: Path) -> bool:
    locator = _control(frame, control_id)
    metadata = locator.evaluate("element => ({tag: element.tagName.toLowerCase(), type: element.type})")
    if metadata != {"tag": "input", "type": "file"}:
        raise WorkdayError("the resume field is not a file input")
    if locator.evaluate("element => element.files && element.files.length"):
        return False
    locator.set_input_files(str(path))
    selected = locator.evaluate("element => element.files && element.files.length ? element.files[0].name : ''")
    if selected != path.name:
        raise WorkdayError("the resume did not remain selected")
    return True


# ------------------------------------------------------------- step analysis


def _result_fields(wrapper: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [control for control in wrapper.get("controls") or [] if isinstance(control, Mapping)]


def _plan_simple(request: Mapping[str, Any], wrapper: Mapping[str, Any]) -> FieldPlan | None:
    path = _SIMPLE_TEXT.get(str(wrapper.get("wrapper") or ""))
    if path is None:
        return None
    controls = [control for control in _result_fields(wrapper) if control.get("type") not in {"file", "checkbox", "radio"}]
    if len(controls) != 1 or not controls[0].get("id"):
        return None
    fact = _fact(request, path)
    if fact is None:
        return None
    value, source = fact
    return FieldPlan(
        label=str(wrapper.get("label") or controls[0].get("label") or wrapper.get("wrapper")),
        kind="text",
        control_id=str(controls[0]["id"]),
        value=value,
        source=source,
        maxlength=controls[0].get("maxlength") if isinstance(controls[0].get("maxlength"), int) else None,
    )


def _row_controls(wrapper: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The controls of one Workday row, keyed by the field after ``--``."""
    result: dict[str, Mapping[str, Any]] = {}
    for control in _result_fields(wrapper):
        parsed = _ROW_PATTERN.match(str(control.get("id") or ""))
        if parsed:
            result.setdefault(parsed.group(3), control)
    return result


def _workspace(wrapper: Mapping[str, Any]) -> str | None:
    """The transient row token and family, e.g. ``workExperience-10``.

    A Workday row often carries its identity on a drop-down button whose
    control id never reaches the hidden input, so the button's id is read too.
    """
    candidates = [str(control.get("id") or "") for control in _result_fields(wrapper)]
    button = wrapper.get("button") if isinstance(wrapper.get("button"), Mapping) else {}
    candidates.append(str(button.get("id") or ""))
    for candidate in candidates:
        parsed = _ROW_PATTERN.match(candidate)
        if parsed:
            return f"{parsed.group(1)}-{parsed.group(2)}"
    return None


def _group_rows(wrappers: Sequence[Mapping[str, Any]], kind: str) -> list[list[Mapping[str, Any]]]:
    """Rows grouped by their transient Workday token, in DOM order."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    order: list[str] = []
    for wrapper in wrappers:
        family = _workspace(wrapper)
        if family is None:
            continue
        name, _, token = family.rpartition("-")
        if name != kind:
            continue
        if token not in grouped:
            grouped[token] = []
            order.append(token)
        grouped[token].append(wrapper)
    return [grouped[token] for token in order]


def _date_plans(
    request: Mapping[str, Any],
    entry: Mapping[str, Any],
    fields: Mapping[str, Mapping[str, Any]],
    *,
    label_base: str,
    path: str,
    index: int,
) -> list[FieldPlan]:
    """Month and year spinbutton plans for one dated entry.

    Only a date the entry actually holds is written. A Present entry has no end
    date: any end date already on screen is left alone and reported, never
    cleared.
    """
    start, end, current = _dates(entry)
    plans: list[FieldPlan] = []
    for prefix, parsed in (("startDate", start), ("endDate", end)):
        if parsed is None:
            continue
        month, year = parsed
        for part, value in (("Month", month), ("Year", year)):
            control = fields.get(f"{prefix}-dateSection{part}-input")
            if control is None or not control.get("id"):
                continue
            if str(control.get("aria_valuenow") or "").lstrip("0") == str(value):
                continue
            plans.append(FieldPlan(
                label=f"{label_base}: {prefix} {part}",
                kind="spinbutton",
                control_id=str(control["id"]),
                value=str(value),
                source=_fact_source(request, f"{path}.{index}.dates"),
            ))
    if current:
        end_control = fields.get("endDate-dateSectionMonth-input") or fields.get("endDate-dateSectionYear-input")
        shown = str((end_control or {}).get("aria_valuenow") or "")
        if shown:
            plans.append(FieldPlan(
                label=f"{label_base}: end date",
                kind="report",
                control_id="",
                value=None,
                source=_fact_source(request, f"{path}.{index}.dates"),
                reason=(
                    f"{label_base} has an end date on screen, but your dates say Present. "
                    "Venator cannot set a date to Present; clear it yourself."
                ),
            ))
    return plans


def _experience_plans(request: Mapping[str, Any], wrappers: Sequence[Mapping[str, Any]]) -> list[FieldPlan]:
    experiences = _experience(request)
    plans: list[FieldPlan] = []
    for index, row in enumerate(_group_rows(wrappers, "workExperience")):
        if index >= len(experiences):
            break
        entry = experiences[index]
        fields: dict[str, Mapping[str, Any]] = {}
        for wrapper in row:
            fields.update(_row_controls(wrapper))
        label_base = str(entry.get("role") or "Experience")
        checks = {
            "jobTitle": entry.get("role"),
            "companyName": entry.get("org"),
            "location": entry.get("location"),
            "roleDescription": _bullets(entry),
        }
        for field_name, value in checks.items():
            control = fields.get(field_name)
            if control is None or not control.get("id") or not isinstance(value, str) or not value.strip():
                continue
            plans.append(FieldPlan(
                label=f"{label_base}: {field_name}",
                kind="text" if field_name != "roleDescription" else "textarea",
                control_id=str(control["id"]),
                value=value.strip(),
                source=_fact_source(request, f"experience.{index}.{field_name}"),
                maxlength=control.get("maxlength") if isinstance(control.get("maxlength"), int) else None,
            ))
        current = fields.get("currentlyWorkHere")
        _, end, is_current = _dates(entry)
        if current is not None and current.get("id") and (is_current or end is not None):
            plans.append(FieldPlan(
                label=f"{label_base}: I currently work here",
                kind="checkbox",
                control_id=str(current["id"]),
                value=is_current,
                source=_fact_source(request, f"experience.{index}.dates"),
            ))
        plans.extend(_date_plans(request, entry, fields, label_base=label_base, path="experience", index=index))
    return plans


def _education_plans(request: Mapping[str, Any], wrappers: Sequence[Mapping[str, Any]]) -> list[FieldPlan]:
    entries = _education(request)
    plans: list[FieldPlan] = []
    for index, row in enumerate(_group_rows(wrappers, "education")):
        if index >= len(entries):
            break
        entry = entries[index]
        fields: dict[str, Mapping[str, Any]] = {}
        for wrapper in row:
            fields.update(_row_controls(wrapper))
        label_base = str(entry.get("org") or "Education")
        values = {
            "schoolName": (entry.get("org"), _fact_source(request, f"education.{index}.org")),
            "fieldOfStudy": (_field_of_study(entry.get("degree")), _fact_source(request, f"education.{index}.degree")),
            "gradeAverage": (entry.get("gpa"), _fact_source(request, f"education.{index}.gpa")),
        }
        for field_name, (value, source) in values.items():
            control = fields.get(field_name)
            if control is None or not control.get("id") or not isinstance(value, str) or not value.strip():
                continue
            plans.append(FieldPlan(
                label=f"Education: {field_name}",
                kind="text",
                control_id=str(control["id"]),
                value=value.strip(),
                source=source,
                maxlength=control.get("maxlength") if isinstance(control.get("maxlength"), int) else None,
            ))
        plans.extend(_date_plans(request, entry, fields, label_base=label_base, path="education", index=index))
    return plans


def _language_plans(request: Mapping[str, Any], wrappers: Sequence[Mapping[str, Any]]) -> list[FieldPlan]:
    """Check ``I am fluent`` only for a language the Profile calls fluent.

    The language and overall-proficiency controls are Workday drop-down buttons;
    they are planned in ``_combobox_plans``.
    """
    languages = _languages(request)
    plans: list[FieldPlan] = []
    rows = _group_rows(wrappers, "language")
    for index, row in enumerate(rows):
        if index >= len(languages):
            break
        entry = languages[index]
        language = str(entry.get("language") or "")
        proficiency = str(entry.get("proficiency") or "")
        controls: dict[str, Mapping[str, Any]] = {}
        for wrapper in row:
            controls.update(_row_controls(wrapper))
        native = controls.get("native")
        if native is None or not native.get("id"):
            continue
        plans.append(FieldPlan(
            label=f"{language}: I am fluent in this language",
            kind="checkbox",
            control_id=str(native["id"]),
            value=_FLUENT_LEVEL.fullmatch(proficiency.strip()) is not None,
            source=f"resume.yaml:languages.{index}.proficiency",
        ))
    return plans


def _library_entries(request: Mapping[str, Any]) -> dict[tuple[str, str], dict]:
    entries: dict[tuple[str, str], dict] = {}
    value = request.get("answers")
    if isinstance(value, list):
        for row in value:
            if isinstance(row, Mapping) and isinstance(row.get("question"), str) and isinstance(row.get("scope"), str):
                entries[(row["question"], row["scope"])] = dict(row)
    return entries


def _question_plan(
    request: Mapping[str, Any],
    wrapper: Mapping[str, Any],
) -> FieldPlan | None:
    """One question answered from the library, an explicit EEO row, or a Profile fact."""
    label = str(wrapper.get("label") or "").strip()
    if not label:
        return None
    if _combobox_button(wrapper) is not None:
        return None  # the drop-down path fills it, or reports its options
    controls = _result_fields(wrapper)
    if not controls:
        return None
    posting = request.get("posting") if isinstance(request.get("posting"), Mapping) else {}
    board = str(posting.get("board") or "")
    company = str(posting.get("company") or posting.get("employer") or "")
    radios = [control for control in controls if control.get("type") == "radio" and control.get("id")]
    if radios:
        options = tuple(str(control.get("label") or control.get("value") or "") for control in radios)
        option_ids = tuple(str(control["id"]) for control in radios)
        category = reserved(label, kind="radio")
        value = _reserved_answer(request, label, category, "radio", list(options))
        source = category or "answer library"
        if value is None:
            row = match(_library_entries(request), text=label, board=board, company=company,
                        kind="radio", options=list(options))
            if row is not None:
                value, source = row["answer"], row["id"]
        if value is None:
            return None
        literal = "Yes" if value is True else "No" if value is False else str(value)
        if literal not in options:
            return None
        return FieldPlan(label=label, kind="radio", control_id=option_ids[options.index(literal)],
                         value=literal, source=source, options=options, option_ids=option_ids)
    control = controls[0]
    control_id = control.get("id")
    if not control_id:
        return None
    tag = str(control.get("tag") or "")
    kind = "textarea" if tag == "textarea" else "checkbox" if control.get("type") == "checkbox" else "text"
    category = reserved(label, kind=kind)
    value = _reserved_answer(request, label, category, kind, [])
    source = category or "Profile fact"
    if value is None:
        row = match(_library_entries(request), text=label, board=board, company=company, kind=kind, options=None)
        if row is None:
            return None
        value, source = row["answer"], row["id"]
    maxlength = control.get("maxlength") if isinstance(control.get("maxlength"), int) else None
    return FieldPlan(label=label, kind=kind, control_id=str(control_id), value=value,
                     source=source, maxlength=maxlength)


def _reserved_answer(request: Mapping[str, Any], label: str, category: str | None, kind: str,
                     options: list[str]) -> str | bool | None:
    """Answer a reserved question only from its own explicit Profile fact or EEO row.

    Sponsorship and authorization read ``constraints.yaml`` through the same
    mapper a review sheet uses, so a sponsorship question can never become a
    "No" and an EEO question falls through to an explicitly-flagged row only.
    """
    if category in {"sponsorship", "authorization", "authorization_sponsorship"}:
        mapping = map_field({"label": label, "kind": kind, "options": options or None},
                            _resume(request), request.get("profile_constraints") or {})
        return mapping.value if mapping.mapped else None
    if category == "eeo":
        posting = request.get("posting") if isinstance(request.get("posting"), Mapping) else {}
        row = match(_library_entries(request), text=label,
                    board=str(posting.get("board") or ""),
                    company=str(posting.get("company") or posting.get("employer") or ""),
                    kind=kind, options=options or None)
        if row is not None and row.get("eeo") is True:
            return row["answer"]
    return None


# ------------------------------------------------------------- plan reports


def _is_question(wrapper: Mapping[str, Any]) -> bool:
    automation_id = str(wrapper.get("wrapper") or "")
    if not wrapper.get("label"):
        return False
    if automation_id in _SIMPLE_TEXT or automation_id in _COMBOBOX_FACTS:
        return False
    if _workspace(wrapper) is not None:
        return False
    controls = _result_fields(wrapper)
    if not controls:
        return bool(wrapper.get("button"))
    return any(control.get("type") != "file" for control in controls)


def _combo_value(request: Mapping[str, Any], path: str, index: int | None = None) -> tuple[str, str] | None:
    if path == "education.degree":
        entries = _education(request)
        if index is None or index >= len(entries):
            return None
        value = entries[index].get("degree")
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip(), _fact_source(request, f"education.{index}.degree")
    return _fact(request, path)


def _matches(current: str, value: str) -> bool:
    return " ".join(current.casefold().split()) == " ".join(value.casefold().split())


def _combobox_plans(request: Mapping[str, Any], wrappers: Sequence[Mapping[str, Any]]) -> list[FieldPlan]:
    """Every combobox this step can fill from a confirmed fact or answer row.

    Only a wrapper's own drop-down button becomes a plan; the option text is
    checked against the listbox at press time, so a fact with no exact option is
    left alone and reported rather than guessed at.
    """
    plans: list[FieldPlan] = []
    for wrapper in wrappers:
        automation_id = str(wrapper.get("wrapper") or "")
        button = _combobox_button(wrapper)
        if button is None or not button.get("id"):
            continue
        if automation_id == "formField-degree":
            continue  # one per education row below, positioned by the row token
        if automation_id in _COMBOBOX_FACTS:
            path, label = _COMBOBOX_FACTS[automation_id]
            fact = _combo_value(request, path)
            if fact is not None:
                plans.append(FieldPlan(label=label, kind="combobox", control_id=str(button["id"]),
                                       value=fact[0], source=fact[1]))
            continue
        if _workspace(wrapper) is not None:
            continue  # a language row's own buttons are planned below
        if _is_question(wrapper):
            plan = _question_combobox_plan(request, wrapper, button)
            if plan is not None:
                plans.append(plan)
    plans.extend(_education_combobox_plans(request, wrappers))
    plans.extend(_language_combobox_plans(request, wrappers))
    return plans


def _education_combobox_plans(request: Mapping[str, Any], wrappers: Sequence[Mapping[str, Any]]) -> list[FieldPlan]:
    plans: list[FieldPlan] = []
    for index, row in enumerate(_group_rows(wrappers, "education")):
        for wrapper in row:
            if str(wrapper.get("wrapper") or "") != "formField-degree":
                continue
            button = _combobox_button(wrapper)
            if button is None or not button.get("id"):
                continue
            fact = _combo_value(request, "education.degree", index)
            if fact is not None:
                plans.append(FieldPlan(label="Degree", kind="combobox", control_id=str(button["id"]),
                                       value=fact[0], source=fact[1]))
    return plans


def _language_combobox_plans(request: Mapping[str, Any], wrappers: Sequence[Mapping[str, Any]]) -> list[FieldPlan]:
    """Language and Overall proficiency choices for each language row."""
    languages = _languages(request)
    plans: list[FieldPlan] = []
    for index, row in enumerate(_group_rows(wrappers, "language")):
        if index >= len(languages):
            break
        entry = languages[index]
        language = str(entry.get("language") or "")
        proficiency = str(entry.get("proficiency") or "")
        for wrapper in row:
            button = _combobox_button(wrapper)
            if button is None or not button.get("id"):
                continue
            label = str(wrapper.get("label") or "").strip()
            parsed = _ROW_PATTERN.match(str(button.get("id") or ""))
            field_name = parsed.group(3) if parsed else ""
            if field_name == "language" or label.casefold() == "language":
                plans.append(FieldPlan(label="Language", kind="combobox", control_id=str(button["id"]),
                                       value=language, source=f"resume.yaml:languages.{index}.language"))
            elif field_name == "overall" or "overall" in label.casefold():
                plans.append(FieldPlan(label=f"{language or 'Language'}: Overall proficiency", kind="combobox",
                                       control_id=str(button["id"]), value=proficiency,
                                       source=f"resume.yaml:languages.{index}.proficiency"))
    return plans


def _question_combobox_plan(
    request: Mapping[str, Any],
    wrapper: Mapping[str, Any],
    button: Mapping[str, Any],
) -> FieldPlan | None:
    """One drop-down question answered from the library, an EEO row, or the Profile."""
    label = str(wrapper.get("label") or "").strip()
    if not label:
        return None
    posting = request.get("posting") if isinstance(request.get("posting"), Mapping) else {}
    board = str(posting.get("board") or "")
    company = str(posting.get("company") or posting.get("employer") or "")
    category = reserved(label, kind="select")
    value = _reserved_answer(request, label, category, "select", [])
    source = category or "answer library"
    if value is None:
        row = match(_library_entries(request), text=label, board=board, company=company,
                    kind="select", options=None)
        if row is None:
            return None
        value, source = row["answer"], row["id"]
    literal = "Yes" if value is True else "No" if value is False else str(value)
    return FieldPlan(label=label, kind="combobox", control_id=str(button["id"]),
                     value=literal, source=source)


# ------------------------------------------------------------------- filling


_COMBOBOX_META_SCRIPT = r"""
(element) => {
  const field = element.closest('[data-automation-id^="formField-"]');
  const wrap = (value) => (value || "").replace(/\s+/g, " ").trim();
  return {
    tag: element.tagName.toLowerCase(),
    role: (element.getAttribute("role") || "").toLowerCase(),
    haspopup: (element.getAttribute("aria-haspopup") || "").toLowerCase(),
    id: element.id || null,
    automation_id: element.getAttribute("data-automation-id"),
    text: wrap(element.textContent),
    in_field: Boolean(field),
  };
}
"""

_OPTION_META_SCRIPT = r"""
(element) => ({
  role: (element.getAttribute("role") || "").toLowerCase(),
  id: element.id || null,
  automation_id: element.getAttribute("data-automation-id"),
  text: (element.textContent || "").replace(/\s+/g, " ").trim(),
  disabled: (element.getAttribute("aria-disabled") || "").toLowerCase() === "true",
})
"""


def _combobox_popup(frame: Frame, button: Any) -> Any | None:
    """The listbox this button controls, portaled or inline, once it is visible."""
    candidates: list[Any] = []
    for attribute in ("aria-controls", "aria-owns"):
        try:
            popup_id = button.get_attribute(attribute)
        except PlaywrightError:
            return None
        if popup_id:
            candidate = frame.locator(f'[id="{popup_id}"][role="listbox"]')
            if candidate.count() >= 1:
                candidates.append(candidate.first)
    container = button.locator(
        'xpath=ancestor::*[@data-automation-id][starts-with(@data-automation-id, "formField-")][1]'
    )
    inline = container.locator('[role="listbox"]')
    for index in range(inline.count()):
        candidates.append(inline.nth(index))
    for candidate in candidates:
        try:
            candidate.wait_for(state="visible", timeout=500)
            return candidate
        except PlaywrightError:
            continue
    return None


def _exact_option(popup: Any, expected: str) -> Any | None:
    """The one visible option whose whole text matches, disabled options aside."""
    options = popup.locator('[role="option"]')
    for index in range(options.count()):
        option = options.nth(index)
        metadata = option.evaluate(_OPTION_META_SCRIPT)
        if not isinstance(metadata, Mapping) or metadata.get("role") != "option" or metadata.get("disabled") is True:
            continue
        text = " ".join((metadata.get("text") or "").split())
        if not _matches(text, expected):
            continue
        if not option.is_visible():
            continue
        if _forbidden_click(str(metadata.get("id") or ""), str(metadata.get("automation_id") or ""), text):
            continue
        return option
    return None


def _apply_combobox(
    frame: Frame,
    step: StepResult,
    plan: FieldPlan,
    watch: _NetworkWatch,
    attempted: set[str] | None = None,
) -> str:
    """Open one field's own listbox, choose the exact option, or leave it.

    Returns ``"stop"`` when the selection caused a non-GET request, which means
    the employer's own save fired; Venator stops filling immediately.
    """
    if attempted is not None and plan.control_id in attempted:
        return "ok"
    before = watch.count
    try:
        button = _control(frame, plan.control_id)
        metadata = button.evaluate(_COMBOBOX_META_SCRIPT)
        if not isinstance(metadata, Mapping) or metadata.get("tag") != "button" or not (
            metadata.get("haspopup") == "listbox" or metadata.get("role") == "combobox"
        ):
            raise WorkdayError("the field's control is not a combobox")
        if metadata.get("in_field") is not True:
            raise WorkdayError("the combobox is not inside a form field")
        if _forbidden_click(str(metadata.get("id") or ""), str(metadata.get("automation_id") or ""),
                            str(metadata.get("text") or "")):
            raise WorkdayError("refusing to press a button that is not a drop-down")
        current = str(metadata.get("text") or "").strip()
        if _matches(current, str(plan.value)):
            return "ok"
        before_open = watch.count
        button.click()
        popup = _combobox_popup(frame, button)
        if watch.count != before_open:
            button.press("Escape")
            _mark_attempted(attempted, plan)
            step.stopped = True
            step.left.append({"label": plan.label, "reason": "opening the drop-down started an employer request",
                              "source": plan.source, "current": current, "expected": str(plan.value)})
            step.warnings.append(f"Opening {plan.label} caused an employer request; Venator stopped filling this step.")
            return "stop"
        if popup is None:
            button.press("Escape")
            _mark_attempted(attempted, plan)
            _record_left(step, plan, "its options did not open", current)
            step.warnings.append(f"{plan.label} did not open its drop-down; choose it yourself.")
            return "ok"
        option = _exact_option(popup, str(plan.value))
        if option is None:
            button.press("Escape")
            _mark_attempted(attempted, plan)
            _record_left(step, plan, "no option matched exactly", current)
            step.warnings.append(
                f"{plan.label} shows {current!r}; your value is {str(plan.value)!r} and no option matches exactly. "
                "Choose it yourself."
            )
            return "ok"
        before_pick = watch.count
        option.click()
        frame.page.wait_for_timeout(COMBOBOX_SETTLE_MS)
        if watch.count != before_pick:
            _mark_attempted(attempted, plan)
            step.stopped = True
            step.left.append({"label": plan.label, "reason": "the employer saved after this choice",
                              "source": plan.source, "current": current, "expected": str(plan.value)})
            step.warnings.append(
                f"Choosing {plan.label} caused an employer save request; Venator stopped filling this step."
            )
            return "stop"
        if not _matches(str(button.inner_text() or ""), str(plan.value)):
            raise WorkdayError("the chosen option did not remain in the field")
        step.filled += 1
        _record_written(step, plan, current, corrected=current.casefold() not in {"", "select one", "search"},
                        to=plan.value)
        return "ok"
    except (WorkdayError, PlaywrightError) as error:
        reason = str(error).strip() or "the drop-down could not be chosen"
        _mark_attempted(attempted, plan)
        _record_left(step, plan, reason)
        step.fields.append({"label": plan.label, "kind": plan.kind, "status": "skipped", "reason": reason})
        step.warnings.append(f"{plan.label} left for you: {reason}.")
        if watch.count != before:
            step.stopped = True
            step.warnings.append(f"Choosing {plan.label} caused an employer request; Venator stopped filling this step.")
            return "stop"
        return "ok"


def _mark_attempted(attempted: set[str] | None, plan: FieldPlan) -> None:
    if attempted is not None and plan.control_id:
        attempted.add(plan.control_id)


def _record_written(step: StepResult, plan: FieldPlan, current: str | None, *, corrected: bool,
                    to: str | bool) -> None:
    status = "corrected" if corrected else "filled"
    entry = {"label": plan.label, "kind": plan.kind, "status": status, "source": plan.source}
    if corrected:
        entry["from"] = current
        entry["to"] = to
        step.corrected.append(entry)
        step.warnings.append(f"{plan.label} corrected from {current!r} to {to!r}.")
    step.fields.append({"label": plan.label, "kind": plan.kind, "status": status, "reason": "confirmed Profile value"})


def _apply_plan(frame: Frame, step: StepResult, request: Mapping[str, Any], plan: FieldPlan) -> bool:
    """Write one plan. True means a control changed (a file upload included)."""
    try:
        if plan.kind == "report":
            reason = str(plan.reason or "needs a Workday press")
            _record_left(step, plan, reason)
            step.warnings.append(reason)
            return False
        if plan.kind == "file":
            if not _upload(frame, plan.control_id, Path(str(plan.value))):
                return False
            step.filled += 1
            step.fields.append({"label": plan.label, "kind": "file", "status": "filled",
                                "reason": "verified prepared resume uploaded"})
            return True
        if plan.kind in {"text", "textarea"}:
            locator = _control(frame, plan.control_id)
            current = locator.input_value()
            if current.strip() == str(plan.value).strip():
                return False
            _write_text(frame, plan.control_id, str(plan.value), plan.maxlength)
            step.filled += 1
            _record_written(step, plan, current, corrected=bool(current.strip()), to=plan.value)
            return True
        if plan.kind == "checkbox":
            locator = _control(frame, plan.control_id)
            current = locator.is_checked()
            if current is plan.value:
                return False
            _write_checked(frame, plan.control_id, bool(plan.value))
            step.filled += 1
            _record_written(step, plan, str(current), corrected=current is True, to=bool(plan.value))
            return True
        if plan.kind == "radio":
            chosen = _control(frame, plan.control_id)
            if chosen.is_checked():
                return False
            current_label = next(
                (label for option_id, label in zip(plan.option_ids, plan.options)
                 if _control(frame, option_id).is_checked()),
                "",
            )
            _write_checked(frame, plan.control_id, True)
            step.filled += 1
            _record_written(step, plan, current_label or None, corrected=bool(current_label), to=plan.value)
            return True
        if plan.kind == "spinbutton":
            locator = _control(frame, plan.control_id)
            current = str(locator.get_attribute("aria-valuenow") or "")
            if current.lstrip("0") == str(plan.value).lstrip("0"):
                return False
            _write_spinbutton(frame, plan.control_id, str(plan.value))
            step.filled += 1
            _record_written(step, plan, current, corrected=bool(current), to=str(plan.value))
            return True
        raise WorkdayError("no safe way to write this field")
    except (WorkdayError, PlaywrightError) as error:
        reason = str(error).strip() or "the control could not be written"
        step.left.append({"label": plan.label, "reason": reason, "source": plan.source})
        step.fields.append({"label": plan.label, "kind": plan.kind, "status": "skipped", "reason": reason})
        step.warnings.append(f"{plan.label} left for you: {reason}.")
        return False


def _file_plan(request: Mapping[str, Any], wrapper: Mapping[str, Any], step_name: str | None) -> FieldPlan | None:
    if step_name != AUTOFILL_STEP or wrapper.get("visible") is not True:
        return None
    resume_file = request.get("resume_file")
    if not isinstance(resume_file, str) or not Path(resume_file).is_file():
        return None
    for control in _result_fields(wrapper):
        if control.get("automation_id") == RESUME_FILE_AUTOMATION_ID or control.get("type") == "file":
            if control.get("id"):
                return FieldPlan(label="Autofill with Resume", kind="file", control_id=str(control["id"]),
                                 value=resume_file, source="verified prepared resume")
    return None


def fill_current_step(
    page: Page,
    request: Mapping[str, Any],
    *,
    resume_uploaded: bool = False,
    attempted: set[str] | None = None,
    previous_signature: str | None = None,
) -> dict[str, Any]:
    """Inventory the current wizard step, write what is confirmed, and stop.

    The returned mapping is safe to place in the private session state: it
    names changes and disagreements, never a click, and never a request.
    """
    posting = request.get("posting") if isinstance(request.get("posting"), Mapping) else {}
    step = StepResult(step=None, signature="no-document")
    frame = workday_frame(page, posting, allow_test_urls=bool(request.get("allow_test_urls")))
    if frame is None:
        step.warnings.append("No Workday application document is open for this Posting; nothing was filled.")
        return step.report()
    if sign_in_page(frame):
        step.warnings.append("This Workday page asks for a sign-in or verification; nothing was typed and nothing was captured.")
        return step.report()
    step.step = active_step(frame)
    wrappers = frame.evaluate(_INVENTORY_SCRIPT)
    wrappers = [wrapper for wrapper in wrappers if isinstance(wrapper, Mapping)] if isinstance(wrappers, list) else []
    step.signature = _step_signature(step.step, wrappers)
    if step.signature == previous_signature or not wrappers or step.step == REVIEW_STEP:
        return step.report()
    if _use_last_application(frame):
        step.use_last_application = True
        step.warnings.append("Use My Last Application is offered on this step; it is the preferred choice and only you can pick it.")

    _resume_attachment_note(frame, step, wrappers, resume_uploaded=resume_uploaded)

    plans: list[FieldPlan] = []
    for wrapper in wrappers:
        plan = _plan_simple(request, wrapper)
        if plan is not None:
            plans.append(plan)
        file_plan = _file_plan(request, wrapper, step.step)
        if file_plan is not None:
            plans.append(file_plan)
    plans.extend(_experience_plans(request, wrappers))
    plans.extend(_education_plans(request, wrappers))
    plans.extend(_language_plans(request, wrappers))
    for wrapper in wrappers:
        if _is_question(wrapper):
            plan = _question_plan(request, wrapper)
            if plan is not None:
                plans.append(plan)
    plans.extend(_combobox_plans(request, wrappers))

    watch = _NetworkWatch(page)
    try:
        for plan in plans[:MAX_REPORTED_FIELDS]:
            if plan.kind == "combobox":
                if _apply_combobox(frame, step, plan, watch, attempted) == "stop":
                    break
                continue
            if _apply_plan(frame, step, request, plan) and plan.kind == "file":
                step.resume_uploaded = True
    finally:
        watch.close()

    _remember_questions(request, wrappers, step)
    _blank_inventory(frame)
    step.warnings = list(dict.fromkeys(step.warnings))
    step.left = step.left[:MAX_REPORTED_FIELDS]
    step.fields = step.fields[:MAX_REPORTED_FIELDS]
    if step.resume_uploaded:
        step.warnings.append(
            "The verified prepared resume for this Posting is in Workday's Autofill field; "
            "press Continue to let Workday parse it."
        )
        step.warnings = list(dict.fromkeys(step.warnings))
    return step.report()


def _resume_attachment_note(
    frame: Frame,
    step: StepResult,
    wrappers: Sequence[Mapping[str, Any]],
    *,
    resume_uploaded: bool,
) -> None:
    """Tell the person when they need to remove an older résumé.

    On a repeat employer, Use My Last Application carries the previous file,
    and Workday shows a Delete Resume.pdf button where the file input was.
    Removing it needs a click on that control, which Venator never makes; once
    the person removes it the file input returns and the next pass uploads
    this Posting's tailored file.
    """
    if step.step != AUTOFILL_STEP or resume_uploaded:
        return
    try:
        removes = frame.locator('[data-automation-id="delete-file"]').count()
    except PlaywrightError:
        return
    if not removes:
        return
    empty_input = any(
        control.get("type") == "file" and not control.get("files")
        for wrapper in wrappers for control in _result_fields(wrapper)
    )
    if empty_input:
        return
    step.left.append({"label": "Autofill with Resume", "reason": "an older resume is attached",
                      "source": "Workday attachment"})
    step.warnings.append(
        "An earlier résumé is attached here. Venator never presses Delete: remove it yourself "
        "(Delete Resume.pdf), then this Posting's tailored file can be placed in the field."
    )


def _remember_questions(request: Mapping[str, Any], wrappers: Sequence[Mapping[str, Any]], step: StepResult) -> None:
    memory = request.get("form_memory")
    profile_id = request.get("profile_id")
    posting = request.get("posting") if isinstance(request.get("posting"), Mapping) else {}
    if not isinstance(memory, str) or not memory or not isinstance(profile_id, str) or not profile_id:
        return
    questions = []
    for wrapper in wrappers:
        if not _is_question(wrapper):
            continue
        controls = _result_fields(wrapper)
        button = wrapper.get("button") if isinstance(wrapper.get("button"), Mapping) else {}
        radios = [control for control in controls if control.get("type") == "radio"]
        if radios and all(control.get("name") == radios[0].get("name") for control in radios) and radios[0].get("name"):
            name, kind = str(radios[0]["name"]), "radio"
            options = [str(control.get("label") or control.get("value") or "") for control in radios]
            maxlength = None
        elif len(controls) == 1 and controls[0].get("name"):
            name = str(controls[0]["name"])
            kind = ("checkbox" if controls[0].get("type") == "checkbox" else
                    "select" if controls[0].get("tag") == "select" else
                    "textarea" if controls[0].get("tag") == "textarea" else "text")
            options = controls[0].get("options")
            maxlength = controls[0].get("maxlength") if isinstance(controls[0].get("maxlength"), int) else None
        elif button.get("id"):
            # A Workday drop-down question owns no named input; its button id is
            # the stable per-tenant identity for the next review sheet.
            name, kind, options, maxlength = str(button["id"]), "select", None, None
        else:
            continue
        questions.append({
            "name": name,
            "label": str(wrapper.get("label") or "").strip(),
            "kind": kind,
            "required": any(control.get("required") for control in controls),
            "options": list(options or []) if options else None,
            "maxlength": maxlength,
        })
    if not questions:
        return
    from venator.apply.form import remember_workday_form

    try:
        remember_workday_form(Path(memory), posting, questions, profile_id)
    except (OSError, ValueError):
        return


# ----------------------------------------------------------- applied receipt


def candidate_home_marker(
    frame: Frame,
    posting: Mapping[str, Any],
    *,
    allow_test_urls: bool = False,
) -> dict[str, Any] | None:
    """A receipt only from a Candidate Home row bound to title and requisition.

    The completion URL carries no job id and the modal is generic, so neither
    can record Applied. The row under ``taskListContainer`` names the job title
    and the requisition; both have to match this Posting.
    """
    parsed = urlparse(frame.url)
    if not (allow_test_urls and parsed.scheme == "file") and not workday_host_matches(frame.url, posting):
        return None
    key = str(posting.get("key") or "").split(":")
    if len(key) != 3 or key[0] != "workday":
        return None
    title = str(posting.get("title") or "").strip()
    if not title:
        return None
    requisition = key[2]
    try:
        rows = frame.locator(CANDIDATE_ROW_SELECTOR)
        for index in range(rows.count()):
            row = rows.nth(index)
            if not row.is_visible():
                continue
            title_node = row.locator(CANDIDATE_TITLE_SELECTOR)
            if title_node.count() != 1:
                continue
            row_title = " ".join(title_node.first.inner_text().split())
            if row_title != " ".join(title.split()):
                continue
            status = row.locator('[data-automation-id="applicationStatus"]')
            if status.count() != 1 or status.inner_text().strip().casefold() in {"", "draft", "not submitted", "in progress"}:
                continue
            if not re.search(r"(?<![\w.-])" + re.escape(requisition) + r"(?![\w.-])", row.inner_text()):
                continue
            return {
                "system": "workday",
                "board": key[1],
                "job_id": requisition,
                "host": parsed.hostname or "",
                "requisition": requisition,
                "title": row_title,
                "at": time.time(),
            }
    except PlaywrightError:
        return None
    return None


# ---------------------------------------------------------------- the session


class WorkdaySession:
    """Re-fills each new wizard step while the worker keeps the browser open."""

    def __init__(self, context: Any, page: Page, request: Mapping[str, Any], *,
                 resume_uploaded: bool = False, first_report: Mapping[str, Any] | None = None) -> None:
        self.context = context
        self.page = page
        self.request = request
        self.last_signature: str | None = None
        self.total_filled = 0
        self.steps: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.failed = False
        self.resume_uploaded = resume_uploaded
        #: Combobox buttons already tried and left alone, so a no-match is not
        #: re-opened on every tick.
        self.attempted: set[str] = set()
        if first_report is not None:
            self._record_report(first_report)

    def _record_report(self, report: Mapping[str, Any]) -> None:
        if report.get("resume_uploaded"):
            self.resume_uploaded = True
        self.last_signature = str(report.get("signature") or "")
        self.total_filled += int(report.get("filled") or 0)
        self.warnings = list(dict.fromkeys([*self.warnings, *[str(item) for item in report.get("warnings") or []]]))
        self.steps.append({
            "step": report.get("step"),
            "filled": report.get("filled"),
            "corrected": report.get("corrected"),
            "left": report.get("left"),
            "fields": report.get("fields"),
        })
        self.steps = self.steps[-12:]
        if report.get("stopped"):
            self.failed = True

    def tick(self) -> dict[str, Any] | None:
        if self.failed:
            return None
        try:
            report = fill_current_step(self.page, self.request, resume_uploaded=self.resume_uploaded,
                                       attempted=self.attempted, previous_signature=self.last_signature)
        except Exception as error:  # a gone page must not kill the worker
            self.failed = True
            self.warnings.append(f"Workday assistance stopped: {' '.join(str(error).split())[:300] or 'the page could not be read'}.")
            return None
        if str(report.get("signature") or "") == self.last_signature:
            return None
        self._record_report(report)
        return report
