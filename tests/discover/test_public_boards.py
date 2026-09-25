from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.discover import public_boards as feeds
from venator.discover.refresh import refresh_posting
from venator.discover.harvest import extract_board_tokens
from venator.discover.register import board_from_url

FIXTURE = json.loads((Path(__file__).parent / "fixtures/public_boards.json").read_text())


def test_job_ld_preserves_encoded_quotes_in_description():
    job = {"@type": "JobPosting", "title": "Lab Assistant", "description": "Use &quot;sterile&quot; tools"}
    script = '<script type="application/ld+json">' + json.dumps(job) + "</script>"
    assert feeds._job_ld(script, "icims", "example.icims.com")["description"] == job["description"]


def test_talentbrew_literal_tabs_are_escaped_only_inside_json_strings():
    script = ('<script type="application/ld+json">{\t"@type":"JobPosting",'
              '"title":"Engineer","description":"<ul>\t<li>Role</li></ul>"}</script>')
    job = feeds._job_ld(script, "talentbrew", "careers.questdiagnostics.com")
    assert job["description"] == "<ul>\t<li>Role</li></ul>"
    with pytest.raises(feeds.BoardResponseError, match="malformed"):
        feeds._job_ld(script, "icims", "example.icims.com")
    with pytest.raises(feeds.BoardResponseError, match="malformed"):
        feeds._job_ld(script.replace("\t", "\x00"), "talentbrew", "careers.questdiagnostics.com")


def test_sitemap_details_and_partial(monkeypatch):
    monkeypatch.setattr(feeds.time, "sleep", lambda _: None)
    for source, fetch in (("icims", feeds.fetch_icims), ("peopleclick", feeds.fetch_peopleclick), ("talentbrew", feeds.fetch_talentbrew)):
        fixture = FIXTURE[source]
        def read(url, *_):
            if url.endswith("sitemap.xml"):
                return fixture["sitemap"]
            return '<script type="application/ld+json">' + json.dumps(fixture["job"]) + "</script>"
        monkeypatch.setattr(feeds, "_get_text", read)
        batch = fetch(fixture["board"])
        assert batch.complete and len(batch) == 1
        assert batch[0]["description_kind"] == "full"
        assert batch[0]["key"].startswith(f"{source}:{fixture['board']}:")
        assert board_from_url(fixture["url"]) == [(source, fixture["board"])]
        monkeypatch.setattr(feeds, "_get_text", lambda url, *_: fixture["sitemap"] if url.endswith("sitemap.xml") else "<html>missing</html>")
        partial = fetch(fixture["board"])
        assert not partial.complete and partial.status == "partial" and not partial


def test_refresh_reads_only_the_exact_detail(monkeypatch):
    for source in ("icims", "peopleclick", "talentbrew", "avature"):
        fixture = FIXTURE[source]
        def read(url, *_):
            assert "sitemap.xml" not in url and "SearchJobs" not in url
            if source == "avature":
                return (f'<title>{fixture["title"]} -  - 22538 - Broad Institute</title>'
                        + '<script>var legacyViewCopilotData = ' + json.dumps(fixture["facts"]) + ';</script>')
            return '<script type="application/ld+json">' + json.dumps(fixture["job"]) + "</script>"
        monkeypatch.setattr(feeds, "_get_text", read)
        original = {"source": source, "board": fixture["board"], "url": fixture["url"],
                    "external_id": "22538" if source == "avature" else
                    ("14045" if source == "icims" else "34879" if source == "peopleclick" else "100137762224")}
        original["key"] = f'{source}:{original["board"]}:{original["external_id"]}'
        refreshed = refresh_posting(original)
        assert refreshed["verification_status"] == "verified"
        assert refreshed["key"] == original["key"]
        assert refresh_posting({**original, "external_id": "wrong"})["verification_status"] == "unknown"


def test_workable_full_feed_and_malformed_row(monkeypatch):
    fixture = FIXTURE["workable"]
    monkeypatch.setattr(feeds, "_get_json", lambda *a, **k: fixture["feed"])
    batch = feeds.fetch_workable(fixture["board"])
    assert batch.complete and len(batch) == 1
    assert batch[0]["description_kind"] == "full"
    assert batch[0]["external_id"] == fixture["feed"]["jobs"][0]["shortcode"]
    assert ("workable", "editas") in extract_board_tokens("https://apply.workable.com/editas/")
    monkeypatch.setattr(feeds, "_get_json", lambda *a, **k: {"jobs": [{}]})
    with pytest.raises(feeds.BoardResponseError):
        feeds.fetch_workable("editas")


def test_avature_search_and_detail(monkeypatch):
    fixture = FIXTURE["avature"]
    url = fixture["url"]
    monkeypatch.setattr(feeds.time, "sleep", lambda _: None)
    def read(request, *_):
        if "SearchJobs" in request:
            return f'<div class="results results--listed"><a href="{url}">Job</a></div>'
        return (f'<title>{fixture["title"]} -  - 22538 - Broad Institute</title>'
                + '<script>var legacyViewCopilotData = ' + json.dumps(fixture["facts"]) + ';</script>')
    monkeypatch.setattr(feeds, "_get_text", read)
    batch = feeds.fetch_avature("broadinstitute")
    assert batch.complete and len(batch) == 1
    assert batch[0]["description_kind"] == "full"
    assert batch[0]["title"] == fixture["title"]
    assert board_from_url(url) == [("avature", "broadinstitute")]
    monkeypatch.setattr(feeds, "_get_text", lambda request, *_: read(request) if "SearchJobs" in request else "<html>missing detail</html>")
    partial = feeds.fetch_avature("broadinstitute")
    assert not partial.complete and partial.status == "partial" and not partial
