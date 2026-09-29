from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.fill.mapping import (
    fact_name_for_label,
    map_field,
)

RESUME = {
    "name": 'Alex Example',
    "contact": {
        "location": "Example City, MA",
        "phone": "555-010-1234",
        "email": "alex@example.test",
        "linkedin": "www.linkedin.com/in/alex-example",
    },
    "education": [
        {
            "org": "Example University",
            "date": "May 2027 (Expected)",
            "degree": "Bachelor of Science in Biochemistry",
            "gpa": "3.50/4.00",
        }
    ],
}
BASE_CONSTRAINTS = {
    "work_authorization": {
        "status": "international student",
        "requires_sponsorship": True,
    },
    "option_aliases": {
        "school": {"Example University": ["Example College"]},
        "degree": {"Bachelor of Science in Biochemistry": ["Bachelor's Degree"]},
        "sponsorship": {"Yes": ["Yes, but not until the future"]},
    },
}


def field(label: str, kind: str, options: list[str] | None = None) -> dict:
    return {
        "label": label,
        "kind": kind,
        "required": True,
        "selector": "#field",
        "options": options,
    }


def test_washington_school_word_order_collision_stays_unmapped_without_alias() -> None:
    resume = {
        **RESUME,
        "education": [{"org": "Washington University", "degree": "B.S."}],
    }
    options = ["Other", "University of Washington", "Washington State University"]

    mapping = map_field(field("School", "select", options), resume, BASE_CONSTRAINTS)

    assert not mapping.mapped
    assert mapping.value is None
    assert mapping.reason == (
        "Profile value 'Washington University' only matches after unsafe school word "
        "reordering: 'University of Washington'; add an explicit school option alias"
    )


def test_realistic_greenhouse_school_fixture_resolves_example() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "greenhouse-school-options.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    mapping = map_field(
        field("School", "select", fixture["greenhouse_school_options"]),
        RESUME,
        BASE_CONSTRAINTS,
    )

    assert mapping.mapped
    assert mapping.value == "Example College"


def test_structurally_equivalent_school_options_stay_unmapped_when_ambiguous() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "greenhouse-school-options.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    resume = {
        **RESUME,
        "education": [{"org": "University of Northbridge", "degree": "B.S."}],
    }
    mapping = map_field(
        field(
            "School",
            "select",
            fixture["ambiguous_school_options"],
        ),
        resume,
        BASE_CONSTRAINTS,
    )

    assert not mapping.mapped
    assert mapping.value is None
    assert mapping.reason == (
        "Profile value 'University of Northbridge' matches multiple canonical options after "
        "normalization: 'University-of-Northbridge', 'UNIVERSITY OF NORTHBRIDGE!'"
    )


def test_explicit_school_and_degree_tables_resolve_known_canonical_names() -> None:
    school = map_field(
        field("School", "select", ["Example College", "Other"]),
        RESUME,
        BASE_CONSTRAINTS,
    )
    degree = map_field(
        field("Degree", "select", ["Bachelor's Degree", "Other"]),
        RESUME,
        BASE_CONSTRAINTS,
    )

    assert school.mapped and school.value == "Example College"
    assert degree.mapped and degree.value == "Bachelor's Degree"


@pytest.mark.parametrize(
    "label",
    [
        "Do you now or will you in the future need employer sponsorship to work in the United States?",
        "Do you now—or will you in the future—need employer sponsorship to work in the United States?",
        "Do you now or will you in the future need employer sponsorship for a visa to work in the country where the job is open?",
        "Will you now or in the future need employer sponsorship to work in the United States?",
        "Will you need employer sponsorship now or later?",
        "Do you require visa sponsorship now or at a later date?",
    ],
)
def test_observed_sponsorship_phrasings_only_map_to_an_affirmative_option(label: str) -> None:
    mapping = map_field(
        field(
            label,
            "select",
            ["Yes, I will need sponsorship support now", "Yes, but not until the future", "No"],
        ),
        RESUME,
        BASE_CONSTRAINTS,
    )

    assert mapping.mapped
    assert mapping.value == "Yes, but not until the future"
    assert not str(mapping.value).casefold().startswith("no")


@pytest.mark.parametrize(
    "label",
    [
        "Need employer sponsorship?",
        "Require sponsorship?",
        "Do you require sponsorship now or in the future?",
        "Will you now or in the future require sponsorship to work in the United States?",
        "Will you now or in the future require employer sponsorship?",
    ],
)
def test_sponsorship_aliases_map_only_from_the_stated_profile_fact(label: str) -> None:
    constraints = {"work_authorization": {"requires_sponsorship": True}}

    mapping = map_field(field(label, "radio", ["Yes", "No"]), RESUME, constraints)

    assert mapping.mapped
    assert mapping.value == "Yes"
    assert mapping.source == "constraints.yaml:work_authorization.requires_sponsorship"

    unstated = map_field(
        field(label, "radio", ["Yes", "No"]),
        RESUME,
        {"work_authorization": {"requires_sponsorship": False}},
    )
    assert not unstated.mapped
    assert unstated.value is None


@pytest.mark.parametrize(
    "label",
    [
        "Do you have any sponsored research grants requiring disclosure?",
        "Do you have a colleague who requires visa sponsorship?",
        "This position may require you to sponsor a colleague's visa",
        "Sponsored research",
    ],
)
def test_non_sponsorship_sponsor_phrases_do_not_route_to_sponsorship(label: str) -> None:
    assert fact_name_for_label(label) is None

    mapping = map_field(field(label, "radio", ["Yes", "No"]), RESUME, BASE_CONSTRAINTS)

    assert not mapping.mapped
    assert mapping.value is None
    assert mapping.reason == "no Profile fact covers this"


def test_sponsorship_never_falls_back_to_no() -> None:
    mapping = map_field(
        field("Do you need employer sponsorship now or in the future?", "select", ["No"]),
        RESUME,
        BASE_CONSTRAINTS,
    )

    assert not mapping.mapped
    assert mapping.value is None


def test_eeo_is_never_defaulted_to_decline() -> None:
    labels_and_options = [
        ("Gender", ["Male", "Female", "Decline To Self Identify"]),
        ("Are you Hispanic/Latino?", ["Yes", "No", "Decline To Self Identify"]),
        ("Veteran Status", ["I am not a protected veteran", "I don't wish to answer"]),
        ("Disability Status", ["No", "I do not want to answer"]),
    ]

    for label, options in labels_and_options:
        mapping = map_field(field(label, "select", options), RESUME, BASE_CONSTRAINTS)
        assert not mapping.mapped
        assert mapping.value is None
        assert mapping.reason == "reserved question left for the Owner"
