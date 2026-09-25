from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

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
            "gpa": "3.86/4.00",
        }
    ],
}
BASE_CONSTRAINTS = {
    "work_authorization": {
        "status": "F-1 international student",
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


@pytest.mark.parametrize(
    ("label", "path"),
    [
        ("Salary Expectations", "screening.salary_expectations"),
        (
            "Do you reside in the location for this job posting or within commutable distance?",
            "screening.resides_near_posting",
        ),
        (
            "Have you ever been employed by or performed services for Example Company, a subsidiary, or an acquired company?",
            "screening.prior_employment_at_company",
        ),
        ("Do you have relatives at this company?", "screening.relatives_at_company"),
        ("How did you hear about us?", "screening.how_heard"),
        ("Country", "screening.country"),
        ("Gender", "screening.eeo.gender"),
        ("Are you Hispanic/Latino?", "screening.eeo.hispanic_latino"),
        ("Veteran Status", "screening.eeo.veteran_status"),
        ("Disability Status", "screening.eeo.disability_status"),
    ],
)
def test_absent_screening_answers_stay_unmapped(label: str, path: str) -> None:
    mapping = map_field(field(label, "text"), RESUME, BASE_CONSTRAINTS)

    assert not mapping.mapped
    assert mapping.reason == f"owner has not provided constraints.yaml:{path}"


def test_provided_screening_answers_map_exactly_or_by_normalized_membership() -> None:
    constraints = {
        **BASE_CONSTRAINTS,
        "screening": {
            "salary_expectations": "$45,000",
            "resides_near_posting": "yes",
            "prior_employment_at_company": False,
            "relatives_at_company": "No",
            "how_heard": "college university fair",
            "country": "united states 1",
            "eeo": {
                "gender": "decline to self identify",
                "hispanic_latino": None,
                "veteran_status": "I don't wish to answer",
                "disability_status": "I do not want to answer",
            },
        },
    }
    cases = [
        (field("Salary Expectations", "text"), "$45,000"),
        (field("Do you reside near the job location?", "select", ["Yes", "No"]), "Yes"),
        (field("Have you ever worked for this company?", "radio", ["Yes", "No"]), "No"),
        (field("Do you have relatives at this company?", "select", ["Yes", "No"]), "No"),
        (
            field(
                "How did you hear about us?",
                "select",
                ["College / University Fair", "Other"],
            ),
            "College / University Fair",
        ),
        (field("Country", "select", ["Canada +1", "United States +1"]), "United States +1"),
        (
            field("Gender", "select", ["Male", "Female", "Decline To Self Identify"]),
            "Decline To Self Identify",
        ),
        (
            field(
                "Veteran Status",
                "select",
                ["I am not a protected veteran", "I don't wish to answer"],
            ),
            "I don't wish to answer",
        ),
        (
            field(
                "Disability Status",
                "select",
                ["No, I do not have a disability", "I do not want to answer"],
            ),
            "I do not want to answer",
        ),
    ]

    for spec_field, expected in cases:
        mapping = map_field(spec_field, RESUME, constraints)
        assert mapping.mapped, mapping.reason
        assert mapping.value == expected

    hispanic = map_field(
        field("Are you Hispanic/Latino?", "select", ["Yes", "No", "Decline To Self Identify"]),
        RESUME,
        constraints,
    )
    assert not hispanic.mapped
    assert "owner has not provided" in hispanic.reason


def test_provided_screening_choice_stays_unmapped_without_canonical_membership() -> None:
    constraints = {
        **BASE_CONSTRAINTS,
        "screening": {"how_heard": "Online search"},
    }
    mapping = map_field(
        field("How did you hear about us?", "select", ["Through my network", "Other"]),
        RESUME,
        constraints,
    )

    assert not mapping.mapped
    assert mapping.reason == "Profile value 'Online search' is not an exact or canonical option"


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
        assert "owner has not provided" in mapping.reason


def test_screening_scaffold_is_declared_but_never_defaulted() -> None:
    """Every Profile carries the screening fields, and every one starts null.

    They are real optional fields now rather than commented-out scaffolding, so
    a Profile can be filled in without knowing the schema — but an absent
    answer must still leave a form field unmapped, never guessed.
    """
    for directory in sorted(Path("profiles").iterdir()):
        constraints = yaml.safe_load((directory / "constraints.yaml").read_text(encoding="utf-8"))
        screening = constraints["screening"]

        assert set(screening) == {
            "salary_expectations",
            "resides_near_posting",
            "prior_employment_at_company",
            "relatives_at_company",
            "how_heard",
            "country",
            "eeo",
        }, directory
        assert set(screening["eeo"]) == {
            "gender",
            "hispanic_latino",
            "veteran_status",
            "disability_status",
        }, directory
        assert all(value is None for key, value in screening.items() if key != "eeo"), directory
        assert all(value is None for value in screening["eeo"].values()), directory
