"""Per-Profile append-only answer and statement library."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from venator.apply.classify import reserved
from venator.fill.mapping import normalize_label
from venator.profile.claim import claim_store, store_owner

KINDS = frozenset({"yes_no", "select", "text", "textarea", "statement"})
_EMPLOYER_DEPENDENT = re.compile(r"\b(worked for|employed by|know anyone at|relatives at|interested in|previously worked at)\b")


def question_key(text: str, company: str = "") -> str:
    """Replace a company's full name and first word before punctuation folding."""
    normalized = normalize_label(text)
    name = normalize_label(company)
    for candidate in sorted({name, name.split(" ")[0] if name else ""}, key=len, reverse=True):
        if candidate:
            normalized = re.sub(r"(?<!\w)" + re.escape(candidate) + r"(?!\w)", " companyplaceholder ", normalized)
    return normalized.replace("companyplaceholder", "{company}").strip()


def employer_dependent(question: str) -> bool:
    return "{company}" in question or bool(_EMPLOYER_DEPENDENT.search(question))


def row_hash(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def rows(directory: Path, profile_id: str) -> list[dict[str, Any]]:
    owner = store_owner(directory)
    if owner is not None and owner != profile_id:
        raise ValueError("The answer library belongs to another Profile.")
    result = []
    for path in sorted(directory.glob("????-??-??.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("profile_id") != profile_id:
                raise ValueError("An answer belongs to another Profile or is invalid.")
            result.append(value)
    return result


def latest(directory: Path, profile_id: str) -> dict[tuple[str, str], dict]:
    return {(row["question"], row["scope"]): row for row in rows(directory, profile_id)}


def append(directory: Path, profile_id: str, *, text: str, kind: str, answer: str | bool | None,
           board: str, company: str = "", universal: bool = False, eeo: bool = False) -> dict:
    if kind not in KINDS or not isinstance(text, str) or not text.strip():
        raise ValueError("Choose a question and its answer kind.")
    if answer is not None and not isinstance(answer, (str, bool)):
        raise ValueError("The answer must be text, yes/no, or a retraction.")
    if isinstance(answer, str) and (len(answer) > 1800 or "\n" in answer or "\r" in answer):
        raise ValueError("An answer must be one passage of at most 1800 characters.")
    if kind == "statement" and (not isinstance(answer, str) or not answer.strip()):
        raise ValueError("A statement must be a passage written by the Owner.")
    if kind != "statement":
        category = reserved(text)
        if category is not None and (category != "eeo" or not eeo):
            raise ValueError("This question cannot be stored in the answer library.")
        if eeo and category != "eeo":
            raise ValueError("An EEO answer must be explicitly identified as EEO.")
    key = question_key(text, company)
    if universal and kind != "statement" and employer_dependent(key):
        raise ValueError("An employer-specific answer must stay with that employer.")
    scope = "any" if universal else board
    if not scope or not isinstance(scope, str):
        raise ValueError("Choose an employer for this answer.")
    rows(directory, profile_id)
    claim_store(directory, profile_id)
    now = datetime.now(timezone.utc)
    row = {"id": uuid.uuid4().hex, "question": key, "text": text.strip(), "kind": kind,
           "answer": answer, "scope": scope, "at": now.isoformat(), "profile_id": profile_id}
    if eeo:
        row["eeo"] = True
    with (directory / f"{now.date().isoformat()}.jsonl").open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def replace(directory: Path, profile_id: str, old: dict, answer: str | bool | None) -> dict:
    if latest(directory, profile_id).get((old["question"], old["scope"])) != old:
        raise ValueError("This answer changed. Reload the Answers view.")
    if answer is not None and (not isinstance(answer, (str, bool)) or isinstance(answer, str)
                               and (not answer.strip() or len(answer) > 1800 or "\n" in answer or "\r" in answer)):
        raise ValueError("An answer must be one passage of at most 1800 characters.")
    if old.get("kind") == "statement" and answer is not None and not isinstance(answer, str):
        raise ValueError("A statement must remain a passage written by the Owner.")
    now = datetime.now(timezone.utc)
    row = {**old, "id": uuid.uuid4().hex, "answer": answer, "at": now.isoformat()}
    with (directory / f"{now.date().isoformat()}.jsonl").open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def retract(directory: Path, profile_id: str, old: dict) -> dict:
    return replace(directory, profile_id, old, None)


def promote(directory: Path, profile_id: str, old: dict) -> dict:
    if latest(directory, profile_id).get((old["question"], old["scope"])) != old:
        raise ValueError("This answer changed. Reload the Answers view.")
    if old.get("answer") is None or employer_dependent(old["question"]):
        raise ValueError("An employer-specific answer must stay with that employer.")
    now = datetime.now(timezone.utc)
    row = {**old, "id": uuid.uuid4().hex, "scope": "any", "at": now.isoformat()}
    with (directory / f"{now.date().isoformat()}.jsonl").open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(row, ensure_ascii=False) + "\n")
    replace(directory, profile_id, old, None)
    return row


def match(entries: dict[tuple[str, str], dict], *, text: str, board: str, company: str,
          kind: str, options: list[str] | None = None) -> dict | None:
    category = reserved(text, kind=kind)
    if category is not None and category != "eeo":
        return None
    key = question_key(text, company)
    for scope in (board, "any"):
        row = entries.get((key, scope))
        if not row or row.get("answer") is None or row.get("kind") == "statement":
            continue
        if category == "eeo" and row.get("eeo") is not True:
            continue
        if category is None and row.get("eeo") is True:
            continue
        if scope == "any" and employer_dependent(key):
            continue
        answer = row["answer"]
        literal = "Yes" if answer is True else "No" if answer is False else answer
        if options is not None and literal not in options:
            return None
        return row
    return None


def pinned_current(directory: Path, profile_id: str, pins: list[dict]) -> bool:
    current = latest(directory, profile_id)
    for pin in pins:
        row = current.get((pin.get("question"), pin.get("scope")))
        if row is None or row.get("id") != pin.get("id") or row_hash(row) != pin.get("hash"):
            return False
    return True
