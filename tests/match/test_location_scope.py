"""The Posting's sites, not shared city names, decide geographic scope."""
from __future__ import annotations

import pytest

from venator.match.filters import location_reading
from venator.match.location_scope import location_scope
from venator.profile.schema import FilterPolicy


@pytest.mark.parametrize(("place", "expected"), [
    ("Cambridge, MA USA", "ma"),
    ("Holyoke MA", "ma"),
    ("Lexington, Massachusetts, United States", "ma"),
    ("USA - Pennsylvania - North Wales", "outside"),
    ("Linthicum MD", "outside"),
    ("Concord NH", "new_england"),
    ("Concord, NC", "outside"),
    ("Lebanon, NH", "new_england"),
    ("New York, Connecticut", "new_england"),
    ("Cambridge, UK", "outside"),
    ("Gurabo, Puerto Rico, United States of America", "outside"),
    ("San Juan, Puerto Rico", "outside"),
    ("Cambridge MN", "outside"),
    ("Burlington, VT", "new_england"),
    ("Burlington NC", "outside"),
    ("Schönenwerd, SO, Switzerland", "outside"),
    ("Ho Chi Minh, Tan Phu, Vietnam", "outside"),
    ("Washington University Medical Campus", "unreadable"),
    ("Lebanon, PA", "outside"),
    ("Germany-Waldbronn", "outside"),
    ("2 Locations", "unreadable"),
    ("Remote", "remote"),
    ("Remote, Brazil", "outside"),
    ("Remote, Germany", "outside"),
    ("Remote, Serbia", "outside"),
    ("Remote, United Kingdom", "outside"),
    ("KOR-Remote", "outside"),
    ("AUS-Remote", "outside"),
    ("BRA-Remote", "outside"),
    ("CHN-Remote", "outside"),
    ("ARG-Remote", "outside"),
    ("ITA-Remote", "outside"),
    ("GBR-Remote", "outside"),
    ("Brazil-Remote Location-Araguaia", "outside"),
    ("Remote, USA", "remote"),
    ("United States - Remote", "remote"),
    ("US-Remote", "remote"),
    ("Remote Colorado", "outside"),
    ("Remote, Minnesota, USA", "outside"),
    ("Remote Texas", "outside"),
    ("Remote New Mexico", "outside"),
    ("Remote Massachusetts", "ma"),
    ("Remote - MA", "ma"),
    ("Remote, Rhode Island, USA", "new_england"),
    ("Remote; Boston, MA", "ma"),
    ("Tokyo", "outside"),
    ("Tokyo (NPKK Sales)", "outside"),
    ("Seoul", "outside"),
    ("Basel", "outside"),
    ("Beijing", "outside"),
    ("Frankfurt am Main", "outside"),
    ("Ljubljana", "outside"),
    ("Montevideo", "outside"),
    ("Grenzach", "unreadable"),
    ("Kaiseraugst", "outside"),
    ("Vitry-sur-Seine", "outside"),
    ("City of Singapore", "outside"),
    ("Exeter Hospital", "new_england"),
    ("Anna Jaques Hospital", "ma"),
    ("Beth Israel Deaconess Hospital Plymouth", "ma"),
    ("Winchester Hospital", "ma"),
    ("Maddock Alumni Center", "new_england"),
    ("Mount Auburn Hospital", "ma"),
    ("Cambridge", "ma"),
    ("London", "outside"),
    ("Lisbon", "new_england"),  # also a town in Maine
    ("Berlin", "ma"),  # also a New England town
    ("Boston Job Posting Location", "ma"),
    ("South Waltham, Middlesex County", "ma"),
    ("US, Lenexa KCIB (PRA)", "outside"),
    ("Remote, Korea, Republic of", "outside"),
    ("USA", "remote"),
    ("", "unreadable"),
    ("Cambridge, UK; Concord NH", "new_england"),
    ("Concord, NC | Burlington, VT", "new_england"),
])
def test_site_scope(place: str, expected: str) -> None:
    assert location_scope({"location": place}) == expected


def test_structured_sites_and_unconfigured_profile() -> None:
    posting = {"location": "2 Locations", "locations": [
        {"name": "Concord", "region": "NC", "country": "US"},
        {"name": "Burlington", "region": "VT", "country": "US"},
    ]}
    assert location_scope(posting) == "new_england"
    assert location_scope({"location": "3 Locations", "locations": [
        "3 Locations", "Raritan, New Jersey, United States of America",
        "Horsham, Pennsylvania, United States of America",
    ]}) == "outside"
    assert location_scope({"location": "2 Locations", "locations": [
        {"name": "Berlin", "country": "DE"}, {"name": "Boston", "region": "MA", "country": "US"},
    ]}) == "ma"
    assert location_scope({"location": "2 Locations", "locations": ["2 Locations"]}) == "unreadable"
    assert location_scope({"location": "2 Locations", "locations": [
        {"name": "Remote", "country": "DE"},
    ]}) == "outside"
    assert location_scope({"locations": ["Durham; Remote Colorado"], "title": "Technician / Durham, NC"}) == "outside"
    assert location_scope({"locations": ["Remote Texas; Remote New Mexico"]}) == "outside"
    assert location_scope({"locations": ["Remote Colorado", "Boston, MA"]}) == "ma"
    assert location_scope({"locations": [{"name": "Remote", "region": "CO", "country": "US"}]}) == "outside"
    assert location_scope({"location": "2 Locations", "locations": [
        {"name": "2 Locations"},
    ], "source_facts": {"jobPostingInfo": {
        "locationsText": "2 Locations", "additionalLocations": [
            "Aachen, Germany", "Berlin, CT",
        ],
    }}}) == "new_england"
    assert location_scope({"location": "2 Locations", "source_facts": {
        "jobPostingInfo": {"locationsText": "2 Locations", "additionalLocations": [
            "Aachen, Germany", "Basel, Switzerland",
        ]},
    }}) == "outside"
    # Historical Postings can carry source sites without a structured list.
    # View recovers those sites even when the display is not a count.
    assert location_scope({"location": "Concord, NC", "source_facts": {
        "jobPostingInfo": {"additionalLocations": ["Boston, MA"]},
    }}) == "ma"
    assert location_reading(posting, FilterPolicy()).verdict == "pass"
    policy = FilterPolicy(enabled=("location",), location_regions=("MA", "RI", "NH", "CT", "VT", "ME"))
    assert location_reading({"location": "Concord, NC"}, policy).verdict == "kill"
    assert location_reading({"location": "Remote, Germany"}, policy).verdict == "kill"
    assert location_reading({"location": "Tokyo"}, policy).verdict == "kill"
    assert location_reading({"location": "Exeter Hospital"}, policy).verdict == "pass"
    assert location_reading({"location": "2 Locations"}, policy).fact == "unreadable"
    assert location_reading({"location": "Remote"}, policy).fact == "remote"
    assert location_scope({"location": "Washington University Medical Campus"}, employer_has_local=False) == "outside"
    assert location_scope({"location": "Washington University Medical Campus"}, employer_has_local=True) == "unreadable"
    assert location_scope({"location": "11 Locations"}, employer_has_local=False) == "unreadable"
    assert location_scope({"location": "Boston-MA"}) == "ma"
    assert location_scope({"location": "MA-Boston"}) == "ma"


@pytest.mark.parametrize(("name", "country"), [
    ("Berlin", "DE"), ("Marcy-l'Etoile", "FR"), ("Marburg - Office", "DE"),
    ("Milton Park", "GB"), ("2 Locations", "DE"), ("4 Locations", "DE"),
    ("Remote", "GB"), ("Berlin; Grenzach", "DE"),
])
def test_explicit_foreign_country_cannot_be_vouched_for(name: str, country: str) -> None:
    posting = {"location": name, "locations": [{"name": name, "country": country}],
               "title": "Technician (Boston, MA)"}
    assert location_scope(posting, employer_has_local=True) == "outside"
    assert location_scope(posting, physical_only=True) == "outside"
    assert location_scope({"location": name, "country": country}, employer_has_local=True) == "outside"


@pytest.mark.parametrize("facts", [
    {"jobPostingInfo": {"jobRequisitionLocation": {"country": {"alpha2Code": "DE"}}}},
    {"jobPostingInfo": {"country": {"descriptor": "France"}}},
    {"address": {"postalAddress": {"addressCountry": "United Kingdom"}}},
])
def test_stored_source_country_overrides_empty_normalized_country(facts: dict) -> None:
    posting = {"location": "Berlin", "locations": [{"name": "Berlin", "country": ""}],
               "source_facts": facts, "title": "Technician (Boston, MA)"}
    assert location_scope(posting, employer_has_local=True) == "outside"
    assert location_scope(posting, physical_only=True) == "outside"


def test_foreign_primary_country_preserves_explicit_us_secondary_sites() -> None:
    facts = {"jobPostingInfo": {
        "jobRequisitionLocation": {"country": {"alpha2Code": "DE"}},
        "locationsText": "2 Locations",
        "additionalLocations": [{"name": "Boston", "region": "MA", "country": "US"}],
    }}
    for locations in ([], [
        {"name": "Berlin", "country": "DE"},
        {"name": "Boston", "region": "MA", "country": "US"},
    ]):
        posting = {"location": "2 Locations", "locations": locations, "source_facts": facts}
        assert location_scope(posting, employer_has_local=False) == "ma"
        assert location_scope(posting, physical_only=True) == "ma"


def test_recovered_source_sites_retain_their_countries() -> None:
    posting = {"location": "2 Locations", "source_facts": {"jobPostingInfo": {
        "additionalLocations": [{"name": "Berlin", "country": "DE"}],
    }}}
    assert location_scope(posting, employer_has_local=True) == "outside"
    assert location_scope(posting, physical_only=True) == "outside"


def test_title_disambiguates_only_unknown_or_shared_sites() -> None:
    assert location_scope({"location": "Madison", "title": "Research Technician / Madison, WI (On-Site)"}) == "outside"
    assert location_scope({"location": "Unknown Campus", "title": "Technician - Marlborough, MA"}) == "ma"
    assert location_scope({"location": "Unknown Campus", "title": "Technician (Boston, MA)"}) == "ma"
    assert location_scope({"location": "Concord, NC", "title": "Technician (Boston, MA)"}) == "outside"
    assert location_scope({"location": "Madison", "title": "Technician (Madison)"}) == "new_england"
    assert location_scope({"locations": ["Boston, MA", "Madison"], "title": "Technician / Madison, WI"}) == "ma"
    assert location_scope({"locations": ["Remote, USA", "Madison"], "title": "Technician / Madison, WI"}) == "new_england"
    assert location_scope({"locations": [{"name": "Madison", "country": "US"}], "title": "Technician / Madison, WI"}) == "outside"
    for hospital in ("Newport Hospital", "Rhode Island Hospital", "The Miriam Hospital", "Bradley Hospital"):
        assert location_scope({"location": hospital}) == "new_england"
