from pathlib import Path
from unittest.mock import patch

import httpx

from venator.discover.adapters import ADAPTERS, fetch_smartrecruiters, normalize_smartrecruiters_job
from venator.discover.run import _process_board
from venator.discover.store import latest_postings


def test_smartrecruiters_detail_sections_become_full_posting_text():
    detail = {
        "id": "7440001",
        "name": "Research Associate",
        "releasedDate": "2026-09-23T12:00:00Z",
        "postingUrl": "https://jobs.smartrecruiters.com/Example/7440001-research-associate",
        "applyUrl": "https://jobs.smartrecruiters.com/Example/7440001-research-associate?oga=true",
        "location": {
            "fullLocation": "Cambridge, Massachusetts, United States",
            "city": "Cambridge",
            "region": "Massachusetts",
            "country": "us",
            "hybrid": True,
        },
        "jobAd": {
            "sections": {
                "jobDescription": {"title": "Job Description", "text": "<p>Run assays.</p>"},
                "qualifications": {"title": "Qualifications", "text": "<p>Bachelor's degree.</p>"},
            }
        },
    }

    posting = normalize_smartrecruiters_job("Example", detail)

    assert posting["key"] == "smartrecruiters:Example:7440001"
    assert posting["description_kind"] == "full"
    assert "Run assays" in posting["description_html"]
    assert "Bachelor's degree" in posting["description_html"]
    assert posting["workplace_type"] == "hybrid"
    assert posting["published_at"] == "2026-09-23T12:00:00Z"
    assert "smartrecruiters" in ADAPTERS


def test_unavailable_or_wrong_exact_detail_does_not_erase_listed_postings(tmp_path: Path):
    summaries = [
        {"id": "1", "name": "Lab Assistant", "postingUrl": "https://jobs.smartrecruiters.com/Example/1"},
        {"id": "2", "name": "Research Associate", "postingUrl": "https://jobs.smartrecruiters.com/Example/2"},
    ]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/postings"):
            return httpx.Response(200, json={"totalFound": 2, "content": summaries})
        if request.url.path.endswith("/1"):
            return httpx.Response(503)
        return httpx.Response(200, json={
            "id": "not-2", "name": "Wrong job",
            "jobAd": {"sections": {"jobDescription": {"text": "Wrong description"}}},
        })

    client = httpx.Client(transport=httpx.MockTransport(answer))
    with patch("venator.discover.adapters.httpx.Client", return_value=client):
        batch = fetch_smartrecruiters("Example")
    assert batch.status == "partial"
    assert {row["external_id"] for row in batch} == {"1", "2"}
    assert all(row["description_kind"] == "missing" for row in batch)
    _process_board(tmp_path, "smartrecruiters", "Example", lambda _: batch)
    stored = latest_postings(tmp_path)
    assert set(stored) == {"smartrecruiters:Example:1", "smartrecruiters:Example:2"}
    assert all(row["verification_status"] == "unknown" for row in stored.values())
    assert all(row["listing_status"] == "open" for row in stored.values())
