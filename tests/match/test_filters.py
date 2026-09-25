from __future__ import annotations

import dataclasses
import json
import random
from pathlib import Path

import pytest
import yaml

from venator.match.filters import (
    apply_filters,
    education_fit_filter,
    education_fit_reading,
    role_target_filter,
    strip_html,
    work_authorization_filter,
)
from venator.profile import load_profile
from venator.profile.schema import EducationFitPolicy, FilterPolicy, RoleTargetPolicy

REPOSITORY = Path(__file__).parents[2]

# The Hard Filters are generic; what they match on is Profile data. These cases
# assert the Owner's policy behaves as the Owner wrote it.
POLICY = load_profile(REPOSITORY / "profiles" / "example").filters


def _ladder_vocabulary(profile: str = "example") -> dict[str, tuple[str, ...]]:
    """The title terms a Profile writes for each rung, read from the Profile itself."""
    targeting = yaml.safe_load(
        (REPOSITORY / "profiles" / profile / "targeting.yaml").read_text(encoding="utf-8")
    )
    levels = targeting["filters"]["role_target"]["levels"]
    return {level["name"]: tuple(level.get("title_terms") or ()) for level in levels}


def posting(**changes) -> dict:
    value = {
        "key": "source:board:1",
        "title": "Laboratory Intern",
        "location": "Boston, MA",
        "description_html": "<p>Currently pursuing a bachelor's degree.</p>",
    }
    value.update(changes)
    return value


def test_strip_html_handles_entity_escaped_ats_markup() -> None:
    assert strip_html("&lt;p&gt;Housing &amp;amp; travel stipend&lt;/p&gt;") == "Housing & travel stipend"


def test_strip_html_ignores_non_visible_script_and_style_content() -> None:
    markup = "<style>.hidden { color: red; }</style><p>Visible</p><script>hidden()</script>"
    assert strip_html(markup) == "Visible"


def test_work_authorization_does_not_kill_no_sponsorship_wording() -> None:
    result = work_authorization_filter(
        posting(description_html="Applicants must be authorized to work without sponsorship now or in future.")
    )
    assert result[:2] == ("pass", "work_authorization")


@pytest.mark.parametrize(
    "requirement",
    [
        "Currently pursuing a bachelor's degree in biochemistry.",
        "Bachelor's degree preferred, not required.",
        "Two years of laboratory experience.",
        "The ideal candidate may have up to 5 years of experience.",
    ],
)
def test_education_fit_passes_non_requirements_and_lower_experience(requirement: str) -> None:
    assert education_fit_filter(posting(description_html=requirement), POLICY)[:2] == ("pass", "education_fit")


def test_education_fit_without_an_experience_minimum_does_not_crash(tmp_path: Path) -> None:
    """The Profile shape the schema promises never crashes: a degree rule, no years rule."""
    directory = tmp_path / "degrees-only"
    directory.mkdir()
    (directory / "targeting.yaml").write_text(
        "filters:\n"
        "  enabled: [education_fit]\n"
        "  education_fit:\n"
        "    kill_completed_degree: true\n",
        encoding="utf-8",
    )
    policy = load_profile(directory).filters
    described = posting(
        description_html="<p>Requirements: 5 years in building assay pipelines.</p>"
    )

    assert education_fit_filter(described, policy)[:2] == ("pass", "education_fit")
    assert apply_filters(described, policy)[:2] == ("pass", None)


# --- Hard Filter decision regressions -----------------------------------------
# Each case below is a shape that actually occurred in the committed corpus and
# was decided wrongly. The Posting keys are named so a failure is traceable back
# to the decision it came from.


def test_a_truncated_description_reads_as_unknown_not_as_permission() -> None:
    """193 Postings are 500-character snippets; nothing read the flag saying so.

    Both description-only filters treated a truncated description as "no
    restriction stated", which is why work authorization has never killed an
    Adzuna Posting and structurally could not.
    """
    truncated = posting(snippet=True, description_html="<p>Join our growing team of scientists…</p>")
    full = posting(description_html="<p>Join our growing team of scientists.</p>")

    for hard_filter in (work_authorization_filter, education_fit_filter):
        blind = hard_filter(truncated, POLICY)
        seen = hard_filter(full, POLICY)
        assert blind[0] == seen[0] == "pass"
        # A Posting that could not be assessed must not be recorded the same way
        # as one that was assessed and found clean.
        assert blind[2] != seen[2]
        assert "could not be assessed" in blind[2]
        assert "could not be assessed" not in seen[2]


def test_the_aggregate_pass_reason_admits_what_it_could_not_read() -> None:
    verdict, rule, reason, _ = apply_filters(
        posting(snippet=True, title="Laboratory Technician", description_html="<p>A growing team…</p>"),
        POLICY,
    )

    assert (verdict, rule) == ("pass", None)
    assert "could not be assessed" in reason and "truncated snippet" in reason


def test_a_bonus_points_clearance_is_a_preference_not_a_requirement() -> None:
    """greenhouse:lilasciences:4324020009 — VP, Head of Government Partnerships.

    Three of the five work-authorization kills quoted a "Bonus Points For"
    bullet. A bonus-points clearance excludes nobody; it is the opposite of a
    requirement. education_fit already carried this guard and this filter did not.
    """
    description = (
        "<p>Willingness to travel regularly to build the relationships required for the "
        "company to succeed.</p><p>Bonus Points For</p><ul>"
        "<li>Advanced degree in a relevant scientific discipline</li>"
        "<li>Experience at a venture-backed startup</li>"
        "<li>Experience selling SaaS</li>"
        "<li>Active U.S. security clearance</li></ul>"
    )

    assert work_authorization_filter(posting(description_html=description), POLICY)[:2] == (
        "pass",
        "work_authorization",
    )


def test_a_tiered_posting_is_read_at_its_lowest_rung() -> None:
    """greenhouse:lilasciences:4274132009 — Maintenance Engineering Technician I/II.

    The loop saw "1+ years", fell through, and killed on the Technician II rung
    100 characters later. A ladder is not a floor.
    """
    description = (
        "<li>Technician I (L3): 1+ years of hands-on instrument maintenance experience "
        "in a research, clinical, or production lab setting</li>"
        "<li>Technician II (L4): 3+ years of hands-on instrument maintenance experience, "
        "with demonstrated independence on troubleshooting</li>"
    )

    assert education_fit_filter(posting(description_html=description), POLICY)[:2] == (
        "pass",
        "education_fit",
    )


def test_a_range_whose_floor_clears_the_bar_no_longer_kills() -> None:
    result = education_fit_filter(
        posting(description_html="<li>Required 1-5+ years of laboratory experience</li>"), POLICY
    )

    assert result[:2] == ("pass", "education_fit")


def test_ms_office_is_microsoft_not_a_master_of_science() -> None:
    """greenhouse:relaytherapeutics:6099048004 — right outcome, fabricated evidence.

    "MS" in "MS Office" satisfied the degree pattern and the kill quoted an
    unrelated bullet. A defective reason on a correct kill is an unearned
    confidence signal on the dashboard the Owner reads.
    """
    result = education_fit_filter(
        posting(
            description_html="<li>You have strong skills and are familiar with MS Office products "
            "(MS Project, Excel and Power Point)</li>"
        ),
        POLICY,
    )

    assert result[:2] == ("pass", "education_fit")


def test_role_target_kills_titles_above_the_band_the_profile_targets() -> None:
    """No filter read role type at all; 73.6 % of everything judged was a Senior
    Director, a Staff Engineer or a Loading Dock Associate."""
    verdict, rule, reason = role_target_filter(
        posting(title="Senior Director, Large Pharma US Clinical & Patient Technology"), POLICY
    )

    assert (verdict, rule) == ("kill", "role_target")
    # Read at the rung nearest the band — "Senior Director" is a Senior to a
    # Profile accepting intern through mid — and still above it. Resolving toward
    # the band is a safety valve, not a hole: it moves a title as close to the
    # band as the ladder allows, and a title whose closest reading is still out
    # of band was never ambiguous about being out of band.
    assert "above" in reason and "senior" in reason


def test_role_target_passes_a_title_it_cannot_place() -> None:
    """Conservative by construction: the title is the only field an aggregator
    does not truncate, so an unplaceable title is passed, not guessed at."""
    verdict, _, reason = role_target_filter(posting(title="Scientist, Protein Engineering"), POLICY)

    assert verdict == "pass"
    assert "passed conservatively" in reason


# The cases below correct decisions PR #7 left wrong on the merits while pinning
# them as correct in the golden. Each names the Posting it came from.


def test_the_same_branch_with_nothing_calling_it_required_passes() -> None:
    """The guard the bundled scan was missing, stated as the difference it makes.

    The case above with both requirement signals taken out: no requirement
    wording, and *industry* experience rather than postdoctoral, which nothing
    about a doctorate is presupposed by. The completed-degree scan has always
    declined to kill on a degree no wording calls required; the bundled scan used
    to kill anyway, which is how "We love a good BS in Biology. 1 year of
    laboratory experience." became a kill. An unasserted requirement resolves to
    "not sure".
    """
    result = education_fit_filter(
        posting(
            description_html="<li>MS in Biochemistry, Biophysics, or a related field with 4+ "
            "years of relevant experience, or PhD in a related discipline with 0-2 years of "
            "industry experience.</li>"
        ),
        POLICY,
    )

    assert result[:2] == ("pass", "education_fit")


@pytest.mark.parametrize(
    "heading",
    ["Who You Are:", "What You'll Bring:", "What we are looking for:", "Your experience includes:"],
)
def test_a_benign_heading_far_above_a_wish_does_not_make_it_a_requirement(heading: str) -> None:
    """The failure mode the fixture could not previously exhibit, made exhibitable.

    A heading is not near the line it governs, which is why walking back to the
    nearest one was unbounded — and being unbounded is what made it wrong. "Who
    You Are" is a culture heading, and with sixteen hundred characters of benign
    body between it and a nice-to-have, the flagship wish this branch was written
    to rescue died anyway. The heading rule is removed rather than narrowed; this
    pins that it stays removed.
    """
    body = ("We are a team building tools for biology. " * 40)[:1600]
    wish = "We love a good BS in Biology. 1 year of laboratory experience."

    assert education_fit_filter(
        posting(description_html=f"<p>{heading} curious and hands-on. {body}{wish}</p>"), POLICY
    )[:2] == ("pass", "education_fit")


def test_a_requirements_heading_does_not_make_an_enrolment_a_completed_degree() -> None:
    """One of the four the author called plainly wrong to kill, still dying at review.

    A co-op asking to be *enrolled* in a program is the Owner exactly. It reached
    the bundled scan under "What You'll Need to Succeed" and was killed on a
    degree nobody had completed.
    """
    assert education_fit_filter(
        posting(
            description_html="<li>What You'll Need to Succeed Enrolled in a Bachelor's or "
            "Master's program in a STEM field with 1 year of hands-on project experience.</li>"
        ),
        POLICY,
    )[:2] == ("pass", "education_fit")


def test_a_degree_the_posting_only_prefers_does_not_bundle_into_a_reachable_branch() -> None:
    """The bound on the fix above: bundling must not turn a preference into a bar."""
    result = education_fit_filter(
        posting(
            description_html="<li>Bachelor's degree preferred, with 1 year of laboratory "
            "experience</li>"
        ),
        POLICY,
    )

    assert result[:2] == ("pass", "education_fit")


@pytest.mark.parametrize("title", ["Global Commercial Leader", "Team Leader, Bioanalysis"])
def test_an_unread_leadership_title_passes_rather_than_being_guessed(
    title: str,
) -> None:
    """adzuna:us:5830007804 — deliberately still passing, and here is why.

    "Leader" would place the first of these, and would also place "Team Leader",
    which sits inside the band this Profile accepts. A rung vocabulary is a
    convenience and never the safety mechanism: a word it lacks has to resolve to
    "not sure" and cost one more Posting to review, not to "no" and hide a Posting silently.
    """
    verdict, _, reason = role_target_filter(posting(title=title), POLICY)

    assert verdict == "pass"
    assert "passed conservatively" in reason


IN_BAND = "title names {} {} role, within the seniority band"
ABSTAINED = "title names no seniority band level; passed conservatively"


@pytest.mark.parametrize(
    "title",
    [
        "Biosafety Officer",
        "Radiation Safety Officer",
        "Research Officer, Immunology",
        "Scientific Officer I",
    ],
)
def test_an_ordinary_lab_officer_is_not_a_chief_anything(title: str) -> None:
    """The case segmentation cannot reach, so the term came off the ladder.

    Here `Officer` *is* the role noun, in the leading segment, with no other rung
    word beside it — neither the segment rule nor the junior-rung rule can save
    these. `officer` also earned nothing: `chief` and the C-suite acronyms
    already catch every real C-suite title it would have caught. Same test `cmo`
    failed, same answer.
    """
    verdict, rule, _ = role_target_filter(posting(title=title), POLICY)

    assert (verdict, rule) == ("pass", "role_target")


def test_the_c_suite_is_still_cut_without_the_word_officer() -> None:
    """The bound on the removal above: dropping the term must not open the band."""
    for title in ("Chief Scientific Officer", "Chief Medical Officer, Oncology"):
        verdict, rule, reason = role_target_filter(posting(title=title), POLICY)

        assert (verdict, rule) == ("kill", "role_target")
        assert "executive" in reason


def test_a_kill_reason_names_the_rung_with_the_article_english_uses() -> None:
    """"a executive role" is what the Owner was reading in the dashboard."""
    _, _, reason = role_target_filter(posting(title="Chief Scientific Officer"), POLICY)

    assert "names an executive role" in reason
    assert "names a executive" not in reason


def test_a_kill_on_an_ambiguous_title_says_it_read_the_title_generously() -> None:
    """A compound ``Chief of Staff`` title keeps its executive level in the reason.

    The old junior-token reading quoted ``Staff`` and described the title as a
    senior role. The complete phrase is executive, so the explanation now names
    the level that makes the kill.
    """
    _, _, ambiguous = role_target_filter(posting(title="Chief of Staff"), POLICY)
    _, _, plain = role_target_filter(posting(title="Chief Scientific Officer"), POLICY)

    assert ambiguous.startswith("read as close to the seniority band as this ladder allows")
    assert "an executive role ('Chief')" in ambiguous and "above the" in ambiguous
    assert plain.startswith("title names an executive role ('Chief')")


@pytest.mark.parametrize(
    "description",
    [
        # A wish, then a year count, and no requirement wording anywhere near
        # either — so no signal but the guard itself decides these.
        "We love a good BS in Biology. 1 year of laboratory experience.",
        "A BS in Biology goes a long way here. 1 year of laboratory experience.",
        "We hire people who enjoy a BS in Biology. 2 years of laboratory experience.",
    ],
)
def test_a_degree_nobody_required_does_not_bundle_into_a_stated_minimum(
    description: str,
) -> None:
    """The bundled-degree scan had no requirement guard at all; the point scan did.

    Each case here is load-bearing on the requirement guard: stub the guard out
    of ``_bundled_degree`` — not the ``_REQUIREMENT`` pattern, which
    ``_stated_as_preference`` reads too — and all three become kills. The
    parametrization used to mix in three cases worded "nice to have", "a plus"
    and "bonus points", filed as evidence for this guard, and this docstring then
    claimed all four were load-bearing on it. Measured by stubbing: they are not.
    All three are decided earlier, and they are asserted below where they say
    what they test. "A bachelor's degree is a plus. 1 year of laboratory
    experience." was kept here on the reasoning that the optional-degree guard
    reads only backwards from the degree — true, and beside the point, because
    the *years* match never survives to be bundled with anything: "a plus" sits
    inside its 70-character lookback and ``_stated_as_preference`` drops it
    before ``_bundled_degree`` is ever called.

    Thirteen corpus Postings were masked from this failure, not seventeen as this
    docstring first claimed. Measured reading: Postings whose bundled scan
    attaches a degree only when the requirement guard is absent — a subset of the corpus, and
    every one of them states at least three years, which the years rule
    (``experience_years_kill: 3``) kills on regardless. Ten state five or more
    and twelve are full descriptions rather than snippets, which is where the
    other counts in circulation come from. Zero of the thirteen are decided by
    the guard, so the golden fixture could not tell this accidental correctness
    from the real thing, and these synthetics assert the phrasing directly.
    """
    assert education_fit_filter(posting(description_html=f"<p>{description}</p>"), POLICY)[:2] == (
        "pass",
        "education_fit",
    )


@pytest.mark.parametrize(
    "description",
    [
        "Nice to have: a BS in Biology. 2 years of laboratory experience.",
        "Bonus points for a BS in Biology. 2 years of hands-on lab experience.",
        # Moved here from the requirement-guard cases above, where it was filed
        # as load-bearing on a guard it never reaches.
        "A bachelor's degree is a plus. 1 year of laboratory experience.",
    ],
)
def test_a_degree_worded_as_a_preference_does_not_bundle_either(description: str) -> None:
    """The older guards, asserted as themselves rather than as evidence for the new one.

    None of these three reaches ``_bundled_degree`` at all: the preference
    wording sits inside the stated minimum's 70-character lookback, so
    ``_stated_as_preference`` drops the minimum before anything is grouped into a
    requirement, and each one passes with the bundled scan's requirement guard
    stubbed out. What then decides them is the standalone degree scan, and even
    there they divide: "nice to have" and "bonus points" read as optional
    degrees, while "a plus" *follows* its degree, where the optional-degree guard
    cannot see it — that one survives because no requirement wording sits near
    the degree either. Keeping all three is right; filing them under the new
    guard, as evidence for it, was not.
    """
    assert education_fit_filter(posting(description_html=f"<p>{description}</p>"), POLICY)[:2] == (
        "pass",
        "education_fit",
    )


@pytest.mark.parametrize(
    "mention",
    [
        # A mention that has nothing to do with the requirement.
        "Our postdoc alumni network runs monthly seminars.",
        "You will report to a postdoctoral fellow on the team.",
        "Recent graduates and postdoctoral fellows are encouraged to apply.",
        # A mention that says the opposite of what was read from it.
        "Note: this is not a postdoctoral appointment.",
        "No postdoc experience is needed for this role.",
    ],
)
def test_a_postdoc_mentioned_outside_the_requirement_does_not_bundle_a_degree(
    mention: str,
) -> None:
    """The residual hole in the guard that replaced the heading walk.

    Each of these is the passing control below plus one sentence, and the only
    word that matters in that sentence is "postdoc". The bundled-degree scan
    accepted the token anywhere in a ±100-character window around the
    requirement, with no reading of what the sentence carrying it said, so an
    alumni network, a reporting line and two outright denials each turned "we
    love a good BS in Biology" into a stated doctorate. That is the defect the
    heading walk was removed for, bounded to 100 characters — a bound is not a
    fix.

    The token now has to sit inside the requirement itself, between the degree
    and the end of the stated minimum, and a negated one is skipped wherever it
    sits.
    """
    control = "We love a good BS in Biology. 1 year of laboratory experience."

    assert education_fit_filter(posting(description_html=f"<p>{control}</p>"), POLICY)[:2] == (
        "pass",
        "education_fit",
    )
    assert education_fit_filter(
        posting(description_html=f"<p>{control} {mention}</p>"), POLICY
    )[:2] == ("pass", "education_fit")


def test_experience_equivalent_to_a_postdoc_is_not_a_postdoc_requirement() -> None:
    """The mention that follows the stated minimum rather than preceding it.

    "1 year of laboratory experience, equivalent to postdoctoral training" asks
    for one year and offers a comparison; the window read the comparison as the
    requirement. The requirement ends where the stated minimum does.
    """
    description = (
        "We love a good BS in Biology. 1 year of laboratory experience, "
        "equivalent to postdoctoral training."
    )

    assert education_fit_filter(posting(description_html=f"<p>{description}</p>"), POLICY)[:2] == (
        "pass",
        "education_fit",
    )


@pytest.mark.parametrize(
    "denial",
    [
        "No security clearance is required for this position.",
        "A security clearance is not required.",
        "This role does not require an active security clearance.",
        "A green card is not required for this role.",
        "This position is not ITAR-restricted.",
    ],
)
def test_a_work_authorization_restriction_the_posting_denies_is_not_a_restriction(
    denial: str,
) -> None:
    """The same negation blindness, in the filter whose patterns are pure windows.

    Every restriction this Profile writes is a token with up to 55 characters of
    window around it — "security clearance" within 45 characters of "required" —
    so each of these denials matched the pattern for the restriction it denies,
    and a Posting that goes out of its way to say the Owner needs no clearance
    was killed for needing one. Found by sweeping the window-based signals for
    negations rather than by a corpus, which holds only the phrasings that
    happened.
    """
    assert work_authorization_filter(posting(description_html=f"<p>{denial}</p>"), POLICY)[:2] == (
        "pass",
        "work_authorization",
    )


@pytest.mark.parametrize(
    "denial",
    [
        "You do not need a PhD to succeed here. Qualifications: curiosity.",
        "We will consider applicants without a bachelor's degree. "
        "Requirements: attention to detail.",
    ],
)
def test_a_degree_the_posting_says_is_unnecessary_is_not_required(denial: str) -> None:
    """The phrasings ``_DEGREE_NON_REQUIREMENT`` lists in one wording and not another.

    It reads "not required" and "not necessary" and stops there, so "you do not
    need a PhD" and "applicants without a bachelor's degree" reached the
    requirement scan, which found "Qualifications:" and "Requirements:" in the
    next sentence and killed on it. The denial is read from 30 characters rather
    than the 100-wide requirement window, so a sentence two clauses away cannot
    cancel a degree a Posting really asks for.
    """
    assert education_fit_filter(posting(description_html=f"<p>{denial}</p>"), POLICY)[:2] == (
        "pass",
        "education_fit",
    )


# The band shapes a Profile can write, and the reason both are asserted on the
# same titles. The Owner's own band is anchored at the bottom of his ladder, and
# a rule that resolves an ambiguous title *downwards* cannot leave a band that
# starts at rung 0 — so every counterexample to that rule is invisible from this
# Profile, and from the corpus it produced. ``profiles/example/`` already ships
# the other shape.
BOTTOM_ANCHORED = ("intern", "entry", "mid")
MID_SENIOR = ("mid", "senior")


def _banded(accept: tuple[str, ...]) -> FilterPolicy:
    """The Owner's policy with a different slice of his ladder accepted."""
    role = dataclasses.replace(POLICY.role_target, accept=accept)
    return dataclasses.replace(POLICY, role_target=role)


def _accepted_rung(rules: RoleTargetPolicy, title: str) -> str | None:
    """An accepted rung after resolving each offered title as a whole phrase."""
    for offer in rules.alternatives(title):
        placed = rules.place(offer)
        if placed is not None and placed[1].name in rules.accept:
            return placed[1].name
    return None


@pytest.mark.parametrize(
    ("title", "accept", "verdict"),
    [
        # The band still cuts: each of these names one rung, outside the band.
        ("Laboratory Intern", BOTTOM_ANCHORED, "pass"),
        ("Laboratory Intern", MID_SENIOR, "kill"),
        ("Chief Scientific Officer", BOTTOM_ANCHORED, "kill"),
        ("Chief Scientific Officer", MID_SENIOR, "kill"),
        ("Senior Scientist", BOTTOM_ANCHORED, "kill"),
        ("Senior Scientist", MID_SENIOR, "pass"),
        # A compound Director title remains executive even when it contains an
        # accepted Associate token; the whole offered title is outside both
        # Profiles' bands.
        ("Associate Director", BOTTOM_ANCHORED, "kill"),
        ("Associate Director", MID_SENIOR, "kill"),
    ],
)
def test_the_band_a_profile_writes_still_decides_an_unambiguous_title(
    title: str, accept: tuple[str, ...], verdict: str
) -> None:
    """The bound on the rule above: resolving toward the band is not passing everything."""
    assert role_target_filter(posting(title=title), _banded(accept))[0] == verdict


def _fuzz_titles(vocabulary: dict[str, tuple[str, ...]], limit: int) -> list[str]:
    """Titles built from a Profile's own rung vocabulary, in the shapes ATS boards use."""
    shapes = (
        "{a} {b}",
        "{a} {b}, Oncology",
        "{a} {b} - Cambridge",
        "{a} Research {b}",
        "{a} {b} / {a} Scientist",
        "({a}) {b}, Platform",
    )
    terms = [(rung, term) for rung, words in vocabulary.items() for term in words]
    titles = {
        shape.format(a=first.title(), b=second.title())
        for shape in shapes
        for rung, first in terms
        for other, second in terms
        if rung != other
    }
    return random.Random(20260824).sample(sorted(titles), min(limit, len(titles)))


@pytest.mark.parametrize("accept", [BOTTOM_ANCHORED, MID_SENIOR])
def test_no_title_whose_whole_offered_role_is_accepted_is_killed_at_any_band(
    accept: tuple[str, ...]
) -> None:
    """An accepted *resolved offer* remains a pass across band shapes.

    Compound titles may contain a junior word while offering a senior role, so
    checking for any embedded accepted token was too broad. This fuzz keeps the
    stronger invariant: once ``RoleTargetPolicy.place`` resolves the complete
    offered phrase inside the Profile band, the filter must preserve it. True
    slash/or alternatives are evaluated branch by branch.
    """
    policy = _banded(accept)
    rules = policy.role_target
    titles = _fuzz_titles(_ladder_vocabulary(), limit=1200)
    in_band = 0

    for title in titles:
        if rules.exclude.search(title):
            continue  # an excluded function is a different rule, with no escape
        rung = _accepted_rung(rules, title)
        if rung is None:
            continue
        in_band += 1
        verdict, _, reason = role_target_filter(posting(title=title), policy)
        assert verdict == "pass", (
            f"{title!r} names {rung!r}, a rung {list(accept)} accepts, and was killed: {reason}"
        )

    # A property test that generated nothing in band would pass by vacuum.
    assert in_band > 100, f"only {in_band} of {len(titles)} generated titles name an accepted rung"


# ---------------------------------------------------------------------------
# What the Owner has, read against what a Posting asks for.
#
# ``kill_completed_degree`` is a yes/no built for a student: it can say "this
# Posting wants a degree the Owner has not finished" and nothing else. These
# cases pin the widened model beside it — the highest qualification the Owner
# holds, and one in progress — and pin that the old setting still decides
# exactly what it decided before, because reinterpreting a stored yes/no in
# place is how a filter silently changes its verdicts for every Profile that
# already set it.


def _education(**changes) -> FilterPolicy:
    """A Profile that reads education and nothing else."""
    return FilterPolicy(education_fit=dataclasses.replace(EducationFitPolicy(), **changes))


HOLDS_A_BACHELOR = _education(holds="bachelor")
#: Requirement wordings, and the level each one really asks for. A corpus cannot
#: reach these: it holds one Profile, and that Profile never had the new shape.
_REQUIREMENTS = (
    ("Requirements: Bachelor's degree in Marketing.", "bachelor"),
    ("Qualifications: BA in Communications.", "bachelor"),
    ("Requirements: Master's degree in Marketing.", "master"),
    ("Qualifications: an MBA is required.", "master"),
    ("Requirements: PhD in Biology.", "doctorate"),
    # Dotted, because "Ph.D." is the abbreviation the level reader has to strip
    # punctuation out of. It was written here as "Ph.D. or equivalent in
    # Chemistry" and is not any more: that wording states two branches, and the
    # test asserting it kills was asserting the defect this module now reads —
    # see ``test_an_equivalent_that_names_nothing_is_a_branch_of_the_offer``.
    ("Qualifications: a Ph.D. in Chemistry is required.", "doctorate"),
)


@pytest.mark.parametrize(("requirement", "level"), _REQUIREMENTS)
def test_a_degree_requirement_is_read_against_what_the_owner_holds(
    requirement: str, level: str
) -> None:
    """A bachelor's requirement passes for an Owner who holds one; more kills."""
    verdict, rule, reason = education_fit_filter(
        posting(description_html=f"<li>{requirement}</li>"), HOLDS_A_BACHELOR
    )
    if level == "bachelor":
        assert (verdict, rule) == ("pass", "education_fit"), reason
        assert "above the bachelor's degree the Profile states the Owner holds" in reason
    else:
        assert (verdict, rule) == ("kill", "education_fit"), reason
        assert "above the bachelor's degree the Profile states the Owner holds" in reason


@pytest.mark.parametrize(
    "requirement",
    [
        "Requirements: Bachelor's or Master's degree in Marketing.",
        "Qualifications: BS/MS in a related field.",
        "Qualifications: BS, MS, or PhD in a related discipline.",
        "You hold a degree (BSc, MSc or PhD) in Computer Science.",
    ],
)
def test_a_requirement_open_at_several_degrees_is_read_at_its_easiest(requirement: str) -> None:
    """The employer will take whichever of them the applicant has.

    This is the ladder rule ``education_fit`` already applies to stated years,
    applied to the degrees a single requirement offers. Reading the highest of
    them would kill somebody the employer would have interviewed.
    """
    verdict, _, reason = education_fit_filter(
        posting(description_html=f"<li>{requirement}</li>"), HOLDS_A_BACHELOR
    )
    assert verdict == "pass", reason


@pytest.mark.parametrize(
    "requirement",
    [
        "Requirements: a degree in a related field.",
        "Requirements: an advanced degree in a related discipline.",
    ],
)
def test_a_degree_requirement_with_no_stated_level_passes(requirement: str) -> None:
    """The governing rule: an unresolved requirement is never a confident kill."""
    verdict, _, reason = education_fit_filter(
        posting(description_html=f"<li>{requirement}</li>"), HOLDS_A_BACHELOR
    )
    assert verdict == "pass"
    assert reason.startswith("states a completed-degree requirement this Profile cannot place")
    assert reason.endswith("passed conservatively")


def test_a_qualification_in_progress_passes_rather_than_killing() -> None:
    """Employers differ on whether an expected graduation answers a requirement.

    So the Profile does not answer it either. Above the qualification in
    progress is still a kill — nothing is on its way to a doctorate here.
    """
    rules = _education(holds="bachelor", in_progress="master")
    master = education_fit_filter(
        posting(description_html="<li>Requirements: Master's degree in Marketing.</li>"), rules
    )
    doctorate = education_fit_filter(
        posting(description_html="<li>Requirements: PhD in Biology.</li>"), rules
    )
    assert master[0] == "pass"
    assert "cannot place" in master[2]
    assert doctorate[:2] == ("kill", "education_fit")


def test_an_owner_holding_a_doctorate_is_killed_by_no_degree_requirement() -> None:
    rules = _education(holds="doctorate")
    for requirement, _ in _REQUIREMENTS:
        verdict, _, reason = education_fit_filter(
            posting(description_html=f"<li>{requirement}</li>"), rules
        )
        assert verdict == "pass", f"{requirement}: {reason}"


def test_a_profile_that_says_nothing_about_education_kills_nothing_on_it() -> None:
    """Absent is "not configured", never "holds nothing"."""
    for requirement, _ in _REQUIREMENTS:
        verdict, _, reason = education_fit_filter(
            posting(description_html=f"<li>{requirement}</li>"), _education()
        )
        assert (verdict, reason) == ("pass", "no education-fit requirement is configured")


def test_a_degree_bundled_with_years_is_read_against_what_the_owner_holds() -> None:
    """A requirement costs everything it names at once — including its degree.

    The years are within reach in both of these; only the degree differs, and
    only the one above what the Owner holds is a kill.
    """
    rules = _education(holds="bachelor", experience_years_kill=6)
    above = education_fit_filter(
        posting(description_html="<li>Requirements: Master's degree with 2 years of experience.</li>"), rules
    )
    within = education_fit_filter(
        posting(description_html="<li>Requirements: Bachelor's degree with 2 years of experience.</li>"), rules
    )
    offered = education_fit_filter(
        posting(
            description_html="<li>Requirements: Bachelor's or Master's degree with 2 years of experience.</li>"
        ),
        rules,
    )
    assert above[:2] == ("kill", "education_fit")
    assert "alongside 2 years of experience" in above[2]
    assert within[0] == "pass", within[2]
    assert offered[0] == "pass", offered[2]


def test_the_original_completed_degree_setting_decides_exactly_as_it_did() -> None:
    """The backwards-compatibility property, as a property rather than a replay.

    A Profile carrying only ``kill_completed_degree`` must not learn to read a
    level: for that Owner every completed degree is out of reach whatever it
    names, and every one of these is a kill with the wording it has always had.
    The corpus replay proves this for the one committed Profile that sets it;
    this proves it for the wordings the corpus happens not to contain.
    """
    student = _education(kill_completed_degree=True)
    wordings = [requirement for requirement, _ in _REQUIREMENTS] + [
        "Requirements: Bachelor's or Master's degree in Marketing.",
        "Qualifications: BS/MS in a related field.",
        "Requirements: a degree in a related field.",
    ]
    for requirement in wordings:
        verdict, rule, reason = education_fit_filter(
            posting(description_html=f"<li>{requirement}</li>"), student
        )
        assert (verdict, rule) == ("kill", "education_fit"), requirement
        assert reason.startswith("requires a completed degree: "), reason
    passed = education_fit_filter(
        posting(description_html="<li>Requirements: enthusiasm and a portfolio.</li>"), student
    )
    assert passed[2] == "no clear completed-degree requirement"


#: Wordings whose "equivalent" is offered *instead of* the degree: it names
#: experience, or it names nothing at all and the rest of the sentence goes on to
#: describe the offer as a whole. The employer has stated a second branch, and
#: reading only the first is the kill direction. The first six are quoted from
#: ``data/postings/2026-08-18.jsonl``; the rest are wordings the corpus does not
#: contain, which is the half a replay can never reach.
_WAIVED_BY_AN_EQUIVALENT = (
    "You have a PhD (or equivalent industry experience) in computational biology, "
    "bioinformatics, computer science, biochemistry, structural biology, physics.",
    "Who You Are The Academic Foundation: You hold a PhD (or equivalent) in "
    "Pharmacokinetics, Drug Metabolism, Pharmacology, or a related field.",
    "Your Background: You should have earned your Ph.D. or equivalent with 5+ years "
    "of relevant process development experience.",
    "Your Background: Education & Experience BS/BA or equivalent in operations, life "
    "sciences, or a health-related field.",
    "Requirements: Master's degree or equivalent experience.",
    "Qualifications: Ph.D. or equivalent in Chemistry.",
    "Requirements: a Master's degree or the equivalent.",
    "Qualifications: an MBA or an equivalent track record of operating a business.",
    "Requirements: PhD or equivalent practical experience in the field.",
    "Qualifications: Master's degree, or equivalent combination of education and "
    "experience.",
    "Requirements: Bachelor's degree or equivalent work history.",
)

#: The counter-case, and it is the whole reason this rule is not one word wider.
#: Here "equivalent" names another *degree*, or the same degree in another *field
#: of study*. Every branch of these offers is still a degree, so every one of
#: them still states a degree requirement. Reading them as waivers would pass
#: Postings on a degree the employer genuinely requires — the same silent failure
#: as the wrong kill, in the direction nobody would notice. The first three are
#: quoted from the corpus; the rest are not in it.
_STILL_A_DEGREE_REQUIREMENT = (
    "QUALIFICATIONS Bachelor's degree in Business, Finance, Accounting, Economics "
    "or equivalent area.",
    "Qualifications Required MD, MD/PhD, or equivalent medical degree with board "
    "certification in Hematology.",
    "Qualifications: Degree in Pharmacy, Nursing, Epidemiology, Biosciences or "
    "equivalent degree.",
    "Requirements: Master's degree in Marketing or equivalent field.",
    "Qualifications: PhD in Biology or an equivalent scientific discipline.",
    "Requirements: Bachelor's degree in Economics or equivalent area of study.",
    "Qualifications: MBA or equivalent qualification.",
    "Requirements: BS in Chemistry or equivalent major.",
)


@pytest.mark.parametrize("requirement", _WAIVED_BY_AN_EQUIVALENT)
def test_an_equivalent_that_names_nothing_is_a_branch_of_the_offer(requirement: str) -> None:
    """A Posting offering something else in place of the degree has said so.

    "PhD (or equivalent industry experience)" states two ways in, and the matcher
    read one of them. The offer then reads narrower than the Posting wrote it,
    and narrower is always the kill direction: the Owner is never shown a Posting
    whose own wording says experience answers the same question the degree does.

    Asserted under the setting that kills every completed degree, because that is
    the setting the defect was reported under and the one ``profiles/example/``
    carries. A wrong kill here is silent — no review, no dashboard row,
    nothing downstream that records the mistake happened.
    """
    student = _education(kill_completed_degree=True)
    verdict, _, reason = education_fit_filter(
        posting(description_html=f"<li>{requirement}</li>"), student
    )
    assert verdict == "pass", reason
    assert education_fit_reading(posting(description_html=f"<li>{requirement}</li>"), student).fact == (
        "optional_degree"
    )


@pytest.mark.parametrize("requirement", _STILL_A_DEGREE_REQUIREMENT)
def test_an_equivalent_that_names_another_degree_or_field_still_requires_one(
    requirement: str,
) -> None:
    """The direction a blanket "or equivalent" rule would break, pinned.

    "Or equivalent area", "or equivalent medical degree", "or equivalent degree":
    the employer is naming another degree, or the same degree in another field of
    study, and has waived nothing. Three of these are corpus rows that decide
    correctly today, and a rule that read the word rather than what it names
    would flip all three into passes on a degree the employer requires.
    """
    student = _education(kill_completed_degree=True)
    verdict, rule, reason = education_fit_filter(
        posting(description_html=f"<li>{requirement}</li>"), student
    )
    assert (verdict, rule) == ("kill", "education_fit"), reason
    assert reason.startswith("requires a completed degree: "), reason


@pytest.mark.parametrize("requirement", _STILL_A_DEGREE_REQUIREMENT)
def test_the_widened_model_reads_those_same_wordings_as_a_stated_degree(
    requirement: str,
) -> None:
    """The verdict is not the evidence here; the reading is.

    Under an Owner holding a bachelor's these wordings decide three ways — a
    doctorate kills, a bachelor's is met and passes, a level the Posting never
    named cannot be placed and passes — and a waived degree would pass too. So
    the verdict cannot tell the readings apart and the fact token has to. Every
    one of these is a degree requirement the rule *read*: it kills, or it passes
    recording ``degree_met`` or ``degree_unplaced``. None of them may record
    ``optional_degree``, which is the rule saying it decided the degree was not
    binding, or ``clear``, which is the rule saying it found no degree at all.
    """
    reading = education_fit_reading(
        posting(description_html=f"<li>{requirement}</li>"), HOLDS_A_BACHELOR
    )
    if reading.verdict == "kill":
        assert reading.rule == "education_fit", reading.reason
        return
    assert reading.fact in {"degree_met", "degree_unplaced"}, f"{reading.fact}: {reading.reason}"


def test_an_equivalence_in_the_next_sentence_does_not_waive_this_degree() -> None:
    """A guard that reaches past the clause cancels requirements it never read.

    The equivalence a Posting writes about its *experience* bar says nothing
    about its degree bar, and a window wide enough to find it would waive every
    degree stated within a hundred characters of one.
    """
    student = _education(kill_completed_degree=True)
    next_sentence = posting(
        description_html=(
            "<li>Requirements: a Master's degree in Marketing. Five years of agency "
            "work or equivalent hands-on experience.</li>"
        )
    )
    verdict, rule, reason = education_fit_filter(next_sentence, student)
    assert (verdict, rule) == ("kill", "education_fit"), reason


def test_a_waived_degree_is_a_way_in_and_not_a_degree_removed_from_the_reading() -> None:
    """The direction this change could have broken, pinned before it could.

    A wish and an equivalence are not the same finding. Dropping a *wish* from
    the reading leaves the requirements the Posting states standing, which is
    right — "a Bachelor's is preferred" is no way in at all. Dropping an
    *equivalence* the same way would leave a stricter requirement elsewhere in
    the Posting to kill on a bar the Posting has already said can be cleared
    without a degree, which turns a Posting that passed into one that dies. The
    corpus contains no such wording, so only a generated one can hold it.
    """
    both = posting(
        description_html=(
            "<li>Requirements: PhD in Chemistry. Qualifications: Bachelor's degree or "
            "equivalent in Biology.</li>"
        )
    )
    holds_one = _education(holds="bachelor")
    verdict, _, reason = education_fit_filter(both, holds_one)
    assert verdict == "pass", reason
    assert education_fit_reading(both, holds_one).fact == "optional_degree"

    # The same wording under the setting that says the Owner has completed no
    # degree at all. The waived branch is no way in for them either — they have
    # neither the bachelor's nor anything the Posting would take instead that
    # this rule can read — so the doctorate still decides, exactly as it did
    # before this change. A waived offer counts only where the Profile placed it
    # within reach, and under that setting nothing ever is.
    assert education_fit_filter(both, _education(kill_completed_degree=True))[0] == "kill"

    # A wish still is one: "preferred" beside one degree does not open a way past
    # a doctorate the same Posting requires.
    wished = posting(
        description_html=(
            "<li>Requirements: PhD in Chemistry is required. An Associate degree is "
            "preferred.</li>"
        )
    )
    assert education_fit_filter(wished, _education(kill_completed_degree=True))[0] == "kill"


#: Wordings that state a degree as a *preference*. The guard for this only ever
#: read the character straight after the degree, so anything in between defeated
#: it — one adverb in "PhD highly preferred", and the noun in "A Bachelor's
#: degree is preferred", where the match lands on "Bachelor's" and "degree" sits
#: between it and the word that governs it. The failure is adjacency, not
#: adverbs. The first two are quoted from ``data/postings/2026-08-18.jsonl``.
_STATED_AS_A_PREFERENCE = (
    "Your Background Health sciences advanced or doctoral degree, such as a PharmD, "
    "MD, or PhD highly preferred. PA, MSN, NP, DNP accompanied by previous "
    "pharmaceutical industry experience.",
    "Qualifications: BS/MS in Life Sciences or Engineering a plus with minimum of "
    "4-6 years of relevant experience.",
    "A Bachelor's degree is preferred. Requirements: attention to detail.",
    "A Master's degree in Marketing is strongly preferred.",
    "Requirements: attention to detail. A PhD in Biology is ideal.",
    "Qualifications: a Bachelor's degree in a related field is a bonus.",
    "Qualifications: an MBA is nice to have.",
    "A doctorate is desirable.",
    "Requirements: a Bachelor's degree (strongly preferred) in Marketing.",
    "Requirements: a Bachelor's degree (preferred).",
)

#: The direction loosening a preference guard breaks, and the reason this one is
#: bounded three ways. A preference word after a degree says nothing about it
#: when a requirement word sits between them, when it belongs to the next
#: sentence, or when it is inside a bracket qualifying something else. The third
#: is quoted from the corpus and is the one a full-corpus replay passed clean:
#: Beam Therapeutics asks for a "BS degree in a technical discipline (Engineering
#: preferred)", where the *discipline* is preferred and the degree is required.
_STILL_A_STATED_DEGREE = (
    "Bachelor's degree required, Salesforce experience a plus.",
    "Requirements: a Master's degree in Marketing. Familiarity with SEO is a plus.",
    "Qualifications: BS degree in a technical discipline (Engineering preferred) 10+ "
    "years' experience leading maintenance personnel.",
    "Requirements: a Master's degree (Chemistry preferred) in a science.",
    "Qualifications: a Master's degree is required. Experience with paid social is "
    "preferred.",
    "Minimum Qualifications: PhD in Chemistry. Preferred Qualifications: publication "
    "record.",
    "Requirements: BS in Chemistry. Nice to have: familiarity with LC-MS.",
    "You must hold a Master's degree. Prior agency work is a bonus.",
)


@pytest.mark.parametrize("requirement", _STATED_AS_A_PREFERENCE)
def test_a_preference_word_governs_the_degree_it_follows(requirement: str) -> None:
    """A Posting that calls its degree preferred has not made it a requirement.

    The guard existed and read only the character straight after the match, so a
    Posting saying so in almost any natural word order was killed as though the
    degree were required. A wrong kill is silent: no review, no dashboard
    row, nothing downstream that records the Owner was never shown it.

    The vocabulary is the one the stated-years scan has always read. There was
    never a reason for the two to differ — a Posting writing "a plus" about its
    degree got no guard at all while one writing "a plus" about its years got
    one.
    """
    student = _education(kill_completed_degree=True)
    described = posting(description_html=f"<li>{requirement}</li>")
    verdict, _, reason = education_fit_filter(described, student)
    assert verdict == "pass", reason
    assert education_fit_reading(described, student).fact == "optional_degree"


@pytest.mark.parametrize("requirement", _STILL_A_STATED_DEGREE)
def test_a_preference_word_about_something_else_does_not_waive_the_degree(
    requirement: str,
) -> None:
    """The counter-direction, and the reason the forward reading is bounded.

    A preference word after a degree is about that degree only when nothing has
    intervened to make it about something else: a requirement word, a sentence
    boundary, or a bracket qualifying a different noun. Every one of these kills
    on ``main`` and must keep killing, or the fix for a silent wrong kill becomes
    a silent wrong pass on a degree the employer asks for.
    """
    student = _education(kill_completed_degree=True)
    verdict, rule, reason = education_fit_filter(
        posting(description_html=f"<li>{requirement}</li>"), student
    )
    assert (verdict, rule) == ("kill", "education_fit"), reason
    assert reason.startswith("requires a completed degree: "), reason


@pytest.mark.parametrize("requirement", _STILL_A_STATED_DEGREE)
def test_the_widened_model_still_reads_those_as_a_stated_degree(requirement: str) -> None:
    """Under an Owner holding a bachelor's the verdict cannot say which reading it is.

    A bachelor's-level requirement passes because the bar was cleared, which is
    the same verdict a waived degree gets. The fact token separates them: each of
    these is a degree the rule read, so it kills or records ``degree_met`` or
    ``degree_unplaced``, and none may record ``optional_degree``.
    """
    reading = education_fit_reading(
        posting(description_html=f"<li>{requirement}</li>"), HOLDS_A_BACHELOR
    )
    if reading.verdict == "kill":
        assert reading.rule == "education_fit", reading.reason
        return
    assert reading.fact in {"degree_met", "degree_unplaced"}, f"{reading.fact}: {reading.reason}"


def test_a_dotted_abbreviation_does_not_end_its_own_clause() -> None:
    """"Ph.D." writes a full stop the degree pattern does not consume.

    Read as the end of the sentence it cancels the equivalence written straight
    after it, which is how "Ph.D. or equivalent with 5+ years" — a corpus row —
    stayed a doctorate requirement while "PhD or equivalent with 5+ years" did
    not. Two spellings of one sentence must not decide differently.
    """
    student = _education(kill_completed_degree=True)
    for spelling in ("Ph.D.", "PhD", "Ph. D."):
        described = posting(
            description_html=(
                f"<li>Your Background: You should have earned your {spelling} or equivalent "
                "with 5+ years of relevant experience.</li>"
            )
        )
        verdict, _, reason = education_fit_filter(described, student)
        assert verdict == "pass", f"{spelling}: {reason}"

NO_BAND: tuple[str, ...] = ()


def test_a_ladder_with_no_accepted_rung_kills_nothing_on_the_ladder() -> None:
    """The property, not one title: an empty band decides nothing.

    ``band`` used to fall back to ``(0, 0)`` when nothing was accepted, which is
    not "no band" — it is "the bottom rung only", the most aggressive band a
    ladder can express. Every rung above rung 0 was then outside it and killed
    confidently. This generates titles from the Profile's own rung vocabulary,
    the same way the band fuzz above does, and asserts that a Profile accepting
    no rung kills none of them: what cannot be placed passes.

    ``exclude`` is a separate rule with no conservative escape, and it is
    independent of the band, so its kills are skipped here and pinned below.
    """
    policy = _banded(NO_BAND)
    rules = policy.role_target
    titles = _fuzz_titles(_ladder_vocabulary(), limit=1200)
    considered = 0

    assert rules.band is None
    for title in titles:
        if rules.exclude.search(title):
            continue
        considered += 1
        verdict, _, reason = role_target_filter(posting(title=title), policy)
        assert verdict == "pass", f"{title!r} was killed by a ladder accepting nothing: {reason}"

    assert considered > 100, f"only {considered} of {len(titles)} generated titles were considered"


def test_a_ladder_with_no_accepted_rung_records_that_it_could_not_place_the_title() -> None:
    """The abstention says which rule declined and why, in the rule's own reason.

    Where it stops: ``apply_filters`` keeps only a kill's reason and replaces
    every pass with one aggregate line, so this text does not reach the appended
    Filter Decision — that is true of every filter's pass reason on this pipeline
    and is not something this change alters. What it pins is that the rule
    returns a reason naming the missing band rather than an empty or borrowed
    one, so the abstention is legible to anything reading this filter directly.
    """
    verdict, rule, reason = role_target_filter(
        posting(title="Senior Director, Platform Biology"), _banded(NO_BAND)
    )

    assert (verdict, rule) == ("pass", "role_target")
    assert reason == "the Profile names no seniority band to place the title in; passed conservatively"


@pytest.mark.parametrize(
    "accept",
    [NO_BAND, ("intern",), ("mid",), ("executive",), BOTTOM_ANCHORED, MID_SENIOR],
)
def test_no_filter_decision_ever_names_an_empty_seniority_band(accept: tuple[str, ...]) -> None:
    """A reason that names a band must have a band to name.

    With nothing accepted, the helper that lists the accepted rungs returned the
    empty string and the kill read "above the  seniority band the Profile
    targets" — a confident kill against a band that was not there. The double
    space was the visible half; the kill was the defect. Swept over several band
    shapes, including both ends of the ladder, because a wording bug that only
    one band shape reaches is a wording bug the next Profile finds.
    """
    policy = _banded(accept)
    for title in _fuzz_titles(_ladder_vocabulary(), limit=400):
        reason = role_target_filter(posting(title=title), policy)[2]
        assert "  " not in reason, f"{reason!r} names an empty band"
        assert "the  " not in reason


NAMED_SPLITS: tuple[str, ...] = (
    "Loading / Dock",
    "Security / Guard",
    "Real / Estate Analyst",
    "Engineer I/II",
    "Technical Lead / AWS / SaaS",
    "Scientist II / Senior ML Scientist",
    "Research Associate or Associate Scientist",
)

#: Every way an employer writes the punctuation this filter splits on, including
#: the doubled and unspaced forms. A hand-written list of titles only covers the
#: separators whoever wrote it thought of, and the shape that broke this branch
#: was the one nobody thought of.
SEPARATORS: tuple[str, ...] = (
    "/", " / ", "/ ", " /", "//", " // ", "  /  ", " or ", " OR ", " or / ", "/ or ",
)


def _split_titles() -> list[str]:
    """Titles built by splitting each excluded term across every separator shape."""
    terms = ("loading dock", "security guard", "real estate", "account executive")
    return [
        shape.format(left=" ".join(words[:cut]), right=" ".join(words[cut:]), sep=separator)
        for term in terms
        for words in [term.split()]
        for cut in range(1, len(words))
        for separator in SEPARATORS
        for shape in ("{left}{sep}{right}", "Senior {left}{sep}{right}", "{left}{sep}{right}, Boston")
    ]


def test_every_role_a_title_offers_is_a_span_of_the_title() -> None:
    """``alternatives`` may not assemble a phrase the employer never wrote.

    A fragment that is not an offer of its own is merged back into the previous
    one. Merging with a space synthesised "Loading Dock" out of "Loading / Dock",
    and ``exclude`` matches its terms literally between word boundaries — so a
    Profile excluding ``loading dock`` killed a title that never contained the
    phrase, on the one branch of this filter that has no conservative escape.
    Merging through the text the employer wrote keeps every offer a span.

    Asserted as the property over every separator shape rather than over a list
    of titles: merging through the *last* separator alone passes every named
    case above and still fabricates "Loading / Dock" out of "Loading / / Dock",
    because the separator in the middle is dropped with the empty fragment
    beside it. The span property is the thing that must hold; the named titles
    are the regressions that motivated it.
    """
    titles = [*NAMED_SPLITS, *_split_titles()]
    assert len(titles) > 100

    for title in titles:
        for offer in POLICY.role_target.alternatives(title):
            assert offer in title, f"{offer!r} is not a span of {title!r}"


def test_an_excluded_term_split_across_a_slash_is_not_assembled_into_a_kill() -> None:
    """The three terms in this Owner's ``exclude`` a single slash could synthesise.

    Swept over every separator shape, because the kill came from how the offer
    was assembled and not from which punctuation the employer happened to use.
    """
    for term in ("loading dock", "security guard", "real estate"):
        left, right = term.split()
        for separator in SEPARATORS:
            title = f"{left.title()}{separator}{right.title()}"
            verdict, _, reason = role_target_filter(posting(title=title), POLICY)

            assert verdict == "pass", f"{title!r} was killed on a phrase it never held: {reason}"
