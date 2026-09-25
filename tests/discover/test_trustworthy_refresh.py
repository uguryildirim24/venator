from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from venator.discover.adapters import (
    PartialBoardError,
    PostingBatch,
    fetch_greenhouse,
    fetch_lever,
    normalize_lever_job,
    refresh_posting,
)
from venator.discover.run import _ordered_detail_candidates
from venator.discover.store import (
    append_observations,
    latest_postings,
    posting_revision,
    refresh_postings,
    update_source_health,
)


def _seed(directory: Path, postings: list[dict]) -> None:
    refresh_postings(directory, postings, complete=False, status="partial")


def posting(key: str = "greenhouse:board:1", **extra: object) -> dict:
    return {
        "key": key,
        "source": key.split(":", 1)[0],
        "board": key.split(":")[1],
        "external_id": key.rsplit(":", 1)[-1],
        "discovered_at": "2026-08-01T00:00:00+00:00",
        "title": "Scientist",
        "location": "Cambridge, MA",
        "url": "https://example.invalid/job/1",
        "posted_at": "2026-08-01",
        "description_html": "<p>Full requirements.</p>",
        "description_kind": "full",
        "locations": [{"name": "Cambridge, MA", "country": "US", "region": "MA", "city": "Cambridge"}],
        **extra,
    }


def test_lever_preserves_description_lists_and_additional() -> None:
    value = normalize_lever_job(
        "labs",
        {
            "id": "lever-1",
            "text": "Research Associate",
            "categories": {"location": "Boston, MA", "workplaceType": "hybrid"},
            "hostedUrl": "https://jobs.lever.co/labs/lever-1",
            "applyUrl": "https://jobs.lever.co/labs/lever-1/apply",
            "createdAt": 1710000000000,
            "updatedAt": 1711000000000,
            "description": "<p>About the team.</p>",
            "lists": [
                {"text": "What you will do", "content": ["Run assays", "Analyze data"]},
                {"text": "Qualifications", "content": ["BS in biology"]},
            ],
            "additional": "<p>Equal opportunity employer.</p>",
        },
    )

    assert "Run assays" in value["description_html"]
    assert "BS in biology" in value["description_html"]
    assert "Equal opportunity employer" in value["description_html"]
    assert value["description_kind"] == "full"
    assert value["workplace_type"] == "hybrid"
    assert value["apply_url"].endswith("/apply")
    assert value["source_facts"]["lists"][1]["text"] == "Qualifications"


def test_malformed_direct_list_is_not_an_empty_success() -> None:
    response = httpx.Response(
        200,
        json={"unexpected": []},
        request=httpx.Request("GET", "https://example.invalid"),
    )
    with patch("httpx.get", return_value=response):
        try:
            fetch_greenhouse("labs")
        except Exception as error:
            assert "jobs" in str(error)
        else:  # pragma: no cover - protects the failure boundary
            raise AssertionError("malformed board response was accepted")


def test_refresh_appends_new_content_and_preserves_first_seen(tmp_path: Path) -> None:
    first = posting()
    _seed(tmp_path, [first])
    refreshed = posting(description_html="<p>Changed requirements.</p>", title="Senior Scientist")

    result = refresh_postings(
        tmp_path,
        [refreshed],
        source="greenhouse",
        board="board",
        observed_at="2026-08-09T00:00:00+00:00",
    )
    current = latest_postings(tmp_path)[first["key"]]

    assert result.refreshed == 1
    assert current["title"] == "Senior Scientist"
    assert current["description_html"] == "<p>Changed requirements.</p>"
    assert current["first_seen_at"] == "2026-08-01T00:00:00+00:00"
    assert current["discovered_at"] == "2026-08-01T00:00:00+00:00"
    assert current["last_verified_at"] == "2026-08-09T00:00:00+00:00"


def test_complete_snapshot_marks_stale_missing_unknown(tmp_path: Path) -> None:
    _seed(tmp_path, [posting("lever:board:1"), posting("lever:board:2")])

    result = refresh_postings(
        tmp_path,
        [posting("lever:board:1")],
        source="lever",
        board="board",
        complete=True,
        status="ok",
        observed_at="2026-08-09T00:00:00+00:00",
    )
    current = latest_postings(tmp_path)

    assert result.unknown == 1
    assert current["lever:board:1"]["listing_status"] == "open"
    assert current["lever:board:2"]["listing_status"] == "unknown"


def test_failed_or_partial_board_does_not_close_missing_jobs(tmp_path: Path) -> None:
    _seed(tmp_path, [posting("ashby:board:1"), posting("ashby:board:2")])

    refresh_postings(
        tmp_path,
        [],
        source="ashby",
        board="board",
        complete=False,
        status="partial",
        observed_at="2026-08-09T00:00:00+00:00",
    )
    current = latest_postings(tmp_path)
    assert current["ashby:board:1"]["listing_status"] == "open"
    assert current["ashby:board:2"]["listing_status"] == "open"

    refresh_postings(
        tmp_path,
        [],
        source="ashby",
        board="board",
        complete=False,
        status="failed",
        observed_at="2026-08-10T00:00:00+00:00",
    )
    assert latest_postings(tmp_path)["ashby:board:1"]["listing_status"] == "open"


def test_exact_closed_is_the_only_explicit_close_path(tmp_path: Path) -> None:
    key = "greenhouse:board:1"
    _seed(tmp_path, [posting(key)])
    result = refresh_postings(
        tmp_path,
        [],
        source="greenhouse",
        board="board",
        complete=False,
        status="partial",
        closed_keys=[key],
        observed_at="2026-08-10T00:00:00+00:00",
    )
    assert result.closed == 1
    assert latest_postings(tmp_path)[key]["listing_status"] == "closed"


def test_full_description_survives_missing_detail_observation(tmp_path: Path) -> None:
    key = "workday:tenant~Careers:1"
    _seed(tmp_path, [posting(key, source="workday", board="tenant~Careers")])
    append_observations(
        tmp_path,
        [
            posting(
                key,
                source="workday",
                board="tenant~Careers",
                description_html="",
                description_kind="missing",
                verification_status="unknown",
            )
        ],
    )
    current = latest_postings(tmp_path)[key]
    assert current["description_html"] == "<p>Full requirements.</p>"
    assert current["description_kind"] == "full"
    assert current["verification_status"] == "unknown"


def test_revision_ignores_observation_clocks_but_tracks_content_and_status() -> None:
    original = posting(listing_status="open", last_verified_at="2026-08-01T00:00:00+00:00")
    same = dict(original, last_verified_at="2026-08-11T00:00:00+00:00", first_seen_at="later")
    changed = dict(same, description_html="<p>New requirements.</p>")
    closed = dict(same, listing_status="closed")

    assert posting_revision(original) == posting_revision(same)
    assert posting_revision(original) != posting_revision(changed)
    assert posting_revision(original) != posting_revision(closed)


def test_normalized_exact_refresh_keeps_revision_across_verification_clocks(tmp_path: Path) -> None:
    original = posting("greenhouse:labs:1")
    payload = {
        "id": "1",
        "title": "Scientist",
        "absolute_url": original["url"],
        "content": original["description_html"],
        "first_published": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-08T00:00:00Z",
    }
    response = httpx.Response(
        200,
        json=payload,
        request=httpx.Request("GET", "https://example.invalid"),
    )
    with patch("httpx.get", return_value=response):
        first = refresh_posting(original)
    append_observations(tmp_path, [dict(first, detail_attempted_at="2026-08-11T00:00:00Z")], observed_at="2026-08-11T00:00:00Z")
    second = dict(
        first,
        detail_attempted_at="2026-08-12T00:00:00Z",
        detail_verified_at="2026-08-12T00:00:01Z",
        last_detail_verified_at="2026-08-12T00:00:01Z",
        verification_status="verified",
        verification_error="",
    )
    append_observations(tmp_path, [second], observed_at="2026-08-12T00:00:00Z")
    round_trip = latest_postings(tmp_path)[original["key"]]

    assert first["description_kind"] == "full"
    assert posting_revision(first) == posting_revision(second)
    assert posting_revision(first) == posting_revision(round_trip)


def test_exact_refresh_rejects_a_mismatched_greenhouse_identity() -> None:
    original = posting("greenhouse:labs:1")
    response = httpx.Response(
        200,
        json={"id": "different", "title": "Other job", "content": "<p>Other</p>"},
        request=httpx.Request("GET", "https://example.invalid"),
    )
    with patch("httpx.get", return_value=response):
        refreshed = refresh_posting(original)
    assert refreshed["listing_status"] == "unknown"
    assert refreshed["description_html"] == original["description_html"]


def test_ashby_board_outage_does_not_close_an_individual_job() -> None:
    original = posting("ashby:labs:1")
    with patch("httpx.get", return_value=httpx.Response(404)):
        refreshed = refresh_posting(original)
    assert refreshed["listing_status"] == "unknown"


def test_explicitly_empty_source_fields_clear_prior_values(tmp_path: Path) -> None:
    prior = posting(
        "lever:board:1",
        locations=[{"name": "Boston, MA", "country": "US", "region": "MA", "city": "Boston"}],
        location="Boston, MA",
        application_deadline="2026-09-30",
        source_facts={"location": "Boston, MA", "endDate": "2026-09-30"},
    )
    refresh_postings(
        tmp_path,
        [prior],
        source="lever",
        board="board",
        observed_at="2026-08-01T00:00:00+00:00",
    )
    refreshed = dict(
        prior,
        locations=[],
        location="",
        application_deadline=None,
        source_facts={"location": "", "endDate": ""},
    )
    append_observations(tmp_path, [refreshed])
    current = latest_postings(tmp_path)[prior["key"]]
    assert current["locations"] == []
    assert current["location"] == ""
    assert current["application_deadline"] is None


def test_unknown_observation_preserves_last_verified_at_and_records_attempt(tmp_path: Path) -> None:
    prior = posting(
        "greenhouse:board:1",
        last_verified_at="2026-08-01T00:00:00+00:00",
        listing_status="open",
    )
    refresh_postings(
        tmp_path,
        [prior],
        source="greenhouse",
        board="board",
        observed_at="2026-08-01T00:00:00+00:00",
    )
    append_observations(
        tmp_path,
        [dict(prior, listing_status="unknown", verification_status="unknown")],
        observed_at="2026-08-12T00:00:00+00:00",
    )
    current = latest_postings(tmp_path)[prior["key"]]
    assert current["listing_status"] == "unknown"
    assert current["last_verified_at"] == "2026-08-01T00:00:00+00:00"
    assert current["last_attempt_at"] == "2026-08-12T00:00:00+00:00"


def test_source_health_has_stable_nullable_message(tmp_path: Path) -> None:
    update_source_health(
        tmp_path,
        "greenhouse:labs",
        status="ok",
        count=4,
        attempted_at="2026-08-10T00:00:00+00:00",
    )
    update_source_health(
        tmp_path,
        "greenhouse:labs",
        status="failed",
        count=0,
        error="  timeout\nwith details ",
        attempted_at="2026-08-11T00:00:00+00:00",
    )
    value = json.loads((tmp_path / "source-health.json").read_text())
    assert value["greenhouse:labs"] == {
        "status": "failed",
        "last_attempt_at": "2026-08-11T00:00:00+00:00",
        "last_success_at": "2026-08-10T00:00:00+00:00",
        "count": 0,
        "message": "timeout with details",
    }


def test_workday_detail_candidates_have_no_background_cap(tmp_path: Path) -> None:
    values = [
        posting(f"workday:board:{index}", source="workday", board="board", description_html="", description_kind="missing")
        for index in range(60)
    ]
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    assert len(_ordered_detail_candidates("workday", values, {}, now=now)) == 60
    assert len(_ordered_detail_candidates("workday", values, {}, now=now, limit=20)) == 20


def test_legacy_workday_full_rows_enter_the_refresh_queue(tmp_path: Path) -> None:
    values = [
        posting(
            f"workday:board:{index}",
            source="workday",
            board="board",
            description_html="<p>Legacy full text.</p>",
            description_kind="full",
        )
        for index in range(41)
    ]
    candidates = _ordered_detail_candidates(
        "workday",
        values,
        {value["key"]: value for value in values},
        now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    assert len(candidates) == 41


def test_exact_refresh_404_is_closed_and_timeout_is_unknown() -> None:
    original = posting("greenhouse:board:1")
    closed_response = httpx.Response(404)
    with patch("httpx.get", return_value=closed_response):
        assert refresh_posting(original)["listing_status"] == "closed"

    with patch("httpx.get", side_effect=httpx.ReadTimeout("slow")):
        refreshed = refresh_posting(original)
    assert refreshed["listing_status"] == "unknown"
    assert refreshed["description_html"] == original["description_html"]


def test_workday_blocked_exact_refresh_is_unknown() -> None:
    original = posting(
        "workday:amgen.wd1~Careers:R-1",
        source="workday",
        board="amgen.wd1~Careers",
        external_id="R-1",
        external_path="/job/US---Cambridge-MA/Scientist_R-1",
    )
    response = httpx.Response(
        403,
        request=httpx.Request("GET", "https://example.invalid"),
    )
    with patch("httpx.Client.request", return_value=response):
        refreshed = refresh_posting(original)
    assert refreshed["listing_status"] == "unknown"


def test_profile_prioritizes_plausible_and_unknown_workday_details_without_dropping_listings(tmp_path: Path) -> None:
    from venator.discover.run import _process_board
    from venator.profile import load_profile

    profile = load_profile(Path(__file__).parents[2] / "profiles" / "example")
    values = [
        posting("workday:board:a", title="Research Associate", location="Paris, France",
                locations=[{"name": "Paris", "country": "FR"}], description_html="", description_kind="missing"),
        posting("workday:board:b", title="Associate Director", description_html="", description_kind="missing"),
        posting("workday:board:y", title="Research Associate", description_html="", description_kind="missing"),
        posting("workday:board:z", title="Research Associate", location="", locations=[],
                description_html="", description_kind="missing"),
    ]

    def bounded(*args: object, **kwargs: object) -> list:
        return _ordered_detail_candidates(*args, **kwargs, limit=2)

    def detail(value: dict) -> dict:
        return dict(value, description_html="<p>Full details.</p>", description_kind="full")

    with patch("venator.discover.run._ordered_detail_candidates", side_effect=bounded), patch(
        "venator.discover.run.ENRICHERS", {"workday": detail}
    ):
        _process_board(tmp_path, "workday", "board", lambda _: values, profile=profile)

    current = latest_postings(tmp_path)
    assert set(current) == {value["key"] for value in values}
    assert {key for key, value in current.items() if value.get("detail_verified_at")} == {
        "workday:board:a", "workday:board:y",
    }
    assert current["workday:board:a"]["description_kind"] == "full"
    assert current["workday:board:b"]["description_kind"] == "missing"


def test_profile_detail_priority_respects_retry_clocks_and_retains_conflicts() -> None:
    from datetime import datetime, timezone

    from venator.profile import load_profile

    profile = load_profile(Path(__file__).parents[2] / "profiles" / "example")
    now = datetime(2026, 9, 4, tzinfo=timezone.utc)
    relevant = posting("workday:board:y", title="Research Associate", description_html="",
                       description_kind="missing", detail_attempted_at=now.isoformat())
    conflict = posting("workday:board:a", title="Associate Director", description_html="", description_kind="missing")
    candidates = _ordered_detail_candidates(
        "workday", [relevant, conflict], {relevant["key"]: relevant},
        now=now, limit=1, profile=profile,
    )
    assert candidates == []
