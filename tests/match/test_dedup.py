"""Regression tests for conservative, identity-based Posting deduplication."""

from __future__ import annotations

import random
import re
from pathlib import Path

from venator.discover.adapters import ADAPTERS
from venator.match.dedup import (
    SOURCE_NAMES,
    canonical_destination,
    compatible_locations,
    duplicate_decisions,
    duplicate_groups,
    is_remote,
    normalize_employer,
    normalize_title,
)

NAMES = {"benchling": "Benchling", "ginkgobioworks": "Ginkgo Bioworks"}


def posting(key: str, **fields: object) -> dict:
    return {"key": key, "discovered_at": "2026-08-18T18:00:00+00:00", **fields}


def test_native_ids_are_scoped_to_their_employer_board() -> None:
    rows = [posting(f"greenhouse:{board}:42", source="greenhouse", board=board, external_id="42") for board in ("one", "two")]
    assert duplicate_groups(rows, {}) == []


def test_closed_copy_cannot_hide_a_verified_open_copy() -> None:
    identity = {"source": "greenhouse", "board": "one", "external_id": "42", "url": "https://example.test/job/42"}
    old = posting("closed-copy", **identity, listing_status="closed")
    current = posting("open-copy", **identity, listing_status="open", discovered_at="2026-09-04T00:00:00Z")
    groups = duplicate_groups([old, current], {})
    assert len(groups) == 1
    assert groups[0].winner.key == "open-copy"


def test_tracking_parameters_are_removed_but_meaningful_query_data_remains() -> None:
    assert canonical_destination(
        "HTTPS://Jobs.Example.com/jobs/123/?utm_source=adzuna&gclid=x&locale=en#apply"
    ) == "https://jobs.example.com/jobs/123?locale=en"
    assert canonical_destination("https://jobs.example.com/jobs/123?ref=campaign") != canonical_destination(
        "https://jobs.example.com/jobs/123?ref=other"
    )
    assert canonical_destination("https://jobs.example.com/jobs/123?job=1") != canonical_destination(
        "https://jobs.example.com/jobs/123?job=2"
    )


def test_aggregator_and_employer_records_with_same_destination_collapse() -> None:
    employer = posting(
        "ashby:benchling:df9f0b19",
        source="ashby",
        board="benchling",
        title="Solutions Delivery Manager",
        location="Boston, MA",
        description_html="<p>The whole description.</p>",
        external_id="df9f0b19",
        url="https://jobs.benchling.com/jobs/df9f0b19?utm_source=employer",
    )
    aggregated = posting(
        "adzuna:us:5847155128",
        source="adzuna",
        company="Benchling",
        title="Solutions Delivery Manager - Boston",
        location="Boston, Suffolk County",
        snippet=True,
        external_id="5847155128",
        url="https://jobs.benchling.com/jobs/df9f0b19?gclid=tracking",
        discovered_at="2026-08-18T20:39:16+00:00",
    )

    groups = duplicate_groups([aggregated, employer], NAMES)

    assert len(groups) == 1
    assert groups[0].winner.key == employer["key"]
    assert [loser.key for loser in groups[0].losers] == [aggregated["key"]]


def test_same_company_and_title_with_distinct_requisitions_stays_separate() -> None:
    postings = [
        posting(
            f"greenhouse:benchling:{req}",
            source="greenhouse",
            board="benchling",
            company="Benchling",
            title="Research Associate",
            location="Boston, MA",
            external_id=req,
            url=f"https://boards.example/jobs/{req}",
        )
        for req in ("R-100", "R-200")
    ]

    assert duplicate_groups(postings, NAMES) == []


def test_same_title_in_meaningfully_different_locations_stays_separate() -> None:
    postings = [
        posting(
            f"ashby:benchling:{city}",
            source="ashby",
            board="benchling",
            title="Strategic Account Executive",
            location=location,
            external_id=city,
            url=f"https://jobs.benchling.com/jobs/{city}",
        )
        for city, location in (("sd", "San Diego, CA"), ("sf", "San Francisco, CA"))
    ]

    assert not compatible_locations("San Diego, CA", "San Francisco, CA")
    assert duplicate_groups(postings, NAMES) == []


def test_same_requisition_id_is_identity_when_only_one_copy_has_a_destination() -> None:
    employer = posting(
        "greenhouse:benchling:R-42",
        source="greenhouse",
        board="benchling",
        title="Research Associate",
        location="Boston, MA",
        external_id="R-42",
        url="https://boards.example/jobs/R-42",
    )
    imported = posting(
        "user:import:R-42",
        source="import",
        company="Benchling, Inc.",
        title="Research Associate",
        location="Boston",
        requisition_id="R-42",
    )

    groups = duplicate_groups([employer, imported], NAMES)

    assert len(groups) == 1
    assert groups[0].winner.key == employer["key"]


def test_conflicting_strong_identity_fields_never_merge() -> None:
    first = posting(
        "greenhouse:benchling:R-1",
        source="greenhouse",
        board="benchling",
        title="Research Associate",
        external_id="R-1",
        url="https://boards.example/jobs/R-1",
    )
    second = posting(
        "greenhouse:benchling:R-2",
        source="greenhouse",
        board="benchling",
        title="Research Associate",
        external_id="R-2",
        url="https://boards.example/jobs/R-1",
    )

    assert duplicate_groups([first, second], NAMES) == []


def test_company_title_and_location_without_identity_are_not_a_fallback() -> None:
    postings = [
        posting(
            f"adzuna:us:{index}",
            source="adzuna",
            company="Benchling",
            title="Research Associate",
            location="Boston, MA",
        )
        for index in (1, 2)
    ]

    assert duplicate_groups(postings, NAMES) == []


def test_same_destination_is_order_independent_and_full_copy_wins() -> None:
    snippet = posting(
        "adzuna:us:1",
        source="adzuna",
        company="Benchling",
        title="Research Associate",
        location="Boston, MA",
        snippet=True,
        url="https://jobs.benchling.com/jobs/R-1?utm_campaign=x",
        discovered_at="2026-08-01T00:00:00+00:00",
    )
    full = posting(
        "ashby:benchling:R-1",
        source="ashby",
        board="benchling",
        title="Research Associate",
        location="Boston, MA",
        url="https://jobs.benchling.com/jobs/R-1",
        discovered_at="2026-08-18T00:00:00+00:00",
    )
    expected = duplicate_decisions([snippet, full], NAMES)

    for seed in range(12):
        shuffled = [snippet, full]
        random.Random(seed).shuffle(shuffled)
        assert duplicate_decisions(shuffled, NAMES) == expected
    assert set(expected) == {snippet["key"]}


def test_homonymous_locations_remain_distinct_in_location_helper() -> None:
    assert not compatible_locations("Cambridge, MA", "Cambridge, England")
    assert not compatible_locations("London, Ontario", "London, England")
    assert not compatible_locations("Portland, OR", "Portland, ME")
    assert compatible_locations("Boston, MA", "Boston, Suffolk County")
    assert compatible_locations("Remote, US", "San Francisco, CA")
    assert compatible_locations("", "San Francisco, CA")
    assert is_remote("Remote, US")
    assert not is_remote("Remote Opportunity - Canada; Toronto, Ontario")


def test_title_and_employer_normalization_remain_conservative() -> None:
    assert normalize_title("  Research Associate,  ") == "research associate"
    assert normalize_title("Scientist I") != normalize_title("Scientist II")
    assert normalize_title("Scientist (R-4182)") != normalize_title("Scientist")
    assert normalize_employer("Odaia Intelligence Inc.") == normalize_employer("Odaia Intelligence")
    assert normalize_employer("Generate:Biomedicines") != normalize_employer("Generate Biomedicines")


def test_unresolvable_employer_does_not_affect_exact_destination_identity() -> None:
    first = posting(
        "greenhouse:unregistered:1",
        source="greenhouse",
        board="unregistered",
        title="Research Associate",
        url="https://jobs.example/R-1",
    )
    second = posting(
        "adzuna:us:R-1",
        source="adzuna",
        title="Research Associate",
        company="Some Biotech",
        url="https://jobs.example/R-1?utm_source=feed",
    )

    assert len(duplicate_groups([first, second], NAMES)) == 1


def dashboard_source_labels() -> dict[str, str]:
    """Read the dashboard's source map without importing the UI project."""
    labels = (Path(__file__).parents[2] / "ui" / "src" / "labels.ts").read_text(encoding="utf-8")
    match = re.search(r"^const SOURCES = new Map<string, string>\(\[(.*?)^\]\);", labels, re.S | re.M)
    assert match is not None
    entries = re.findall(r'\[\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\]', match.group(1))
    assert entries
    return dict(entries)


def test_every_fetchable_source_has_a_display_name() -> None:
    fetchable = set(ADAPTERS) | {"adzuna"}
    assert fetchable <= set(SOURCE_NAMES)
    assert SOURCE_NAMES == dashboard_source_labels()


def test_missing_description_cannot_hide_an_open_full_detail_copy() -> None:
    identity = {"url": "https://example.test/job/42", "listing_status": "open"}
    missing = posting("workday:board:42", **identity, source="workday", description_kind="missing",
                      description_html="", discovered_at="2026-08-01T00:00:00Z")
    full = posting("greenhouse:board:42", **identity, source="greenhouse", description_kind="full",
                   description_html="<p>Full requirements.</p>", discovered_at="2026-09-04T00:00:00Z")
    assert duplicate_groups([missing, full], {})[0].winner.key == full["key"]
    # Description completeness cannot promote a closed copy above an open one.
    full["listing_status"] = "closed"
    assert duplicate_groups([full, missing], {})[0].winner.key == missing["key"]
