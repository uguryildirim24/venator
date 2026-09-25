"""Small, fictional cross-profession diagnostic; labels describe facts, not scores.

A recommendation means relevant evidence with no established blocker. A missing
skill or credential is uncertainty, never proof the candidate cannot do the job.
Every case runs the real hard filters, without loading an on-disk user profile.
"""
from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

import pytest

from venator.match.assessment import assess_posting
from venator.match.filters import apply_filters
from venator.profile import Profile
from venator.profile.schema import FilterPolicy, Matcher, RoleTargetPolicy, WorkAuthorizationPolicy


# These people and their employers are invented solely for this diagnostic.
PROFILES = {
    "fictional_developer": {
        "skills": ["Python", "React", "Excel"],
        "experience": [{"role": "Software Developer", "bullets": ["Built web applications in React and Python."]}],
    },
    "fictional_marketer": {
        "skills": ["Email marketing", "Campaign reporting", "Excel"],
        "experience": [{"role": "Marketing Coordinator", "bullets": ["Planned email marketing campaigns."]}],
    },
    "fictional_lab_technician": {
        "skills": ["Sample preparation", "Pipetting", "Excel"],
        "experience": [{"role": "Laboratory Technician", "bullets": ["Performed sample preparation and pipetting."]}],
    },
    "fictional_nurse": {
        "skills": ["Patient assessment", "Medication administration", "Excel"],
        "certifications": ["Massachusetts registered nurse license"],
        "experience": [{"role": "Registered Nurse", "bullets": ["Provided patient assessment and medication administration."]}],
    },
}

JOBS = {
    "fictional_developer": ("Software Developer", "Build web applications.", "Python experience required."),
    "fictional_marketer": ("Marketing Coordinator", "Plan email marketing campaigns.", "Email marketing experience required."),
    "fictional_lab_technician": ("Laboratory Technician", "Prepare laboratory samples.", "Sample preparation experience required."),
    "fictional_nurse": ("Registered Nurse", "Provide bedside patient assessment.", "Patient assessment experience and Massachusetts registered nurse license required."),
}


def job(profile_id: str, **overrides: object) -> dict:
    title, duties, requirements = JOBS[profile_id]
    result = {
        "key": f"greenhouse:fictional-benchmark:{profile_id}",
        "source": "greenhouse",
        "company": "Fictional Benchmark Employer",
        "title": title,
        "location": "Boston, MA",
        "description_html": f"<h2>Responsibilities</h2><p>{duties}</p><h2>Requirements</h2><p>{requirements}</p><p>Excel experience required.</p>",
        "listing_status": "open",
        "description_kind": "full",
        "last_verified_at": "2026-09-05T12:00:00Z",
        "opportunity_type": "job",
        "workplace_type": "onsite",
    }
    result.update(overrides)
    return result


def assess(tmp_path: Path, profile_id: str, posting: dict, *, policy: FilterPolicy | None = None, resume: dict | None = None) -> dict:
    candidate = Profile(
        name=profile_id,
        directory=tmp_path,
        resume=deepcopy(PROFILES[profile_id] if resume is None else resume),
        filters=policy or FilterPolicy(),
    )
    decision = apply_filters(posting, candidate.filters)
    result = assess_posting(posting, candidate, {"stage": "hard_filter", **decision._asdict()})
    assert set(result) == {"status", "summary", "evidence", "conflicts", "unknowns"}
    return result


@pytest.mark.parametrize("profile_id", PROFILES)
def test_relevant_work_is_recommended_with_traceable_evidence(tmp_path: Path, profile_id: str) -> None:
    result = assess(tmp_path, profile_id, job(profile_id))
    assert result["status"] == "suitable", result
    assert any(row["source"].startswith("profile.resume.") and row["candidate_evidence"] != "Excel" for row in result["evidence"]), result
    assert not result["conflicts"], result


@pytest.mark.parametrize("profile_id,job_id", [
    (candidate, opening) for candidate in PROFILES for opening in JOBS if candidate != opening
])
def test_shared_office_tool_does_not_establish_unrelated_professional_fit(tmp_path: Path, profile_id: str, job_id: str) -> None:
    result = assess(tmp_path, profile_id, job(job_id))
    # Excel is genuinely supported; the unsupported profession stays uncertain.
    assert result["status"] == "needs_review", result
    assert result["unknowns"], result
    assert not result["conflicts"], result


@pytest.mark.parametrize("requirement", ["Programming experience required.", "Front-end development experience required."])
def test_clear_developer_equivalents_keep_relevant_recommendations(tmp_path: Path, requirement: str) -> None:
    result = assess(tmp_path, "fictional_developer", job(
        "fictional_developer", title="Web Application Developer",
        description_html=f"<h2>Requirements</h2><p>{requirement}</p>",
    ))
    # Python establishes programming; building React web applications establishes
    # front-end development. Neither inference implies unrelated specialization.
    assert result["status"] == "suitable", result
    assert any(row["source"].startswith("profile.resume.") for row in result["evidence"]), result


def test_unestablished_nursing_license_is_review_not_rejection(tmp_path: Path) -> None:
    resume = deepcopy(PROFILES["fictional_nurse"])
    del resume["certifications"]
    result = assess(tmp_path, "fictional_nurse", job("fictional_nurse"), resume=resume)
    assert result["status"] == "needs_review", result
    assert any("license" in item.casefold() for item in result["unknowns"]), result
    assert not result["conflicts"], result


def test_explicit_eligibility_conflict_overrides_matching_work(tmp_path: Path) -> None:
    # This fictional candidate explicitly configured citizenship-only openings as
    # ineligible; no citizenship is inferred from the candidate's name or resume.
    policy = FilterPolicy(enabled=("work_authorization",), work_authorization=WorkAuthorizationPolicy(
        restrictions=(re.compile(r"U\.S\. citizenship required", re.I),),
    ))
    posting = job("fictional_developer")
    posting["description_html"] += "<p>U.S. citizenship required.</p>"
    result = assess(tmp_path, "fictional_developer", posting, policy=policy)
    assert result["status"] == "not_suitable", result
    assert result["conflicts"], result


def test_explicit_role_preference_overrides_shared_skills(tmp_path: Path) -> None:
    policy = FilterPolicy(enabled=("role_target",), role_target=RoleTargetPolicy(
        exclude=Matcher("sales roles explicitly excluded", re.compile(r"\bsales\b", re.I)),
    ))
    result = assess(tmp_path, "fictional_developer", job("fictional_developer", title="Software Sales Engineer"), policy=policy)
    assert result["status"] == "not_suitable", result
    assert result["conflicts"], result


@pytest.mark.parametrize("profile_id", PROFILES)
@pytest.mark.parametrize("source_change,expected", [
    ({"description_kind": "snippet", "snippet": True}, "needs_review"),
    ({"description_kind": "missing", "description_html": ""}, "needs_review"),
    ({"listing_status": "closed"}, "not_suitable"),
])
def test_source_controls_apply_across_professions(tmp_path: Path, profile_id: str, source_change: dict, expected: str) -> None:
    result = assess(tmp_path, profile_id, job(profile_id, **source_change))
    assert result["status"] == expected, result
    assert result["conflicts"] if expected == "not_suitable" else result["unknowns"], result
