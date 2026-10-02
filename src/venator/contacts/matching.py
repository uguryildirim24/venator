"""Posting-bound contacts and outreach, separate from application standing."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from venator.match.dedup import employer_display, normalize_employer

OUTREACH_EVENTS = frozenset({"outreach", "outreach_undo"})


def matches(posting: Mapping, contacts: Iterable[dict], names: Mapping[str, str]) -> list[dict]:
    employer = normalize_employer(employer_display(posting, names))
    return [row for row in contacts
            if (row["board"].strip() == posting.get("board") if row["board"].strip()
                else bool(employer) and normalize_employer(row["company"]) == employer)]


def contact_link(route: str) -> str | None:
    route = route.strip()
    email = route.removeprefix("mailto:")
    if re.fullmatch(r"[^\s@<>?]+@[^\s@<>?]+\.[^\s@<>?]+", email):
        return f"mailto:{email}"
    parsed = urlsplit(route)
    if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
        return route
    return None


def outreach_detail(contact: Mapping) -> str:
    return json.dumps({"contact_id": contact["id"], "person": contact["person"]}, ensure_ascii=False)


def reached_out(events: Iterable[dict], posting_key: str) -> dict[str, str | None]:
    result = {}
    for event in events:
        if event["posting_key"] != posting_key or event["event"] not in OUTREACH_EVENTS:
            continue
        try:
            detail = json.loads(event["detail"])
        except (ValueError, TypeError):
            continue
        if isinstance(detail, dict) and isinstance(detail.get("contact_id"), str):
            result[detail["contact_id"]] = event["at"] if event["event"] == "outreach" else None
    return result
