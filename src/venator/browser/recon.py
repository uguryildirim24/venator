"""Inventory an application form without submitting it.

Usage:
    python -m venator.browser.recon <posting_key> [--out DIR]
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page, sync_playwright

from venator.browser._shared import (
    OUTPUT_ROOT,
    POSTINGS_DIR,
    artifact_dir,
    install_dry_run_guards,
    posting_url,
    write_json,
)

FORM_SPEC_NAME = "form-spec.json"
BLANK_SCREENSHOT_NAME = "blank.png"

_INVENTORY_SCRIPT = r"""
(form) => {
  const controls = Array.from(form.querySelectorAll("input, textarea, select"));
  const seenRadioNames = new Set();

  function textByIds(ids) {
    return (ids || "")
      .split(/\s+/)
      .map((id) => document.getElementById(id)?.textContent?.trim() || "")
      .filter(Boolean)
      .join(" ");
  }

  function cleanLabel(value) {
    return (value || "").replace(/\s+/g, " ").replace(/\s*\*+\s*$/, "").trim();
  }

  function explicitLabel(element) {
    if (element.id) {
      const label = document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
      if (label?.textContent?.trim()) return cleanLabel(label.textContent);
    }
    const wrapped = element.closest("label");
    if (wrapped?.textContent?.trim()) return cleanLabel(wrapped.textContent);
    return "";
  }

  function fieldLabel(element) {
    if ((element.getAttribute("type") || "").toLowerCase() === "file") {
      const group = element.closest('[role="group"][aria-labelledby]');
      const groupLabel = textByIds(group?.getAttribute("aria-labelledby"));
      if (groupLabel) return cleanLabel(groupLabel);
    }
    const aria = element.getAttribute("aria-label")?.trim();
    if (aria) return cleanLabel(aria);
    const labelled = textByIds(element.getAttribute("aria-labelledby"));
    if (labelled) return cleanLabel(labelled);
    const explicit = explicitLabel(element);
    if (explicit) return explicit;
    const fieldset = element.closest("fieldset");
    const legend = fieldset?.querySelector("legend")?.textContent?.trim();
    if (legend) return cleanLabel(legend);
    const container = element.closest(
      ".field, .application-question, .application--questions, [data-testid*='field']"
    );
    const nearby = container?.querySelector("label")?.textContent?.trim();
    return cleanLabel(nearby) || element.name || element.id || "Unlabelled field";
  }

  function radioGroupLabel(element) {
    const fieldset = element.closest("fieldset");
    const legend = fieldset?.querySelector("legend")?.textContent?.trim();
    return cleanLabel(legend) || fieldLabel(element);
  }

  function selectorFor(element) {
    if (element.id) return `#${CSS.escape(element.id)}`;
    if (element.name) {
      return `${element.tagName.toLowerCase()}[name="${CSS.escape(element.name)}"]`;
    }
    const path = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      if (current.id) {
        path.unshift(`#${CSS.escape(current.id)}`);
        break;
      }
      const siblings = Array.from(current.parentElement?.children || [])
        .filter((sibling) => sibling.tagName === current.tagName);
      const position = siblings.indexOf(current) + 1;
      path.unshift(`${current.tagName.toLowerCase()}:nth-of-type(${position})`);
      current = current.parentElement;
    }
    return path.join(" > ");
  }

  function radioLabel(element) {
    return explicitLabel(element) || element.value || "Unlabelled option";
  }

  function isRequired(element) {
    const group = element.closest('[role="group"][aria-required="true"]');
    return element.required || element.getAttribute("aria-required") === "true" || Boolean(group);
  }

  const fields = [];
  for (const element of controls) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute("type") || "text").toLowerCase();
    const role = (element.getAttribute("role") || "").toLowerCase();
    if (element.disabled || type === "hidden" || type === "submit" || type === "button" ||
        type === "image" || element.getAttribute("aria-hidden") === "true") continue;
    if (type !== "file" && element.getClientRects().length === 0) continue;

    let kind;
    if (tag === "textarea") kind = "textarea";
    else if (tag === "select" || role === "combobox") kind = "select";
    else if (type === "radio") kind = "radio";
    else if (type === "checkbox") kind = "checkbox";
    else if (type === "file") kind = "file";
    else kind = "text";

    if (kind === "radio") {
      const groupName = element.name || element.id;
      if (seenRadioNames.has(groupName)) continue;
      seenRadioNames.add(groupName);
      const group = controls.filter(
        (candidate) => candidate.type === "radio" && (candidate.name || candidate.id) === groupName
      );
      fields.push({
        label: radioGroupLabel(element),
        kind,
        required: group.some(isRequired),
        selector: element.name
          ? `input[name="${CSS.escape(element.name)}"]`
          : selectorFor(element),
        options: group.map(radioLabel),
      });
      continue;
    }

    let options = null;
    if (kind === "select") {
      options = tag === "select"
        ? Array.from(element.options)
            .filter((option) => !option.disabled && option.value !== "")
            .map((option) => option.textContent.trim())
        : [];
    }
    fields.push({
      label: fieldLabel(element),
      kind,
      required: isRequired(element),
      selector: selectorFor(element),
      options,
    });
  }
  return fields;
}
"""


def _form_score(form: Locator) -> int:
    controls = form.locator("input:not([type=hidden]), textarea, select").count()
    email = form.locator("input[type=email], input[autocomplete=email], #email").count()
    files = form.locator("input[type=file]").count()
    return controls + (email * 20) + (files * 5)


def locate_application_form(page: Page) -> tuple[Frame, Locator]:
    """Locate an embedded form, or safely follow an Apply anchor without clicking."""
    def best_form() -> tuple[Frame, Locator] | None:
        candidates = []
        for frame in page.frames:
            forms = frame.locator("form")
            for index in range(forms.count()):
                form = forms.nth(index)
                candidates.append((_form_score(form), frame, form))
        if not candidates:
            return None
        score, frame, form = max(candidates, key=lambda candidate: candidate[0])
        return (frame, form) if score > 0 else None

    located = best_form()
    if located:
        return located

    apply_link = page.locator("a[href]").filter(has_text=re.compile(r"\bapply\b", re.IGNORECASE))
    for index in range(apply_link.count()):
        href = apply_link.nth(index).get_attribute("href")
        if href and not href.lower().startswith("javascript:"):
            page.goto(urljoin(page.url, href), wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            located = best_form()
            if located:
                return located
    raise ValueError("application form not found without activating an Apply control")


def _combobox_options(frame: Frame, selector: str) -> list[str]:
    locator = frame.locator(selector).first
    if locator.count() == 0 or locator.get_attribute("role") != "combobox":
        return []
    locator.press("ArrowDown")
    frame.page.wait_for_timeout(100)
    options = [
        text.strip()
        for text in frame.locator('[role="option"]:visible').all_inner_texts()
    ]
    locator.press("Escape")
    return list(dict.fromkeys(option for option in options if option))


def inventory_form(page: Page) -> tuple[Locator, list[dict]]:
    frame, form = locate_application_form(page)
    fields = form.evaluate(_INVENTORY_SCRIPT)
    for field in fields:
        if field["kind"] == "select" and not field["options"]:
            field["options"] = _combobox_options(frame, field["selector"])
    return form, fields


def detect_captcha(page: Page) -> str:
    markup = "\n".join(frame.content().lower() for frame in page.frames)
    if "hcaptcha" in markup:
        return "hcaptcha"
    if "recaptcha" in markup or "g-recaptcha" in markup:
        return "recaptcha"
    if "captcha" in markup:
        return "unknown"
    return "none"


def recon_form(posting_key: str, apply_url: str, output_root: Path = OUTPUT_ROOT) -> dict:
    output_dir = artifact_dir(posting_key, output_root)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        install_dry_run_guards(context)
        page = context.new_page()
        try:
            page.goto(apply_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(750)
            form, fields = inventory_form(page)
            form.screenshot(path=output_dir / BLANK_SCREENSHOT_NAME)
            form_spec = {
                "posting_key": posting_key,
                "apply_url": page.url,
                "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "captcha": detect_captcha(page),
                "fields": fields,
            }
            write_json(output_dir / FORM_SPEC_NAME, form_spec)
            return form_spec
        finally:
            context.close()
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("posting_key")
    parser.add_argument("--out", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--postings-dir", type=Path, default=POSTINGS_DIR)
    args = parser.parse_args()
    try:
        form_spec = recon_form(
            args.posting_key,
            posting_url(args.posting_key, args.postings_dir),
            args.out,
        )
    except (OSError, PlaywrightError, ValueError) as error:
        parser.error(str(error))
    print(f"inventoried {len(form_spec['fields'])} fields for {args.posting_key}")


if __name__ == "__main__":
    main()
