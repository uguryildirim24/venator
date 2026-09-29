"""Read Greenhouse Posting questions without opening or submitting an application."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any


def greenhouse_questions(posting: Mapping[str, Any], *, fetch: Callable[[str], bytes] | None = None) -> list[dict]:
    parts = str(posting.get("key") or "").split(":")
    if len(parts) != 3 or parts[0] != "greenhouse" or not parts[1].replace("-", "").isalnum() or not parts[2].isdigit():
        raise ValueError("This Posting has no Greenhouse board and job ID.")
    url = f"https://boards-api.greenhouse.io/v1/boards/{parts[1]}/jobs/{parts[2]}?questions=true"
    if fetch is None:
        def fetch(address: str) -> bytes:
            with urllib.request.urlopen(address, timeout=12) as response:
                return response.read(2_000_001)
    data = json.loads(fetch(url))
    if not isinstance(data, dict) or str(data.get("id")) != parts[2]:
        raise ValueError("The Greenhouse question response belongs to another Posting.")
    questions = data.get("questions")
    if not isinstance(questions, list):
        raise ValueError("The Greenhouse response contains no question list.")
    result = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        fields = question.get("fields")
        if not isinstance(fields, list):
            continue
        for field in fields:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                continue
            options = field.get("values")
            field_type = str(field.get("type") or "input_text")
            kind = {"input_text": "text", "input_file": "file", "textarea": "textarea",
                    "multi_value_single_select": "select", "multi_value_multi_select": "select"}.get(field_type, field_type)
            result.append({"name": field["name"], "label": str(question.get("label") or ""),
                           "kind": kind, "required": question.get("required") is True,
                           "options": [str(option.get("label")) for option in options if isinstance(option, dict)]
                           if isinstance(options, list) else None,
                           "maxlength": field.get("maxlength") if isinstance(field.get("maxlength"), int) else None})
    names = [row["name"] for row in result]
    if len(names) != len(set(names)):
        raise ValueError("The Greenhouse question response contains duplicate field names.")
    return result


def review_questions(posting: Mapping[str, Any], profile: Any, directory: Any,
                     questions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Bucket every API question; only literal options can be reused."""
    from venator.answers.store import latest, match, row_hash
    from venator.apply.classify import reserved
    from venator.fill.mapping import map_field

    entries = latest(directory, profile.identifier)
    company = str(posting.get("company") or posting.get("employer") or "")
    board = str(posting.get("board") or "")
    reviewed, pins = [], []
    for question in questions:
        kind = question["kind"]
        label = question["label"]
        category = reserved(label, kind=kind)
        options = question.get("options")
        answer = None
        source = None
        bucket = "reserved" if category is not None else "you"
        if kind in {"select", "radio"} and not options:
            reviewed.append({**question, "bucket": bucket, "category": category,
                             "answer": None, "source": None})
            continue
        if category in {"consent", "authorization_sponsorship", "sign_in"}:
            bucket = "reserved"
        elif category == "file":
            bucket = "reserved"
        elif category in {"sponsorship", "authorization"}:
            value = map_field(question, profile.resume, profile.constraints)
            bucket = "reserved"
            if value.mapped:
                answer, source = value.value, value.source
        else:
            if category is None:
                value = map_field(question, profile.resume, profile.constraints)
                if value.mapped:
                    answer, source, bucket = value.value, value.source, "profile"
            if answer is None:
                row = match(entries, text=label, board=board, company=company, kind=kind, options=options)
                if row is not None:
                    answer = row["answer"]
                    source, bucket = row["id"], "library"
                    pins.append({"id": row["id"], "question": row["question"],
                                 "scope": row["scope"], "hash": row_hash(row)})
            if answer is None and category is None and kind == "textarea":
                bucket = "essay"
        if isinstance(answer, bool):
            answer = "Yes" if answer else "No"
        reviewed.append({**question, "bucket": bucket, "category": category, "answer": answer, "source": source})
    return reviewed, pins
