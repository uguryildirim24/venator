from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from venator.discover.adapters import PostingBatch, WorkdayError, fetch_workday, workday_pages
from venator.discover.progress import (
    get_source_progress,
    load_source_progress,
    resume_offset,
    update_source_progress,
)
from venator.discover.run import _process_board
from venator.discover.store import latest_postings, load_source_health, refresh_postings


BOARD = "workday:example.wd1~Careers"


def _entry(number: int) -> dict[str, object]:
    return {
        "title": f"Scientist {number}",
        "externalPath": f"/job/US---Cambridge-MA/Scientist_R-{number}",
        "bulletFields": [f"R-{number}"],
    }


def _posting(number: int) -> dict[str, object]:
    return {
        "key": f"{BOARD}:R-{number}",
        "source": "workday",
        "board": "example.wd1~Careers",
        "external_id": f"R-{number}",
        "title": f"Scientist {number}",
        "description_html": "",
        "description_kind": "missing",
    }


def test_resumable_pages_return_the_next_uncommitted_offset() -> None:
    calls: list[int] = []

    def page(limit: int, offset: int) -> dict[str, object]:
        calls.append(offset)
        return {"total": 100, "jobPostings": [_entry(offset), _entry(offset + 1)]}

    first = workday_pages(
        "example.wd1~Careers", page, limit=2, max_pages=1, resumable=True
    )
    second = workday_pages(
        "example.wd1~Careers", page, limit=2, start_offset=first.next_offset, max_pages=1, resumable=True
    )

    assert calls == [0, 2]
    assert first.next_offset == 2
    assert first.scan_complete is False
    assert first.complete is False
    assert second.next_offset == 4


def test_zero_totals_after_head_do_not_end_a_fresh_walk() -> None:
    calls: list[int] = []

    def fetch(limit: int, offset: int) -> dict[str, object]:
        calls.append(offset)
        return {"total": 5 if offset == 0 else 0,
                "jobPostings": [_entry(n) for n in range(offset, min(offset + limit, 5))]}

    result = workday_pages("example.wd1~Careers", fetch, limit=2, resumable=True)
    assert calls == [0, 2, 4]
    assert len(result) == 5
    assert result.complete is True
    assert result.scan_complete is True


def test_resumed_zero_total_probes_head_before_claiming_completion() -> None:
    calls: list[int] = []

    def fetch(limit: int, offset: int) -> dict[str, object]:
        calls.append(offset)
        return {"total": 5 if offset == 0 else 0,
                "jobPostings": [_entry(n) for n in range(offset, min(offset + limit, 5))]}

    middle = workday_pages("example.wd1~Careers", fetch, limit=2,
                           start_offset=2, max_pages=1, resumable=True)
    assert calls == [2, 0]
    assert middle.next_offset == 4
    assert middle.scan_complete is False
    end = workday_pages("example.wd1~Careers", fetch, limit=2,
                        start_offset=4, resumable=True)
    assert calls == [2, 0, 4, 0]
    assert len(end) == 1
    assert end.scan_complete is True
    assert end.complete is False


def test_stale_repeated_page_stalls_without_advancing_or_spinning() -> None:
    page = {"total": 100, "jobPostings": [_entry(1), _entry(2)]}
    calls: list[int] = []

    def fetch(limit: int, offset: int) -> dict[str, object]:
        calls.append(offset)
        return page

    result = workday_pages(
        "example.wd1~Careers", fetch, limit=2, max_pages=20, resumable=True
    )

    assert result.stalled is True
    assert result.status == "stalled"
    assert result.next_offset == 2
    assert result.pages_fetched == 1
    assert calls == [0, 2]


def test_resumed_empty_page_marks_cycle_done_and_returns_to_head() -> None:
    result = workday_pages(
        "example.wd1~Careers",
        lambda limit, offset: {"total": 40, "jobPostings": []},
        start_offset=40,
        max_pages=1,
        resumable=True,
    )

    assert result.complete is False
    assert result.scan_complete is True
    assert result.next_offset == 40


def test_cross_run_repeated_page_uses_the_persisted_signature() -> None:
    first_page = {"total": 100, "jobPostings": [_entry(1), _entry(2)]}
    calls: list[int] = []

    def fetch(limit: int, offset: int) -> dict[str, object]:
        calls.append(offset)
        return first_page

    first = workday_pages(
        "example.wd1~Careers", fetch, limit=2, max_pages=1, resumable=True
    )
    second = workday_pages(
        "example.wd1~Careers",
        fetch,
        limit=2,
        start_offset=first.next_offset,
        max_pages=20,
        resumable=True,
        previous_page_signature=first.last_page_signature,
    )

    assert second.stalled is True
    assert second.pages_fetched == 0
    assert second.next_offset == first.next_offset
    assert calls == [0, 2]


def test_progress_failure_keeps_the_last_safe_offset_and_expiry_returns_to_head(tmp_path: Path) -> None:
    update_source_progress(
        tmp_path,
        BOARD,
        next_offset=1200,
        status="partial",
        last_progress_at="2026-09-01T00:00:00+00:00",
    )
    update_source_progress(
        tmp_path,
        BOARD,
        status="failed",
        last_attempt_at="2026-09-05T00:00:00+00:00",
        last_error="temporary outage",
    )

    state = get_source_progress(tmp_path, BOARD)
    assert state["next_offset"] == 1200
    assert state["status"] == "failed"
    assert state["last_error"] == "temporary outage"
    assert resume_offset(
        state,
        now=datetime(2026, 9, 9, tzinfo=timezone.utc),
        expires_after=timedelta(days=7),
    ) == (0, True)
    assert resume_offset(
        state,
        now=datetime(2026, 8, 30, tzinfo=timezone.utc),
        expires_after=timedelta(days=7),
    ) == (0, True)
    assert load_source_progress(tmp_path)[BOARD]["next_offset"] == 1200


def test_run_commits_observations_before_advancing_the_workday_cursor(tmp_path: Path) -> None:
    batches = [
        PostingBatch(
            [_posting(1)],
            complete=False,
            status="partial",
            next_offset=20,
            pages_fetched=1,
        ),
        PostingBatch(
            [_posting(2)],
            complete=False,
            status="partial",
            next_offset=40,
            pages_fetched=1,
        ),
    ]

    with patch("venator.discover.run.fetch_workday_window", side_effect=batches) as window, patch(
        "venator.discover.run.ENRICHERS", {},
    ):
        _process_board(tmp_path, "workday", "example.wd1~Careers", fetch_workday)
        _process_board(tmp_path, "workday", "example.wd1~Careers", fetch_workday)

    assert [call.kwargs["start_offset"] for call in window.call_args_list] == [0, 20]
    state = get_source_progress(tmp_path, BOARD)
    assert state["next_offset"] == 40
    assert state["status"] == "partial"
    assert set(latest_postings(tmp_path)) == {f"{BOARD}:R-1", f"{BOARD}:R-2"}
    # A rolling window cannot turn records outside the returned slice into
    # unknown or closed rows merely because they were absent from this run.
    assert all(row.get("listing_status") == "open" for row in latest_postings(tmp_path).values())


def test_run_failure_does_not_advance_the_cursor(tmp_path: Path) -> None:
    batch = PostingBatch(
        [_posting(1)],
        complete=False,
        status="partial",
        next_offset=20,
        pages_fetched=1,
    )
    with patch("venator.discover.run.fetch_workday_window", return_value=batch), patch(
        "venator.discover.run.ENRICHERS", {},
    ):
        _process_board(tmp_path, "workday", "example.wd1~Careers", fetch_workday)

    with patch(
        "venator.discover.run.fetch_workday_window",
        side_effect=WorkdayError("temporary outage"),
    ), patch("venator.discover.run.ENRICHERS", {}):
        result = _process_board(tmp_path, "workday", "example.wd1~Careers", fetch_workday)

    assert result[2] == "failed"
    state = get_source_progress(tmp_path, BOARD)
    assert state["next_offset"] == 20
    assert state["status"] == "failed"


def test_stalled_window_is_partial_health_and_retains_stall_cursor(tmp_path: Path) -> None:
    batch = PostingBatch(
        [],
        complete=False,
        status="stalled",
        error="repeated listing page",
        next_offset=20,
        stalled=True,
    )
    with patch("venator.discover.run.fetch_workday_window", return_value=batch), patch(
        "venator.discover.run.ENRICHERS", {},
    ):
        result = _process_board(tmp_path, "workday", "example.wd1~Careers", fetch_workday)

    assert result[2] == "partial"
    assert load_source_health(tmp_path)[BOARD]["status"] == "partial"
    assert get_source_progress(tmp_path, BOARD)["status"] == "stalled"


def test_detail_queue_includes_due_stored_rows_outside_the_listing_window(tmp_path: Path) -> None:
    old = _posting(99)
    refresh_postings(
        tmp_path,
        [old],
        source="workday",
        board="example.wd1~Careers",
        complete=False,
        status="partial",
    )
    fetched = PostingBatch(
        [_posting(1)],
        complete=False,
        status="partial",
        next_offset=20,
        pages_fetched=1,
    )
    asked: list[str] = []

    def detail(value: dict[str, object]) -> dict[str, object]:
        asked.append(str(value["key"]))
        return dict(value, description_html="<p>Full details.</p>", description_kind="full")

    with patch.dict("venator.discover.run.ENRICHERS", {"workday": detail}, clear=True):
        _process_board(
            tmp_path,
            "workday",
            "example.wd1~Careers",
            lambda _board: fetched,
        )

    assert f"{BOARD}:R-99" in asked
    current = latest_postings(tmp_path)[f"{BOARD}:R-99"]
    assert current["description_html"] == "<p>Full details.</p>"
    assert current["listing_status"] == "open"


def test_successful_detail_rechecks_stored_unknown_as_open_on_empty_window(tmp_path: Path) -> None:
    old = dict(_posting(99), listing_status="unknown")
    refresh_postings(
        tmp_path,
        [old],
        source="workday",
        board="example.wd1~Careers",
        complete=False,
        status="partial",
    )
    fetched = PostingBatch([], complete=False, status="partial", scan_complete=True, next_offset=40)

    def detail(value: dict[str, object]) -> dict[str, object]:
        return dict(value, description_html="<p>Recovered details.</p>", description_kind="full")

    with patch.dict("venator.discover.run.ENRICHERS", {"workday": detail}, clear=True):
        _process_board(
            tmp_path,
            "workday",
            "example.wd1~Careers",
            lambda _board: fetched,
            detail_limit=1,
        )

    current = latest_postings(tmp_path)[f"{BOARD}:R-99"]
    assert current["listing_status"] == "open"
    assert current["detail_verification_status"] == "verified"
