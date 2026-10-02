"""Per-Profile append-only contacts, separate from application and training facts."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from venator.profile.claim import claim_store, store_owner

CONTACT_FIELDS = ("company", "person", "title", "conversation_angle", "contact_route", "sources", "board")


def rows(directory: Path, profile_id: str) -> list[dict[str, Any]]:
    """Read every contact without claiming or changing the store."""
    owner = store_owner(directory)
    if owner is not None and owner != profile_id:
        raise ValueError("The contacts library belongs to another Profile.")
    result = []
    for path in sorted(directory.glob("????-??-??.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("profile_id") != profile_id:
                raise ValueError("A contact belongs to another Profile or is invalid.")
            result.append(value)
    return result


def _contact(value: Mapping[str, str]) -> dict[str, str]:
    contact = {field: value.get(field, "") for field in CONTACT_FIELDS}
    if any(not isinstance(text, str) for text in contact.values()):
        raise ValueError("Contact fields must be text.")
    if not contact["company"].strip() or not contact["person"].strip():
        raise ValueError("A contact needs a company and a person.")
    return contact


def _identity(contact: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(contact.get(field, "") for field in CONTACT_FIELDS)


def append_contacts(directory: Path, profile_id: str, contacts: Iterable[Mapping[str, str]]) -> dict[str, int]:
    """Append distinct rows; exact duplicates include all fields, not just company."""
    existing = {_identity(row) for row in rows(directory, profile_id)}
    values = [_contact(value) for value in contacts]
    fresh = []
    for contact in values:
        identity = _identity(contact)
        if identity not in existing:
            existing.add(identity)
            fresh.append(contact)
    if fresh:
        claim_store(directory, profile_id)
        now = datetime.now(timezone.utc)
        with (directory / f"{now.date().isoformat()}.jsonl").open("a", encoding="utf-8", newline="\n") as output:
            for contact in fresh:
                row = {**contact, "id": uuid.uuid4().hex, "at": now.isoformat(), "profile_id": profile_id}
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"rows": len(values), "imported": len(fresh), "duplicates": len(values) - len(fresh)}
