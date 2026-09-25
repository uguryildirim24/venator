"""Build a truthful FillPlan for dry-run form filling.

Usage:
    python -m venator.fill.plan <posting_key> [--profile NAME] [--form-spec FILE] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping

import yaml

from venator.fill.mapping import DEFAULT_SOURCES, FactSources, map_field
from venator.match.store import load_postings
from venator.profile import (
    Profile,
    ProfileError,
    add_profile_argument,
    announce_profile,
    profile_from_arguments,
    resolve_profile,
)

POSTINGS_DIR = Path("data/postings")
APPLICATIONS_DIR = Path("data/applications")
OUT_DIR = Path("build/fill")
FIELD_KINDS = frozenset({"text", "textarea", "select", "radio", "checkbox", "file"})
CAPTCHA_KINDS = frozenset({"none", "hcaptcha", "recaptcha", "unknown"})

STANDARD_FIELDS = (
    {"label": "First Name", "kind": "text", "required": True, "selector": "#first_name", "options": None},
    {"label": "Last Name", "kind": "text", "required": True, "selector": "#last_name", "options": None},
    {"label": "Email", "kind": "text", "required": True, "selector": "#email", "options": None},
    {"label": "Phone", "kind": "text", "required": False, "selector": "#phone", "options": None},
    {"label": "Location", "kind": "text", "required": False, "selector": "#location", "options": None},
    {"label": "Resume/CV", "kind": "file", "required": True, "selector": "#resume", "options": None},
    {"label": "LinkedIn Profile", "kind": "text", "required": False, "selector": "#linkedin", "options": None},
    {"label": "Website", "kind": "text", "required": False, "selector": "#website", "options": None},
    {"label": "School", "kind": "text", "required": False, "selector": "#school", "options": None},
    {"label": "Degree", "kind": "text", "required": False, "selector": "#degree", "options": None},
    {
        "label": "Are you legally authorized to work in the United States?",
        "kind": "radio",
        "required": True,
        "selector": "[name=work_authorized]",
        "options": ["Yes", "No"],
    },
    {
        "label": "Will you now or in the future require sponsorship for employment visa status?",
        "kind": "radio",
        "required": True,
        "selector": "[name=sponsorship]",
        "options": ["Yes", "No"],
    },
)


def sanitize_posting_key(posting_key: str) -> str:
    return posting_key.replace(":", "-")


def _load_mapping(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return value


def validate_form_spec(value: object, posting_key: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("FormSpec must be a JSON object")
    required_keys = {"posting_key", "apply_url", "captured_at", "captcha", "fields"}
    missing = sorted(required_keys - set(value), key=str)
    if missing:
        raise ValueError(f"FormSpec is missing fields: {missing}")
    if value["posting_key"] != posting_key:
        raise ValueError(
            f"FormSpec posting_key {value['posting_key']!r} does not match {posting_key!r}"
        )
    if not isinstance(value["apply_url"], str) or not value["apply_url"].strip():
        raise ValueError("FormSpec.apply_url must be a non-empty string")
    if not isinstance(value["captured_at"], str) or not value["captured_at"].strip():
        raise ValueError("FormSpec.captured_at must be a non-empty string")
    if value["captcha"] not in CAPTCHA_KINDS:
        raise ValueError(f"FormSpec.captcha must be one of {sorted(CAPTCHA_KINDS)}")
    if not isinstance(value["fields"], list):
        raise ValueError("FormSpec.fields must be a list")

    for index, field in enumerate(value["fields"]):
        label = f"FormSpec.fields[{index}]"
        if not isinstance(field, dict):
            raise ValueError(f"{label} must be an object")
        for name in ("label", "kind", "required", "selector", "options"):
            if name not in field:
                raise ValueError(f"{label} is missing {name!r}")
        if not isinstance(field["label"], str) or not field["label"].strip():
            raise ValueError(f"{label}.label must be a non-empty string")
        if field["kind"] not in FIELD_KINDS:
            raise ValueError(f"{label}.kind must be one of {sorted(FIELD_KINDS)}")
        if not isinstance(field["required"], bool):
            raise ValueError(f"{label}.required must be boolean")
        if not isinstance(field["selector"], str) or not field["selector"].strip():
            raise ValueError(f"{label}.selector must be a non-empty string")
        options = field["options"]
        if field["kind"] in {"select", "radio"}:
            if (
                not isinstance(options, list)
                or not options
                or any(not isinstance(option, str) or not option for option in options)
            ):
                raise ValueError(f"{label}.options must be a non-empty list of strings")
        elif options is not None:
            raise ValueError(f"{label}.options must be null for kind {field['kind']!r}")
    return value


def load_form_spec(path: Path, posting_key: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid FormSpec JSON: {error.msg}") from error
    return validate_form_spec(value, posting_key)


def standard_form_spec(posting_key: str, apply_url: str) -> dict:
    return {
        "posting_key": posting_key,
        "apply_url": apply_url,
        "captured_at": "standard-fallback",
        "captcha": "unknown",
        "fields": [dict(field) for field in STANDARD_FIELDS],
    }


def create_fill_plan(
    posting_key: str,
    apply_url: str,
    form_spec: Mapping[str, object],
    resume: Mapping[str, object],
    constraints: Mapping[str, object],
    *,
    resume_file: Path | None = None,
    sources: FactSources = DEFAULT_SOURCES,
) -> dict:
    fields = form_spec.get("fields")
    if not isinstance(fields, list):
        raise ValueError("FormSpec.fields must be a list")
    mapped_fields = []
    unmapped_fields = []
    for field in fields:
        if not isinstance(field, Mapping):
            raise ValueError("FormSpec fields must be objects")
        mapping = map_field(field, resume, constraints, resume_file=resume_file, sources=sources)
        if mapping.mapped:
            mapped_fields.append(
                {
                    "label": field["label"],
                    "kind": field["kind"],
                    "value": mapping.value,
                    "source": mapping.source,
                }
            )
        else:
            unmapped_fields.append(
                {
                    "label": field["label"],
                    "reason": mapping.reason,
                }
            )
    return {
        "posting_key": posting_key,
        "apply_url": apply_url,
        "never_submit": True,
        "fields": mapped_fields,
        "unmapped": unmapped_fields,
    }


def plan_posting(
    posting_key: str,
    *,
    form_spec_path: Path | None = None,
    out_dir: Path = OUT_DIR,
    postings_dir: Path = POSTINGS_DIR,
    resume_path: Path | None = None,
    constraints_path: Path | None = None,
    applications_dir: Path = APPLICATIONS_DIR,
    profile: Profile | None = None,
) -> tuple[dict, Path]:
    posting = next(
        (posting for posting in load_postings(postings_dir) if posting["key"] == posting_key),
        None,
    )
    if posting is None:
        raise ValueError(f"unknown posting_key: {posting_key!r}")
    apply_url = posting.get("url")
    if not isinstance(apply_url, str) or not apply_url.strip():
        raise ValueError(f"Posting {posting_key!r} has no apply URL")

    if resume_path is None or constraints_path is None:
        if profile is None:
            profile = resolve_profile()
        resume_path = resume_path or profile.resume_path
        constraints_path = constraints_path or profile.constraints_path
    resume = _load_mapping(resume_path)
    constraints = _load_mapping(constraints_path)
    sources = FactSources(resume_path.as_posix(), constraints_path.as_posix())
    form_spec = (
        load_form_spec(form_spec_path, posting_key)
        if form_spec_path is not None
        else standard_form_spec(posting_key, apply_url)
    )
    sanitized_key = sanitize_posting_key(posting_key)
    resume_file = applications_dir / sanitized_key / "resume.pdf"
    plan = create_fill_plan(
        posting_key,
        apply_url,
        form_spec,
        resume,
        constraints,
        resume_file=resume_file,
        sources=sources,
    )
    plan_dir = out_dir / sanitized_key
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / "plan.json"
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return plan, plan_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("posting_key")
    parser.add_argument("--form-spec", type=Path)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--postings-dir", type=Path, default=POSTINGS_DIR)
    add_profile_argument(parser)
    args = parser.parse_args()
    try:
        profile = profile_from_arguments(args)
        announce_profile(profile)
        plan, _ = plan_posting(
            args.posting_key,
            form_spec_path=args.form_spec,
            out_dir=args.out,
            postings_dir=args.postings_dir,
            profile=profile,
        )
    except (OSError, ProfileError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    print(
        f"planned {args.posting_key}: "
        f"{len(plan['fields'])} mapped, {len(plan['unmapped'])} unmapped"
    )


if __name__ == "__main__":
    main()
