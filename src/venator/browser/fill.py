"""Execute a FillPlan in a guarded browser without submitting it.

Usage:
    python -m venator.browser.fill <posting_key> --plan FILE [--out DIR]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page, sync_playwright

from venator.browser._shared import (
    OUTPUT_ROOT,
    POSTINGS_DIR,
    artifact_dir,
    install_dry_run_guards,
    posting_url,
    submit_event_count,
    write_json,
)
from venator.browser.recon import detect_captcha, inventory_form

FILLED_SCREENSHOT_NAME = "filled.png"
FILL_REPORT_NAME = "fill-report.json"
SUPPORTED_KINDS = frozenset({"text", "textarea", "select", "radio", "checkbox", "file"})
FORBIDDEN_INPUT_TYPES = frozenset({"submit", "button", "image", "file"})


class SkipField(ValueError):
    """A safely unfilled field that cannot satisfy the exact-value contract."""


def load_plan(plan_path: Path) -> dict:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid FillPlan JSON: {error.msg}") from error
    if not isinstance(plan, dict):
        raise ValueError("FillPlan must be a JSON object")
    if plan.get("never_submit") is not True:
        raise ValueError("FillPlan.never_submit must be literal true")
    if not isinstance(plan.get("posting_key"), str) or not plan["posting_key"]:
        raise ValueError("FillPlan.posting_key must be a non-empty string")
    if not isinstance(plan.get("apply_url"), str) or not plan["apply_url"]:
        raise ValueError("FillPlan.apply_url must be a non-empty string")
    if not isinstance(plan.get("fields"), list):
        raise ValueError("FillPlan.fields must be a list")
    if not isinstance(plan.get("unmapped", []), list):
        raise ValueError("FillPlan.unmapped must be a list")
    return plan


def _find_locator(page: Page, selector: str | None, label: str) -> tuple[Frame, Locator]:
    for frame in page.frames:
        if selector:
            located = frame.locator(selector)
            if located.count():
                return frame, located
        labelled = frame.get_by_label(label, exact=True)
        if labelled.count():
            return frame, labelled
    raise ValueError("field not found by selector or exact label")


def _assert_safe_target(locator: Locator, kind: str) -> None:
    metadata = locator.first.evaluate(
        """element => ({
          tag: element.tagName.toLowerCase(),
          type: (element.getAttribute("type") || "").toLowerCase(),
          role: (element.getAttribute("role") || "").toLowerCase()
        })"""
    )
    tag = metadata["tag"]
    input_type = metadata["type"]
    role = metadata["role"]
    if tag == "button" or input_type in FORBIDDEN_INPUT_TYPES:
        raise ValueError("selector resolved to an unsafe non-fill control")
    valid = {
        "text": tag == "input" and input_type not in {"radio", "checkbox"},
        "textarea": tag == "textarea",
        "select": tag == "select" or role == "combobox",
        "radio": tag == "input" and input_type == "radio",
        "checkbox": tag == "input" and input_type == "checkbox",
    }
    if not valid.get(kind, False):
        raise ValueError(f"selector target does not match plan kind {kind!r}")


def _assert_safe_option(locator: Locator) -> None:
    metadata = locator.first.evaluate(
        """element => ({
          tag: element.tagName.toLowerCase(),
          type: (element.getAttribute("type") || "").toLowerCase()
        })"""
    )
    if metadata["tag"] == "button" and metadata["type"] != "button":
        raise ValueError("combobox option resolved to an unsafe submit control")
    if metadata["type"] in {"submit", "image"}:
        raise ValueError("combobox option resolved to an unsafe submit control")


def _radio_option(frame: Frame, value: str) -> Locator:
    option = frame.get_by_label(value, exact=True)
    if option.count() == 0:
        raise ValueError("exact radio option not found")
    return option


def _fill_control(
    frame: Frame,
    locator: Locator,
    field: dict,
    spec_field: dict | None,
) -> None:
    kind = field["kind"]
    value = field.get("value")
    _assert_safe_target(locator, kind)
    if kind in {"text", "textarea"}:
        locator.first.fill(str(value))
        if locator.first.input_value() != str(value):
            raise ValueError("entered value did not remain in the field")
        return
    if kind == "checkbox":
        if not isinstance(value, bool):
            raise ValueError("checkbox value must be boolean")
        if value:
            locator.first.check()
        else:
            locator.first.uncheck()
        if locator.first.is_checked() is not value:
            raise ValueError("checkbox state did not match the plan")
        return

    options = (spec_field or {}).get("options") or []
    if str(value) not in options:
        raise SkipField("value is not an exact FormSpec option")
    if kind == "radio":
        option = _radio_option(frame, str(value))
        _assert_safe_target(option, "radio")
        option.check()
        if not option.is_checked():
            raise ValueError("radio option did not remain selected")
        return
    if kind == "select":
        tag = locator.first.evaluate("element => element.tagName.toLowerCase()")
        if tag == "select":
            locator.first.select_option(label=str(value))
        else:
            locator.first.fill(str(value))
            option = frame.get_by_role("option", name=str(value), exact=True)
            option.first.wait_for(state="visible", timeout=2_000)
            if option.count() != 1:
                raise ValueError("exact combobox option not found")
            _assert_safe_option(option)
            option.click()
        return
    raise ValueError(f"unsupported field kind: {kind!r}")


def _result(field: object, status: str, reason: str, *, origin: str = "plan") -> dict:
    if not isinstance(field, dict):
        return {
            "label": "<invalid plan field>",
            "kind": None,
            "source": None,
            "status": status,
            "reason": reason,
            "origin": origin,
        }
    return {
        "label": field.get("label"),
        "kind": field.get("kind"),
        "source": field.get("source"),
        "status": status,
        "reason": reason,
        "origin": origin,
    }


def execute_fill(
    posting_key: str,
    plan_path: Path,
    output_root: Path = OUTPUT_ROOT,
) -> dict:
    plan = load_plan(plan_path)
    if plan["posting_key"] != posting_key:
        raise ValueError("FillPlan.posting_key does not match the requested Posting")
    output_dir = artifact_dir(posting_key, output_root)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        blocked_requests, _ = install_dry_run_guards(context)
        page = context.new_page()
        try:
            page.goto(plan["apply_url"], wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(500)
            form, form_fields = inventory_form(page)
            spec_by_label = {
                field["label"]: field for field in form_fields if isinstance(field.get("label"), str)
            }
            results = []
            for index, field in enumerate(plan["fields"]):
                if not isinstance(field, dict):
                    results.append(_result(field, "failed", f"fields[{index}] must be an object"))
                    continue
                label = field.get("label")
                kind = field.get("kind")
                if not isinstance(label, str) or not label:
                    results.append(_result(field, "failed", "label must be a non-empty string"))
                    continue
                if kind not in SUPPORTED_KINDS:
                    results.append(_result(field, "failed", "unsupported field kind"))
                    continue
                if kind == "file":
                    results.append(_result(field, "skipped", "file staging deferred"))
                    continue
                spec_field = spec_by_label.get(label)
                selector = field.get("selector") or (spec_field or {}).get("selector")
                try:
                    frame, locator = _find_locator(page, selector, label)
                    _fill_control(frame, locator, field, spec_field)
                except SkipField as error:
                    results.append(_result(field, "skipped", str(error)))
                except Exception as error:
                    results.append(_result(field, "failed", str(error)))
                else:
                    results.append(_result(field, "filled", "dry-run value entered"))

            for index, unmapped in enumerate(plan.get("unmapped", [])):
                reason = (
                    unmapped.get("reason", "unmapped by FillPlan")
                    if isinstance(unmapped, dict)
                    else f"unmapped[{index}] must be an object"
                )
                results.append(_result(unmapped, "skipped", str(reason), origin="unmapped"))

            form.screenshot(path=output_dir / FILLED_SCREENSHOT_NAME)
            report = {
                "posting_key": posting_key,
                "apply_url": page.url,
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "never_submit": True,
                "captcha": detect_captcha(page),
                "submit_events": submit_event_count(page),
                "blocked_state_changing_requests": len(blocked_requests),
                "fields": results,
            }
            write_json(output_dir / FILL_REPORT_NAME, report)
            return report
        finally:
            context.close()
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("posting_key")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--postings-dir", type=Path, default=POSTINGS_DIR)
    args = parser.parse_args()
    try:
        posting_url(args.posting_key, args.postings_dir)
        report = execute_fill(args.posting_key, args.plan, args.out)
    except (OSError, PlaywrightError, ValueError) as error:
        parser.error(str(error))
    filled = sum(field["status"] == "filled" for field in report["fields"])
    print(f"dry-run filled {filled} fields for {args.posting_key}; nothing submitted")


if __name__ == "__main__":
    main()
