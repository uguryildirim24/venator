"""View retains real ATS places when display labels say only 'N Locations'."""

from __future__ import annotations

import json
from pathlib import Path

from venator.view.build import _location_places


FIXTURES = Path(__file__).parents[1] / "discover" / "fixtures"


def test_workday_additional_locations_survive_into_view() -> None:
    listing = json.loads((FIXTURES / "workday_listing.json").read_text())
    job = listing["jobPostings"][1]
    posting = {"location": "3 Locations", "locations": [{"name": "3 Locations"}], "source_facts": {"jobPostingInfo": job}}
    names = json.loads(_location_places(posting))
    assert "US - Cambridge, MA" in names
    assert "US - Thousand Oaks, CA" in names
