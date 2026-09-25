"""A second, non-biotech Profile filters and discovers differently.

profiles/example is a mid-level backend engineer in Chicago with unrestricted
work authorization. Run against the same Postings, it must reach different
verdicts from the Owner's biotech-internship Profile — otherwise the wording
would still be coming from the code rather than from the Profile.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from venator.match.filters import apply_filters, role_target_filter, work_authorization_filter
from venator.profile import load_profile

REPOSITORY = Path(__file__).parents[2]
OWNER = load_profile(REPOSITORY / "profiles" / "example")
BACKEND = load_profile(REPOSITORY / "profiles" / "example")


def posting(**changes: object) -> dict:
    value = {
        "key": "greenhouse:acme:1",
        "title": "Backend Engineer",
        "location": "Boston, MA",
        "description_html": "<p>A role.</p>",
    }
    value.update(changes)
    return value
