from __future__ import annotations

from pathlib import Path

from venator.match.assessment import assess_posting
from venator.profile import Profile


def profile(tmp_path: Path, resume: dict) -> Profile:
    return Profile(
        name="candidate",
        directory=tmp_path,
        resume=resume,
    )


def posting(**overrides: object) -> dict:
    value = {
        "key": "greenhouse:example:1",
        "source": "greenhouse",
        "title": "Research Assistant",
        "location": "Boston, MA",
        "locations": ["Boston, MA"],
        "description_html": "<p>Join our team.</p><h2>Requirements</h2><ul><li>Python experience</li></ul>",
        "listing_status": "open",
        "description_kind": "full",
        "last_verified_at": "2026-09-04T12:00:00Z",
        "opportunity_type": "job",
        "workplace_type": "hybrid",
        "application_deadline": "2026-10-01",
    }
    value.update(overrides)
    return value


def passing_decision(**overrides: object) -> dict:
    value = {"stage": "hard_filter", "verdict": "pass", "facts": {}, "reason": "passed"}
    value.update(overrides)
    return value


def test_uses_confirmed_resume_evidence_and_returns_serializable_shape(tmp_path: Path) -> None:
    result = assess_posting(
        posting(),
        profile(
            tmp_path,
            {
                "technical_proficiencies": [{"label": "Tools", "items": "Python, Excel"}],
                "education": [{"degree": "Bachelor of Science in Biochemistry"}],
            },
        ),
        passing_decision(facts={"location": "onsite"}),
    )

    assert set(result) == {"status", "summary", "evidence", "conflicts", "unknowns"}
    assert result["status"] == "suitable"
    assert any(
        row["requirement"].casefold() == "python"
        and row["candidate_evidence"] == "Python"
        and row["source"].startswith("profile.resume.technical_proficiencies")
        for row in result["evidence"]
    )
    assert "Python; SQL" not in str(result["evidence"])


def test_no_evidence_when_resume_has_no_overlap(tmp_path: Path) -> None:
    result = assess_posting(
        posting(),
        profile(tmp_path, {"technical_proficiencies": [{"label": "Tools", "items": "Excel"}]}),
        passing_decision(),
    )

    assert result["status"] == "needs_review"
    assert result["evidence"] == []
    assert any("no established overlap" in unknown for unknown in result["unknowns"])


def test_failed_source_check_keeps_cached_full_text_out_of_suitable(tmp_path: Path) -> None:
    result = assess_posting(
        posting(verification_status="unknown"),
        profile(tmp_path, {"skills": ["Python"]}), passing_decision(),
    )
    assert result["status"] == "needs_review"
    assert any("source verification" in text for text in result["unknowns"])


def test_company_intro_is_not_treated_as_a_requirement(tmp_path: Path) -> None:
    result = assess_posting(
        posting(
            title="Research Assistant",
            description_html="<p>Our company uses Python and SQL in its platform.</p>",
        ),
        profile(tmp_path, {"technical_proficiencies": [{"label": "Tools", "items": "Python, SQL"}]}),
        passing_decision(),
    )

    assert result["status"] == "needs_review"
    assert result["evidence"] == []


def test_snippet_with_title_overlap_needs_review_and_cannot_be_suitable(tmp_path: Path) -> None:
    result = assess_posting(
        posting(title="Python Research Assistant", description_kind="snippet", snippet=True),
        profile(tmp_path, {"technical_proficiencies": [{"label": "Tools", "items": "Python"}]}),
        passing_decision(),
    )

    assert result["status"] == "needs_review"
    assert any("snippet" in unknown for unknown in result["unknowns"])


def test_hard_filter_kill_is_exposed_as_a_conflict(tmp_path: Path) -> None:
    result = assess_posting(
        posting(),
        profile(tmp_path, {"technical_proficiencies": [{"label": "Tools", "items": "Python"}]}),
        {"stage": "hard_filter", "verdict": "kill", "rule": "location", "reason": "outside Boston"},
    )

    assert result["status"] == "not_suitable"
    assert result["evidence"] == []
    assert any("outside Boston" in conflict for conflict in result["conflicts"])


def test_passing_unknown_filter_fact_stays_unknown(tmp_path: Path) -> None:
    result = assess_posting(
        posting(),
        profile(tmp_path, {"technical_proficiencies": [{"label": "Tools", "items": "Python"}]}),
        passing_decision(facts={"work_authorization": "unassessed"}),
    )

    assert result["status"] == "needs_review"
    assert any("work authorization" in unknown and "unassessed" in unknown for unknown in result["unknowns"])


def test_closed_posting_is_not_suitable_even_with_evidence(tmp_path: Path) -> None:
    result = assess_posting(
        posting(listing_status="closed"),
        profile(tmp_path, {"technical_proficiencies": [{"label": "Tools", "items": "Python"}]}),
        passing_decision(),
    )

    assert result["status"] == "not_suitable"
    assert any("closed" in conflict for conflict in result["conflicts"])


def test_no_profile_is_unassessed(tmp_path: Path) -> None:
    result = assess_posting(posting(), None)

    assert result == {
        "status": "unassessed",
        "summary": "No candidate Profile is available for assessment.",
        "evidence": [],
        "conflicts": [],
        "unknowns": ["candidate Profile is unavailable"],
    }


def test_one_matching_skill_does_not_hide_another_required_skill(tmp_path: Path) -> None:
    result = assess_posting(
        posting(description_html="<h2>Requirements</h2><ul><li>Python and Rust experience</li></ul>"),
        profile(tmp_path, {"skills": ["Python"]}),
        passing_decision(),
    )
    # A potential match is not a certification that every skill is established.
    assert result["status"] == "suitable"
    assert any("rust" in line for line in result["unknowns"])


def test_broader_relation_can_match_one_branch_of_an_embedded_requirement(tmp_path: Path) -> None:
    result = assess_posting(
        posting(description_html="""<h2>Preferred Qualifications</h2>
          <p>No college degree necessary but some skills in coding, biology or computational biology required.</p>"""),
        profile(tmp_path, {"skills": ["Python"]}),
        passing_decision(),
    )
    assert result["status"] == "suitable", result
    assert any(
        row.get("basis") == "related_skill" and row["requirement"].casefold() == "coding"
        for row in result["evidence"]
    ), result


def test_geography_pass_alone_is_not_candidate_fit(tmp_path: Path) -> None:
    result = assess_posting(
        posting(), profile(tmp_path, {"skills": ["Excel"]}),
        passing_decision(facts={"location": "onsite"}),
    )
    assert result["status"] == "needs_review"


def test_cpp_is_not_csharp_evidence(tmp_path: Path) -> None:
    result = assess_posting(
        posting(description_html="<h2>Requirements</h2><p>C# experience required.</p>"),
        profile(tmp_path, {"skills": ["C++"]}), passing_decision(),
    )
    assert result["status"] != "suitable"
    assert not any(row["source"].startswith("profile.resume.") for row in result["evidence"])


def test_missing_salary_and_unclear_structured_geography_are_not_satisfied_requirements(tmp_path: Path) -> None:
    for facts in ({"location": "unknown"}, {"eligibility": "salary_unknown"}, {"eligibility": "degree_timing"}):
        result = assess_posting(posting(), profile(tmp_path, {"skills": ["Python"]}), passing_decision(facts=facts))
        assert result["status"] == "needs_review"


def test_a_workday_listing_does_not_reverify_its_old_description(tmp_path: Path) -> None:
    result = assess_posting(posting(source="workday"), profile(tmp_path, {"skills": ["Python"]}), passing_decision())
    assert result["status"] == "needs_review"
    assert any("detail refresh" in item for item in result["unknowns"])


def test_matching_skill_does_not_cover_unlisted_certification_requirement(tmp_path: Path) -> None:
    result = assess_posting(
        posting(description_html="<h2>Requirements</h2><p>Python and cryo-electron microscopy certification required.</p>"),
        profile(tmp_path, {"skills": ["Python"]}), passing_decision(),
    )
    assert result["status"] == "needs_review"
    assert any("cryo-electron microscopy certification" in item for item in result["unknowns"])
    assert any(row["requirement"] == "Python" for row in result["evidence"])


def test_employer_platform_stack_is_not_a_candidate_requirement(tmp_path: Path) -> None:
    result = assess_posting(
        posting(description_html="""<h2>Requirements</h2>
          <p>Our platform requires Python for internal data processing.</p>
          <p>Communication skills required.</p>"""),
        profile(tmp_path, {"skills": ["Python"]}),
        passing_decision(),
    )
    assert result["status"] == "needs_review", result
    assert not any(row["requirement"].casefold() == "python" for row in result["evidence"]), result


def test_independently_supported_compound_requirements_remain_suitable(tmp_path: Path) -> None:
    result = assess_posting(
        posting(description_html="<h2>Requirements</h2><p>Python and cryo-electron microscopy certification required.</p>"),
        profile(tmp_path, {"skills": ["Python"], "certifications": ["cryo-electron microscopy certification"]}),
        passing_decision(),
    )
    assert result["status"] == "suitable"


def test_confirmed_phrase_can_span_a_conjunction(tmp_path: Path) -> None:
    result = assess_posting(
        posting(description_html="<h2>Requirements</h2><p>Research and development experience required.</p>"),
        profile(tmp_path, {"skills": ["Research and development"]}), passing_decision(),
    )
    assert result["status"] == "suitable"


def test_ats_sections_exclude_pay_benefits_location_and_legal_footer(tmp_path: Path) -> None:
    import html

    description = html.escape('''
      <p><strong>Position Summary</strong></p>
      <p>This is an opportunity to gain experience in an automated laboratory.</p>
      <p><strong>Required Qualifications</strong></p>
      <ul><li>Python experience required.</li></ul>
      <p><strong>Preferred Qualifications</strong></p>
      <ul><li>Familiarity with automated laboratory equipment.</li></ul>
      <hr>
      <p>The base salary range for this role is $37,700-$53,500. Actual pay within this
      range will depend on a candidate's skills, expertise, and experience. We also offer
      a comprehensive benefits package including medical, dental &amp; vision coverage.</p>
      <p>This position requires regular on-site attendance to the Boston office.</p>
      <div>It is our policy to provide equal employment opportunities to all applicants.</div>
      <div><strong>Privacy Notice</strong></div>
      <div>I understand that I am applying for employment and am being asked to provide
      information in connection with my application.</div>
    ''')
    result = assess_posting(posting(description_html=description), profile(tmp_path, {"skills": ["Python"]}), passing_decision())
    assert result["status"] == "suitable"
    assert any("Optional qualification gap" in item for item in result["unknowns"])
    assert not any(term in " ".join(result["unknowns"]) for term in ("$37", "salary", "Privacy", "employment opportunities", "Boston office", "gain experience"))


def test_preferred_gap_does_not_hide_a_required_certification(tmp_path: Path) -> None:
    description = '''<p><strong>Required Qualifications</strong></p>
      <ul><li>Python and cryo-electron microscopy certification required.</li></ul>
      <p><strong>Preferred Qualifications</strong></p>
      <ul><li>Familiarity with automated laboratory equipment.</li></ul>'''
    result = assess_posting(posting(description_html=description), profile(tmp_path, {"skills": ["Python"]}), passing_decision())
    assert result["status"] == "needs_review"
    assert any("cryo-electron microscopy certification" in item for item in result["unknowns"])


def test_optional_section_stops_at_inline_role_heading(tmp_path: Path) -> None:
    result = assess_posting(
        posting(description_html='''<p><strong>Preferred Qualifications:</strong></p>
          <ul><li>Python experience.</li></ul>
          <p><strong>About the Role:</strong> We are deploying a fleet of robots.</p>
          <p><strong>Responsibilities:</strong></p><ul><li>Operate and maintain equipment.</li></ul>'''),
        profile(tmp_path, {"skills": ["Python"]}), passing_decision(),
    )
    assert result["status"] == "needs_review"
    assert not any("fleet" in item or "equipment" in item for item in result["unknowns"])


def test_absent_decision_remains_unassessed(tmp_path: Path) -> None:
    result = assess_posting(posting(), profile(tmp_path, {"skills": ["Python"]}))
    assert result["status"] == "unassessed"


def test_explicit_required_clause_is_not_waived_by_preferred_heading(tmp_path: Path) -> None:
    result = assess_posting(
        posting(description_html='''<p><strong>Preferred Qualifications:</strong></p>
          <ul><li>Python and cryo-electron microscopy certification required.</li>
          <li>Automated laboratory experience is not required.</li></ul>'''),
        profile(tmp_path, {"skills": ["Python"]}), passing_decision(),
    )
    assert result["status"] == "needs_review"
    assert any(item.startswith("Qualification not established:") and "certification" in item for item in result["unknowns"])
    assert any(item.startswith("Optional qualification gap:") and "not required" in item for item in result["unknowns"])


def test_intern_title_overlap_does_not_establish_qualification(tmp_path: Path) -> None:
    result = assess_posting(
        posting(title="Automation Scientist Intern", description_html="<p>Join our laboratory.</p>"),
        profile(tmp_path, {"experience": [{"role": "Intern", "bullets": ["Worked on campus."]}]}),
        passing_decision(),
    )
    assert result["status"] == "needs_review"
    assert any("title overlap alone" in item for item in result["unknowns"])


def test_skill_in_title_and_requirements_still_establishes_qualification(tmp_path: Path) -> None:
    result = assess_posting(posting(title="Python Developer"), profile(tmp_path, {"skills": ["Python"]}), passing_decision())
    assert result["status"] == "suitable"


def test_workday_parenthetical_qualification_headings_stop_at_pay_and_policy(tmp_path: Path) -> None:
    from venator.match.assessment import _contexts, _plain

    description = '''<p>Here’s What You’ll Need (Basic Qualifications)</p>
      <ul><li>Python experience required.</li></ul>
      <p>Here’s What You’ll Bring to the Table (Preferred Qualifications)</p>
      <ul><li>Experience with laboratory automation.</li></ul>
      <p>Pay &amp; Benefits</p>
      <p>The salary range for this role is $20.00 - $60.00. An individual’s position
      within the salary range will be based on qualifications, certifications,
      experience, skills, performance, and business needs.</p>
      <p>Equal Opportunities</p>
      <p>We provide equal employment opportunity and non-discrimination.
      If you meet the Basic Qualifications for the role, please apply!</p>
      <p>Export Control Notice</p>
      <p>Employment is contingent upon the applicant’s ability to access export-controlled
      information. Only individuals who qualify as U.S. persons are eligible for this position.</p>'''
    contexts = _contexts({}, _plain(description))
    assert [source for text, source in contexts if "laboratory automation" in text] == ["posting.description_html.preferred"]
    assert not any(term in text for text, _ in contexts for term in ("Here’s What", "$20.00", "non-discrimination"))
    assert any("export-controlled" in text for text, _ in contexts)
    result = assess_posting(posting(description_html=description), profile(tmp_path, {"skills": ["Python"]}), passing_decision())
    assert result["status"] == "needs_review"
    assert any("export-controlled" in item for item in result["unknowns"])
    assert not any(term in " ".join(result["unknowns"]) for term in ("Here’s What", "$20.00", "non-discrimination"))


def test_optional_overlap_cannot_certify_unrecognized_required_section(tmp_path: Path) -> None:
    result = assess_posting(posting(description_html="""<p><strong>What You'll Need to Succeed</strong></p>
      <ul><li>2 to 5 years of laboratory experience.</li><li>Experience operating SEM and XRD.</li></ul>
      <p><strong>Bonus Points For</strong></p><ul><li>Comfort with high-throughput workflows.</li></ul>"""),
      profile(tmp_path, {"skills": ["high-throughput"]}), passing_decision())
    assert result["status"] == "needs_review"
    assert any("optional qualifications" in item for item in result["unknowns"])


def test_actual_work_can_establish_relevance_with_visible_skill_gaps(tmp_path: Path) -> None:
    result = assess_posting(posting(description_html='''<h2>Responsibilities</h2>
      <p>Integrate Lab Data: Build data pipelines for experimental results.</p>
      <h2>Preferred Qualifications</h2><p>Strong proficiency in Python.</p>'''),
      profile(tmp_path, {"skills": ["data pipelines"]}), passing_decision())
    assert result["status"] == "suitable"
    assert any(row["requirement"] == "data pipelines" for row in result["evidence"])
    assert any("python" in row.casefold() for row in result["unknowns"])


def test_headings_and_relevance_are_not_biotech_profile_specific(tmp_path: Path) -> None:
    result = assess_posting(posting(title="Editor", description_html='''<h2>Qualifications &amp; Experience</h2>
      <p>Copy editing experience.</p><p>Familiarity with our publishing system.</p>'''),
      profile(tmp_path, {"skills": ["Copy editing"]}), passing_decision())
    assert result["status"] == "suitable"
    assert any("publishing" in row for row in result["unknowns"])


def test_generic_punctuation_and_verb_are_not_technical_evidence(tmp_path: Path) -> None:
    result = assess_posting(posting(description_html='''<h2>Requirements</h2>
      <p>You excel in a fast-paced setting.</p><p>Hands-on materials characterization using high-throughput workflows.</p>'''),
      profile(tmp_path, {"skills": ["Excel"], "experience": [{"role": "Intern", "bullets": ["Observed high-throughput instruments, e.g., automated analyzers."]}]}), passing_decision())
    assert result["status"] == "needs_review"
    assert not any(row["source"].startswith("profile.resume") for row in result["evidence"])


def test_knowing_a_technique_does_not_establish_its_required_certificate(tmp_path: Path) -> None:
    result = assess_posting(posting(description_html="<h2>Requirements</h2><p>Python and cryo-electron microscopy certification required.</p>"),
                           profile(tmp_path, {"skills": ["Python", "cryo-electron microscopy"]}), passing_decision())
    assert result["status"] == "needs_review"
    assert any("Credential" in row for row in result["unknowns"])


def test_explicitly_withheld_facts_never_establish_relevance(tmp_path: Path) -> None:
    from venator.match.assessment import _resume_facts
    for marker in ({"draft": True}, {"confirmed": False}, {"owner_confirmed": "no"}, {"include": False}, {"status": "pending"}):
        for resume in (
            {"technical_proficiencies": [{"items": "Python", **marker}]},
            {"skills": [{"text": "Python", **marker}]},
            {"experience": [{"role": "Developer", "bullets": [{"text": "Python", **marker}]}]},
            {"education": [{"degree": "Python", **marker}]},
            {"skills": ["Python"], **marker},
        ):
            assert not any("Python" in fact.value for fact in _resume_facts(resume))


def test_in_progress_degree_evidence_keeps_the_expected_date(tmp_path: Path) -> None:
    result = assess_posting(posting(description_html="<h2>Requirements</h2><p>Biochemistry coursework.</p>"),
                           profile(tmp_path, {"education": [{"degree": "Bachelor of Science in Biochemistry", "date": "May 2027 (Expected)"}]}), passing_decision())
    assert any("Expected" in row["candidate_evidence"] for row in result["evidence"])
