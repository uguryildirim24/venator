"""Source coverage in the view is based on evidence, not source batch counts."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from venator.view.build import build_database, source_coverage


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_build_coverage_clock(monkeypatch):
    # The integration tests use dated evidence, just like the direct tests.
    # Advancing the host clock must not turn their fresh fixtures stale.
    monkeypatch.setattr(
        "venator.view.build.source_coverage",
        lambda postings: source_coverage(postings, now=NOW),
    )


def _posting(source: str, board: str, key: str, **fields: object) -> dict:
    return {
        "key": f"{source}:{board}:{key}",
        "source": source,
        "board": board,
        "title": "Research associate",
        "description_kind": "full",
        "description_html": "<p>Readable job details.</p>",
        "listing_status": "open",
        "verification_status": "verified",
        "last_verified_at": "2026-09-05T11:00:00Z",
        **fields,
    }


def test_source_coverage_separates_listing_and_detail_evidence() -> None:
    rows = [
        _posting("greenhouse", "acme", "verified"),
        _posting("greenhouse", "acme", "failed", verification_status="unknown"),
        _posting("greenhouse", "acme", "missing", description_kind="missing", description_html=""),
        _posting("greenhouse", "acme", "closed", listing_status="closed"),
        _posting("workday", "globex", "legacy", detail_verification_status=None),
        _posting(
            "workday",
            "globex",
            "failed-detail",
            detail_verification_status="unknown",
            detail_verified_at="2026-09-05T10:00:00Z",
        ),
        _posting(
            "workday",
            "globex",
            "verified-detail",
            detail_verification_status="verified",
            detail_verified_at="2026-09-05T10:00:00Z",
        ),
        _posting(
            "workday",
            "globex",
            "future-clock",
            detail_verification_status="verified",
            detail_verified_at="2027-09-05T10:00:00Z",
        ),
        _posting(
            "workday",
            "globex",
            "unknown-listing",
            listing_status="unknown",
            detail_verification_status="verified",
            detail_verified_at="2026-09-05T10:00:00Z",
        ),
    ]

    assert source_coverage(rows, now=NOW) == {
        "greenhouse:acme": {
            "known_jobs": 4,
            "full_verified_details": 1,
            "needs_detail_check": 2,
        },
        "workday:globex": {
            "known_jobs": 5,
            "full_verified_details": 1,
            "needs_detail_check": 4,
        },
    }


def test_source_coverage_rejects_malformed_clock_and_empty_full_html() -> None:
    rows = [
        _posting(
            "lever",
            "acme",
            "malformed-clock",
            last_verified_at="yesterday",
        ),
        _posting(
            "ashby",
            "acme",
            "empty-html",
            description_html="<!-- no readable details -->",
        ),
    ]

    assert source_coverage(rows, now=NOW)["lever:acme"]["full_verified_details"] == 0
    assert source_coverage(rows, now=NOW)["ashby:acme"]["full_verified_details"] == 0
    assert source_coverage(rows, now=NOW)["lever:acme"]["needs_detail_check"] == 1
    assert source_coverage(rows, now=NOW)["ashby:acme"]["needs_detail_check"] == 1


def test_legacy_postings_without_source_metadata_remain_in_coverage() -> None:
    assert source_coverage([
        {"key": "imported:one", "title": "Imported posting"},
        {"key": "lever:two", "source": "lever", "title": "Missing board"},
    ], now=NOW) == {
        "unknown": {"known_jobs": 1, "full_verified_details": 0, "needs_detail_check": 1},
        "lever:unknown": {"known_jobs": 1, "full_verified_details": 0, "needs_detail_check": 1},
    }


def test_stale_workday_detail_is_pending_again_after_shared_refresh_window() -> None:
    rows = [
        _posting(
            "workday",
            "acme",
            "stale",
            detail_verification_status="verified",
            detail_verified_at="2026-08-28T11:00:00Z",
        )
    ]

    assert source_coverage(rows, now=NOW)["workday:acme"] == {
        "known_jobs": 1,
        "full_verified_details": 0,
        "needs_detail_check": 1,
    }


def test_stale_direct_listing_verification_is_pending_too() -> None:
    rows = [_posting("greenhouse", "acme", "stale", last_verified_at="2026-08-28T11:00:00Z")]

    assert source_coverage(rows, now=NOW)["greenhouse:acme"] == {
        "known_jobs": 1,
        "full_verified_details": 0,
        "needs_detail_check": 1,
    }


def test_build_projects_partial_health_against_known_rows_and_zero_sources(tmp_path: Path) -> None:
    postings_dir = tmp_path / "postings"
    postings_dir.mkdir()
    (postings_dir / "2026-09-05.jsonl").write_text(
        json.dumps(_posting("greenhouse", "acme", "one")) + "\n",
        encoding="utf-8",
    )
    (postings_dir / "source-health.json").write_text(
        json.dumps(
            {
                "greenhouse:acme": {
                    "status": "partial",
                    "count": 1200,
                    "message": "The board returned a partial page.",
                },
                "greenhouse:empty": {
                    "status": "ok",
                    "count": 0,
                    "message": None,
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "venator.db"

    build_database(
        postings_dir,
        tmp_path / "decisions",
        output,
        tmp_path / "track",
        tmp_path / "runs.jsonl",
    )

    with sqlite3.connect(output) as database:
        rows = database.execute(
            "SELECT source_key, status, count, known_jobs, full_verified_details, needs_detail_check "
            "FROM source_health ORDER BY source_key"
        ).fetchall()
    assert rows == [
        ("greenhouse:acme", "partial", 1200, 1, 1, 0),
        ("greenhouse:empty", "ok", 0, 0, 0, 0),
    ]


def test_build_exposes_postings_without_health_as_unknown_source_coverage(tmp_path: Path) -> None:
    postings_dir = tmp_path / "postings"
    postings_dir.mkdir()
    (postings_dir / "2026-09-05.jsonl").write_text(
        json.dumps(_posting("lever", "acme", "one")) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "venator.db"

    build_database(
        postings_dir,
        tmp_path / "decisions",
        output,
        tmp_path / "track",
        tmp_path / "runs.jsonl",
    )

    with sqlite3.connect(output) as database:
        row = database.execute(
            "SELECT source_key, status, known_jobs, full_verified_details, needs_detail_check "
            "FROM source_health"
        ).fetchone()
    assert row == ("lever:acme", "unknown", 1, 1, 0)
