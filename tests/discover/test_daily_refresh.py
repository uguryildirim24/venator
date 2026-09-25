"""Interactive discovery stays bounded; background discovery drains."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from venator.discover.adapters import normalize_greenhouse_job
from venator.discover.run import _ordered_detail_candidates, _process_board
from venator.discover.store import latest_postings
from venator.profile import load_profile
from venator.profile.schema import SearchTargeting
from venator.view.build import source_coverage


def test_organizational_office_cannot_replace_the_postings_work_location():
    posting = normalize_greenhouse_job("labs", {
        "id": 1, "title": "Software Intern", "location": {"name": "Emeryville, California"},
        "offices": [{"name": "Boston Drydock", "location": "Boston, Massachusetts, United States"}],
        "content": "<p>On-site software internship in Emeryville, California.</p>",
    })
    assert posting["location"] == "Emeryville, California"
    assert posting["source_facts"]["offices"][0]["name"] == "Boston Drydock"


def test_explicit_additional_work_locations_are_retained():
    posting = normalize_greenhouse_job("labs", {
        "id": 1, "location": {"name": "Emeryville, California"},
        "locations": [{"name": "Boston, Massachusetts"}],
    })
    assert "Boston, Massachusetts" in posting["location"]


def test_successful_direct_feed_counts_as_verified_without_an_extra_detail_request(tmp_path):
    posting = normalize_greenhouse_job("labs", {
        "id": 1, "title": "Research Intern", "location": {"name": "Boston, Massachusetts"},
        "content": "<p>Conduct experiments and analyze laboratory results.</p>",
    })
    _process_board(tmp_path, "greenhouse", "labs", lambda board: [posting])
    rows = latest_postings(tmp_path)
    assert rows[posting["key"]]["verification_status"] == "verified"
    assert source_coverage(rows.values())["greenhouse:labs"] == {
        "known_jobs": 1, "full_verified_details": 1, "needs_detail_check": 0,
    }


def test_title_search_phrases_do_not_reorder_detail_candidates():
    profile = load_profile(Path(__file__).parents[2] / "profiles" / "example")
    profile = replace(profile, search=SearchTargeting(queries=("software",), seniority=("co-op",)))
    rows = [
        {"key": "workday:board:a", "title": "Unspecified opening"},
        {"key": "workday:board:b", "title": "Software Engineer"},
        {"key": "workday:board:c", "title": "Research Co Op"},
    ]
    with patch("venator.discover.run.apply_filters") as filters:
        filters.return_value.verdict = "pass"
        ordered = _ordered_detail_candidates("workday", rows, {}, now=datetime.now(timezone.utc), profile=profile)
    assert [row["key"] for row in ordered] == [rows[0]["key"], rows[1]["key"], rows[2]["key"]]


def test_interactive_refresh_passes_small_budgets_to_every_registered_employer(tmp_path):
    from venator.discover.run import run

    profile = load_profile(Path(__file__).parents[2] / "profiles" / "example")
    with patch(
        "venator.discover.run._process_board", return_value=(0, 0, "ok"),
    ) as process:
        run(profile, tmp_path, interactive=True)
    boards = list(process.call_args_list)
    assert len(boards) == sum(len(boards) for boards in profile.sources.boards.values())
    for call in boards:
        assert call.kwargs["listing_pages"] == 3
        assert call.kwargs["detail_limit"] == 20
