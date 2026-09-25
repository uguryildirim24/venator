"""A bounded refresh must make progress instead of repeating recent attempts."""
from datetime import datetime, timedelta, timezone

import pytest

from venator.discover.run import _detail_is_due, _ordered_detail_candidates


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def job(key, *, days_ago=None, missing=False, attempted=None):
    value = {
        "key": f"workday:example~Careers:{key}",
        "description_html": "" if missing else "<p>Employer requirements.</p>",
        "description_kind": "missing" if missing else "full",
    }
    if days_ago is not None:
        value["detail_verified_at"] = (NOW - timedelta(days=days_ago)).isoformat()
    if attempted is not None:
        value["detail_attempted_at"] = attempted.isoformat()
    return value


def queue(*rows, limit=40, now=NOW):
    return _ordered_detail_candidates(
        "workday", rows, {row["key"]: row for row in rows}, now=now, limit=limit
    )


def test_oldest_overdue_description_is_checked_first():
    recent, older, oldest = job("a", days_ago=8), job("b", days_ago=15), job("c", days_ago=30)
    assert queue(recent, older, oldest, limit=1) == [oldest]


@pytest.mark.parametrize("missing", [False, True])
def test_failed_check_waits_for_retry_window_even_with_a_cached_description(missing):
    failed = job("failed", days_ago=30, missing=missing, attempted=NOW)
    assert not _detail_is_due(failed, now=NOW + timedelta(minutes=1))
    assert _detail_is_due(failed, now=NOW + timedelta(hours=6))


def test_a_failed_job_moves_behind_jobs_with_older_attempts_after_cooldown():
    failed = job("a", days_ago=30, attempted=NOW - timedelta(hours=7))
    neglected = job("b", days_ago=15)
    assert queue(failed, neglected, limit=1) == [neglected]


def test_a_previously_failed_missing_description_does_not_displace_an_unchecked_job():
    failed = job("a", missing=True, attempted=NOW - timedelta(hours=7))
    unchecked = job("z", missing=True)
    assert queue(failed, unchecked, limit=1) == [unchecked]


def test_successful_recent_description_is_not_rechecked():
    assert queue(job("fresh", days_ago=1)) == []


def test_failed_recheck_of_recent_full_description_retries_after_cooldown():
    failed = job("failed", days_ago=1, attempted=NOW)
    failed["detail_verification_status"] = "unknown"
    assert queue(failed, now=NOW + timedelta(minutes=1)) == []
    assert queue(failed, now=NOW + timedelta(hours=6)) == [failed]


def test_closed_jobs_do_not_consume_the_detail_budget():
    closed = dict(job("a", days_ago=30), listing_status="closed")
    waiting = job("z", missing=True)
    assert queue(closed, waiting, limit=1) == [waiting]


@pytest.mark.parametrize("stamp", ["not a time", "2026-09-05T12:00:00", "2099-01-01T00:00:00Z"])
def test_unusable_clocks_cannot_crash_or_defer_verification_forever(stamp):
    invalid = job("invalid", days_ago=30)
    invalid.update(detail_verified_at=stamp, detail_attempted_at=stamp)
    assert queue(invalid) == [invalid]
