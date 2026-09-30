from __future__ import annotations

from venator.discover.adapters import normalize_ashby_job
from venator.match.location_scope import location_scope


def test_ashby_postal_country_survives_normalization() -> None:
    job = {"id": "uk-site", "title": "Analyst", "location": "Milton Park",
           "address": {"postalAddress": {"addressCountry": "United Kingdom",
                                        "addressLocality": "Abingdon"}}}
    posting = normalize_ashby_job("example", job)
    assert posting["location"] == "Milton Park"
    assert posting["locations"][0]["country"] == "GB"
    assert posting["source_facts"]["address"] == job["address"]


def test_ashby_foreign_primary_keeps_explicit_us_secondary_site() -> None:
    job = {"id": "multiple-sites", "title": "Analyst", "location": "Berlin",
           "address": {"postalAddress": {"addressCountry": "Germany"}},
           "secondaryLocations": [{"location": "Boston, MA", "country": "US"}]}
    posting = normalize_ashby_job("example", job)
    assert [site["country"] for site in posting["locations"]] == ["DE", "US"]
    assert location_scope(posting, employer_has_local=False) == "ma"
    assert location_scope(posting, physical_only=True) == "ma"
