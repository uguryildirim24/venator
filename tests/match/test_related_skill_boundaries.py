"""Related evidence must not certify specialization, credentials, or seniority."""
from pathlib import Path

import pytest

from venator.match.assessment import assess_posting
from venator.match.filters import apply_filters
from venator.profile import Profile
from venator.profile.schema import FilterPolicy


def assessment(tmp_path: Path, resume: dict, requirement: str, title: str = "Specialist") -> dict:
    candidate = Profile(name="fictional", directory=tmp_path, resume=resume, filters=FilterPolicy())
    posting = {
        "key": "greenhouse:fictional:boundaries", "source": "greenhouse",
        "title": title, "location": "Boston, MA", "company": "Fictional Employer",
        "description_html": f"<h2>Requirements</h2><p>{requirement}</p>",
        "listing_status": "open", "description_kind": "full",
        "last_verified_at": "2026-09-05T12:00:00Z", "opportunity_type": "job",
    }
    decision = apply_filters(posting, candidate.filters)
    return assess_posting(posting, candidate, {"stage": "hard_filter", **decision._asdict()})


@pytest.mark.parametrize("requirement", [
    "CNC programming experience required.",
    "PLC programming experience required.",
    "Neuro-linguistic programming experience required.",
    "Five years of programming experience required.",
    "3 years of programming experience required.",
    "Programming certification required.",
    "Medical coding experience required.",
    "Front-end development experience required.",
])
def test_python_does_not_certify_specialization_or_tenure(tmp_path: Path, requirement: str) -> None:
    result = assessment(tmp_path, {"skills": ["Python"]}, requirement)
    assert result["status"] == "needs_review", result
    assert not any(row.get("basis") == "related_skill" for row in result["evidence"]), result
    assert result["unknowns"]


def test_relationship_does_not_run_backwards(tmp_path: Path) -> None:
    result = assessment(tmp_path, {"skills": ["Programming"]}, "Python experience required.")
    assert result["status"] == "needs_review", result
    assert not any(row.get("basis") == "related_skill" for row in result["evidence"]), result


@pytest.mark.parametrize("bullet", [
    "Observed colleagues who built React web applications.",
    "Watched a demonstration of React web applications.",
    "Did not build React web applications.",
    "Built a prototype without React web applications.",
    "Built web applications using another framework; lack React experience.",
])
def test_observed_or_negated_work_cannot_support_related_skill(tmp_path: Path, bullet: str) -> None:
    result = assessment(tmp_path, {"experience": [{"bullets": [bullet]}]}, "Front-end development experience required.")
    assert result["status"] == "needs_review", result
    assert not any(row.get("basis") == "related_skill" for row in result["evidence"]), result


@pytest.mark.parametrize("bullet", [
    "Observed colleagues who built React web applications.",
    "Watched a demonstration of React web applications.",
    "Did not use React in any project.",
    "Built a prototype without React web applications.",
])
def test_observed_or_negated_technical_fact_cannot_create_exact_evidence(tmp_path: Path, bullet: str) -> None:
    result = assessment(tmp_path, {"experience": [{"bullets": [bullet]}]}, "React experience required.")
    assert result["status"] == "needs_review", result
    assert not any(row["source"].startswith("profile.resume.") for row in result["evidence"]), result


@pytest.mark.parametrize("bullet,requirement", [
    ("Did not use Python or React in any project.", "React experience required."),
    ("Observed Python and SQL workflows.", "SQL experience required."),
    ("No experience with Python, React or SQL.", "SQL experience required."),
])
def test_shared_negation_or_observation_scope_covers_conjoined_facts(tmp_path: Path, bullet: str, requirement: str) -> None:
    result = assessment(tmp_path, {"experience": [{"bullets": [bullet]}]}, requirement)
    assert result["status"] == "needs_review", result
    assert not any(row["source"].startswith("profile.resume.") for row in result["evidence"]), result


def test_observation_then_explicitly_owned_action_keeps_the_owned_fact(tmp_path: Path) -> None:
    result = assessment(
        tmp_path,
        {"experience": [{"bullets": ["Observed React web applications, but built Python scripts."]}]},
        "Programming experience required.",
    )
    assert result["status"] == "suitable", result
    assert any(row.get("basis") == "related_skill" for row in result["evidence"]), result


@pytest.mark.parametrize("bullet,expected", [
    ("Observed React web applications and built Python scripts.", True),
    ("Observed React web applications and they built Python scripts.", False),
])
def test_observation_scope_requires_an_owned_action_boundary(tmp_path: Path, bullet: str, expected: bool) -> None:
    result = assessment(tmp_path, {"experience": [{"bullets": [bullet]}]}, "Programming experience required.")
    has_related = any(row.get("basis") == "related_skill" for row in result["evidence"])
    assert has_related is expected, result


@pytest.mark.parametrize("resume,requirement,source", [
    ({"skills": ["Python"]}, "Programming experience required.", "profile.resume.skills[0][0]"),
    ({"education": [{"coursework": ["Python"]}]}, "Coding knowledge required.", "profile.resume.education[0].coursework[0]"),
    ({"projects": [{"bullets": ["Built web applications in React."]}]}, "Front-end development experience required.", "profile.resume.projects[0].bullets[0]"),
])
def test_plain_broader_work_retains_label_and_original_evidence(tmp_path: Path, resume: dict, requirement: str, source: str) -> None:
    result = assessment(tmp_path, resume, requirement)
    assert result["status"] == "suitable", result
    related = [row for row in result["evidence"] if row.get("basis") == "related_skill"]
    assert len(related) == 1, result
    assert related[0]["source"] == source
    assert related[0]["requirement"] in requirement
    assert not any("qualification" in item.casefold() or "resume evidence" in item.casefold() for item in result["unknowns"]), result


@pytest.mark.parametrize("tool", ["Excel", "Microsoft Office", "Google Workspace"])
def test_office_tools_can_establish_relevance_when_explicitly_the_work(tmp_path: Path, tool: str) -> None:
    result = assessment(tmp_path, {"skills": [tool]}, f"{tool} experience required.", title=f"{tool} Trainer")
    assert result["status"] == "suitable", result
    assert any(row["candidate_evidence"] == tool for row in result["evidence"])


@pytest.mark.parametrize("tool", ["Office", "MS Office", "M365", "Google Workspace"])
def test_supporting_tool_variants_do_not_establish_unrelated_fit(tmp_path: Path, tool: str) -> None:
    result = assessment(tmp_path, {"skills": [tool]}, f"{tool} experience required.", title="Operations Specialist")
    assert result["status"] == "needs_review", result
    assert any("supporting" in item.casefold() for item in result["unknowns"]), result


@pytest.mark.parametrize("requirement", [
    "5 years of coding and programming experience required.",
    "CNC programming and coding experience required.",
    "Programming certification and coding experience required.",
])
def test_relation_cannot_drop_shared_tenure_or_specialization_modifiers(tmp_path: Path, requirement: str) -> None:
    result = assessment(tmp_path, {"skills": ["Python"]}, requirement)
    assert result["status"] == "needs_review", result
    assert not any(row.get("basis") == "related_skill" for row in result["evidence"]), result
