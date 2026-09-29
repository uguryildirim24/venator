"""Check a derived dashboard before it replaces the last usable view."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence

from venator.discover.store import posting_revision


def verify_dashboard(
    database: sqlite3.Connection,
    postings: Sequence[dict],
    latest_decisions: Mapping[str, dict],
    filters_version: str | None,
) -> None:
    """Validate completeness and recommendation prerequisites, including cache hits.

    This checks the published data contract, not semantic matching quality.
    Ordinary skill gaps may remain visible on a potential match.
    """
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(f"Dashboard validation failed: {message}")

    source = {posting["key"]: posting for posting in postings}
    view_keys = {row[0] for row in database.execute("SELECT key FROM postings")}
    cursor = database.execute(
        "SELECT posting_key, status, evidence, conflicts, unknowns, listing_status, "
        "description_kind, last_verified_at, assessed_by FROM assessments"
    )
    rows = list(cursor)
    require(view_keys == set(source), "postings do not match the current source store")
    require({row[0] for row in rows} == view_keys, "each posting needs an assessment")
    for key, status, raw_evidence, raw_conflicts, raw_unknowns, listing, kind, verified, assessed_by in rows:
        require(status in {"suitable", "not_suitable", "needs_review", "unassessed"}, f"{key}: invalid assessment status")
        require(assessed_by == "deterministic", f"{key}: invalid assessment source")
        try:
            evidence, conflicts, unknowns = map(json.loads, (raw_evidence, raw_conflicts, raw_unknowns))
        except (ValueError, TypeError) as error:
            raise ValueError(f"Dashboard validation failed: {key}: unreadable assessment evidence") from error
        require(all(isinstance(value, list) for value in (evidence, conflicts, unknowns)), f"{key}: assessment evidence must be lists")
        if status != "suitable":
            continue
        posting = source[key]
        decision = latest_decisions.get(key, {})
        require(
            filters_version is not None
            and decision.get("filters_version") == filters_version
            and decision.get("posting_version") == posting_revision(posting)
            and decision.get("verdict") == "pass"
            and isinstance(decision.get("facts"), Mapping),
            f"{key}: recommendation has no current passing filter evidence",
        )
        require(listing == "open" and kind == "full" and bool(verified), f"{key}: recommendation needs a verified open listing and full details")
        require(posting.get("verification_status") not in {"unknown", "failed"}, f"{key}: recommendation follows an unsuccessful source check")
        if posting.get("source") == "workday":
            require(posting.get("detail_verification_status") not in {"unknown", "failed"}, f"{key}: recommendation follows an unsuccessful detail check")
        require(not conflicts, f"{key}: recommendation has a hard conflict")
        require(any(
            isinstance(item, dict)
            and str(item.get("source", "")).startswith("profile.resume.")
            and item.get("requirement") and item.get("candidate_evidence")
            for item in evidence
        ), f"{key}: recommendation has no resume evidence")

    known = database.execute("SELECT COALESCE(SUM(known_jobs), 0) FROM source_health").fetchone()[0]
    require(known == len(source), "source coverage does not account for every posting")
    invalid_coverage = database.execute(
        "SELECT source_key FROM source_health WHERE known_jobs < 0 OR full_verified_details < 0 "
        "OR needs_detail_check < 0 OR full_verified_details + needs_detail_check > known_jobs"
    ).fetchone()
    require(invalid_coverage is None, "source coverage counts are inconsistent")
