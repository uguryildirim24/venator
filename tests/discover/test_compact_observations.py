import json

import pytest

import venator.discover.store as posting_store
from venator.discover.store import refresh_postings, load_postings
from venator.match.store import load_postings as matching_postings
from venator.discover.adapters import workday_pages, PartialBoardError, WorkdayError


def test_unchanged_refresh_does_not_copy_the_description_and_both_readers_agree(tmp_path):
    job = {"key": "greenhouse:acme:1", "source": "greenhouse", "board": "acme", "title": "Role",
           "description_html": "Full job description " * 1000, "description_kind": "full"}
    refresh_postings(tmp_path, [job], observed_at="2026-09-04T00:00:00Z")
    refresh_postings(tmp_path, [job], observed_at="2026-09-05T00:00:00Z")
    rows = [json.loads(line) for path in tmp_path.glob("*.jsonl") for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert len(json.dumps(rows[1])) < 200
    assert "description_html" not in rows[1]
    current = load_postings(tmp_path)
    assert current == matching_postings(tmp_path)
    assert current[0]["description_html"] == job["description_html"]
    assert current[0]["last_verified_at"] == "2026-09-05T00:00:00Z"
    assert current[0]["first_seen_at"] == "2026-09-04T00:00:00Z"
    refresh_postings(
        tmp_path,
        [{**job, "title": "Renamed role"}],
        observed_at="2026-09-06T00:00:00Z",
    )
    refresh_postings(
        tmp_path,
        [{**job, "title": "Renamed role", "description_html": "Changed requirements"}],
        observed_at="2026-09-07T00:00:00Z",
    )
    rows = [
        json.loads(line)
        for path in sorted(tmp_path.glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    assert rows[2]["_observation"]["title"] == "Renamed role"
    assert "description_html" not in rows[2]["_observation"]
    assert rows[3]["_observation"]["description_html"] == "Changed requirements"
    assert load_postings(tmp_path)[0]["description_html"] == "Changed requirements"


def test_legacy_description_html_loads_as_full_text(tmp_path):
    row = {
        "key": "workday:jj.wd5~JJ:R-1",
        "source": "workday",
        "board": "jj.wd5~JJ",
        "description_html": "<p>Complete historical description.</p>",
    }
    (tmp_path / "2026-08-31.jsonl").write_text(json.dumps(row) + "\n")
    assert load_postings(tmp_path)[0]["description_kind"] == "full"
    assert matching_postings(tmp_path)[0]["description_kind"] == "full"


def test_a_compact_record_cannot_change_reserved_store_fields(tmp_path):
    (tmp_path / "2026-09-04.jsonl").write_text(json.dumps({"key": "p:1"}) + "\n" + json.dumps({
        "key": "p:1", "_observation": {"key": "p:2"},
    }) + "\n")
    with pytest.raises(ValueError, match="Invalid Posting observation"):
        load_postings(tmp_path)


def test_posting_store_splits_before_the_daily_file_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(posting_store, "POSTING_PART_MAX_BYTES", 260)
    jobs = [
        {
            "key": f"greenhouse:acme:{number}",
            "source": "greenhouse",
            "board": "acme",
            "title": f"Role {number}",
        }
        for number in range(4)
    ]

    refresh_postings(tmp_path, jobs, observed_at="2026-09-23T00:00:00Z")

    paths = sorted(tmp_path.glob("*.jsonl"))
    assert len(paths) > 1
    assert paths[0].stem.count("_") == 0
    assert all(path.stat().st_size <= posting_store.POSTING_PART_MAX_BYTES for path in paths)
    assert [job["key"] for job in load_postings(tmp_path)] == [job["key"] for job in jobs]


def test_one_missing_workday_id_does_not_discard_other_valid_jobs():
    valid = {"title": "Scientist", "externalPath": "/job/US/Scientist_R-1", "bulletFields": ["R-1"]}
    broken = {"title": "Missing identifier", "externalPath": "/job/US/Other", "bulletFields": []}
    result = workday_pages("lilly.wd115~LLY", lambda limit, offset: {"total": 2, "jobPostings": [valid, broken]})
    assert len(result) == 1
    assert result.complete is False
    assert result.status == "partial"
    assert "identity" in result.error


def test_workday_page_outage_preserves_the_successful_prefix():
    valid = {"title": "Scientist", "externalPath": "/job/US/Scientist_R-1", "bulletFields": ["R-1"]}
    def page(limit, offset):
        if offset:
            raise WorkdayError("temporary outage")
        return {"total": 2, "jobPostings": [valid]}
    with pytest.raises(PartialBoardError) as failure:
        workday_pages("lilly.wd115~LLY", page, limit=1)
    assert len(failure.value.postings) == 1


def test_workday_country_correction_survives_compact_observations(tmp_path):
    from venator.discover.adapters import normalize_workday_listing, normalize_workday_detail
    listing = normalize_workday_listing("roche.wd3~roche-ext", {"jobPostings": [{
        "title": "Fixture intern", "externalPath": "/job/Petaling-Jaya/Fixture_202601-100377",
        "locationsText": "Petaling Jaya", "bulletFields": ["202601-100377"],
    }]})[0]
    refresh_postings(tmp_path, [listing], observed_at="2026-09-04T00:00:00Z")
    detail = normalize_workday_detail(listing, {"jobPostingInfo": {
        "jobReqId": listing["external_id"],
        "location": "Petaling Jaya", "jobDescription": "Synthetic full description.",
        "country": {"descriptor": "Malaysia"}, "jobRequisitionLocation": {"country": {"alpha2Code": "MY"}},
    }})
    refresh_postings(tmp_path, [detail], observed_at="2026-09-05T00:00:00Z")
    refresh_postings(tmp_path, [detail], observed_at="2026-09-06T00:00:00Z")
    current = load_postings(tmp_path)[0]
    assert current["locations"] == [{"name": "Petaling Jaya", "country": "MY", "region": "", "city": "Petaling Jaya"}]
    assert current["description_html"] == "Synthetic full description."
    assert current["last_verified_at"] == "2026-09-06T00:00:00Z"
    assert matching_postings(tmp_path)[0]["locations"] == current["locations"]
