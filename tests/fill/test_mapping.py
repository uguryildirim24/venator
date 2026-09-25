from __future__ import annotations

from pathlib import Path

from venator.fill.mapping import map_field

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
CONSTRAINTS = {
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


def field(
    label: str,
    kind: str,
    options: list[str] | None = None,
) -> dict:
    return {
        "label": label,
        "kind": kind,
        "required": True,
        "selector": f"#{kind}",
        "options": options,
    }


def test_form_fields_exercise_every_kind_truthfully(tmp_path: Path) -> None:
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"profile-derived resume")
    fields = [
        field("First Name", "text"),
        field("Work Authorization Status", "textarea"),
        field("School", "select", ["Other", "Example University"]),
        field("Do you require sponsorship?", "radio", ["Yes", "No"]),
        field("Will you require sponsorship?", "checkbox"),
        field("Resume/CV", "file"),
    ]

    mappings = [
        map_field(item, RESUME, CONSTRAINTS, resume_file=resume_file) for item in fields
    ]

    assert [mapping.value for mapping in mappings] == [
        "Alex",
        "F-1 international student",
        "Example University",
        "Yes",
        True,
        str(resume_file),
    ]
    assert all(mapping.mapped for mapping in mappings)
    assert mappings[0].source == "resume.yaml:name"
    assert mappings[1].source == "constraints.yaml:work_authorization.status"
    assert mappings[2].source == "resume.yaml:education.0.org"
    assert mappings[3].source == "constraints.yaml:work_authorization.requires_sponsorship"


def test_sponsorship_radio_is_honest_and_never_maps_to_no() -> None:
    mapping = map_field(
        field("Do you require sponsorship?", "radio", ["No", "Yes"]),
        RESUME,
        CONSTRAINTS,
    )

    assert mapping.mapped
    assert mapping.value == "Yes"
    assert mapping.value != "No"


def test_choice_resolution_normalizes_options_but_rejects_semantic_near_misses() -> None:
    sponsorship = map_field(
        field(
            "Will you now or in the future require sponsorship?",
            "radio",
            ["Yes, sponsorship is required", "No"],
        ),
        RESUME,
        CONSTRAINTS,
    )
    school = map_field(
        field("School", "select", ["example university", "Other"]),
        RESUME,
        CONSTRAINTS,
    )

    assert not sponsorship.mapped
    assert sponsorship.reason == "Profile value 'Yes' is not an exact or canonical option"
    assert school.mapped
    assert school.value == "example university"


def test_binary_work_authorization_is_not_invented_from_f1_status() -> None:
    """A status describes; it does not attest.

    The Owner can now state the answer outright
    (``work_authorization.authorized_to_work``) and this Profile has not, so the
    question stays theirs to answer — the reason names the field to set, and
    says in as many words that silence is not a "No".
    """
    mapping = map_field(
        field(
            "Are you legally authorized to work in the United States?",
            "radio",
            ["Yes", "No"],
        ),
        RESUME,
        CONSTRAINTS,
    )

    assert not mapping.mapped
    assert "work_authorization.authorized_to_work" in mapping.reason
    assert "never answered 'No'" in mapping.reason


def test_unknown_label_is_explicitly_unmapped() -> None:
    mapping = map_field(field("Describe a scientific breakthrough", "textarea"), RESUME, CONSTRAINTS)

    assert not mapping.mapped
    assert mapping.reason == "no Profile fact covers this"
