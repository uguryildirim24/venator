"""Loading, validating, and choosing a Profile."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from venator.match.filters import role_target_filter
from venator.profile import (
    PROFILE_ENV,
    ProfileError,
    load_profile,
    resolve_profile_dir,
)

REPOSITORY = Path(__file__).parents[2]


def write_profile(directory: Path, **files: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / f"{name}.yaml").write_text(text, encoding="utf-8")
    return directory


def test_a_profile_of_three_empty_files_loads(tmp_path: Path) -> None:
    profile = load_profile(write_profile(tmp_path / "empty", resume="", constraints="", targeting=""))

    assert profile.name == "empty"
    assert profile.scaffold is False
    assert profile.filters.enabled == ()
    assert profile.sources.configured is False
    assert profile.search.queries == ()
    assert profile.resume == {} and profile.constraints == {}


def test_a_profile_directory_that_does_not_exist_says_what_was_expected(tmp_path: Path) -> None:
    with pytest.raises(ProfileError) as error:
        load_profile(tmp_path / "nobody")

    assert "resume.yaml, constraints.yaml, targeting.yaml" in str(error.value)


@pytest.mark.parametrize(
    ("targeting", "expected"),
    [
        ("match:\n  threshold: 140\n", "targeting.yaml: match.threshold is retired (decision 17)"),
        ("match:\n  threshold: high\n", "targeting.yaml: match.threshold is retired (decision 17)"),
        ("scoring:\n  applicant: leftover\n", "targeting.yaml: scoring is retired (decision 17)"),
        ("match: {}\n", "targeting.yaml: match is retired (decision 17); delete the match: block"),
        ("search:\n  remote: sometimes\n", "targeting.yaml: search.remote must be one of ['required', 'preferred', 'acceptable', 'no'] (got 'sometimes')"),
        ("search:\n  queries: biotech\n", "targeting.yaml: search.queries must be a list of strings (got 'biotech')"),
        ("search:\n  queries: ['', ok]\n", "targeting.yaml: search.queries.0 must be a non-empty string (got '')"),
        ("filters:\n  enabled: [salary]\n", "targeting.yaml: filters.enabled.0 must name a Hard Filter"),
        ("filters:\n  education_fit:\n    kill_completed_degree: maybe\n", "targeting.yaml: filters.education_fit.kill_completed_degree must be true or false (got 'maybe')"),
        ("filters:\n  work_authorization:\n    restriction_patterns: ['(unclosed']\n", "restriction_patterns.0 is not a valid regular expression"),
        ("filters:\n  education_fit:\n    experience_years_kill: 20\n", "must not exceed experience_years_implausible (15) (got 20)"),
        ("sources:\n  names:\n    ghost: null\n", "targeting.yaml: sources.names.ghost must be an employer display name"),
        (
            "sources:\n  names:\n    benchling: Benchling\n  boards:\n    ashby: [benchling, asimov]\n",
            "targeting.yaml: sources.names must name every board this Profile registers — ['asimov']",
        ),
        ("profile: [not, a, mapping]\n", "targeting.yaml: profile must be a mapping"),
    ],
)
def test_a_malformed_profile_is_rejected_by_file_and_field(
    tmp_path: Path, targeting: str, expected: str
) -> None:
    directory = write_profile(tmp_path / "broken", targeting=targeting)

    with pytest.raises(ProfileError) as error:
        load_profile(directory)

    message = str(error.value)
    assert expected in message
    assert str(directory / "targeting.yaml") in message


def test_unparseable_yaml_names_the_file(tmp_path: Path) -> None:
    directory = write_profile(tmp_path / "broken", targeting="match:\n  threshold: [\n")

    with pytest.raises(ProfileError) as error:
        load_profile(directory)

    assert "targeting.yaml is not valid YAML" in str(error.value)


def test_a_profile_file_holding_a_list_is_rejected(tmp_path: Path) -> None:
    directory = write_profile(tmp_path / "broken", resume="- one\n- two\n")

    with pytest.raises(ProfileError) as error:
        load_profile(directory)

    assert "resume.yaml must contain a YAML mapping, not a list" in str(error.value)


def test_the_only_non_scaffold_profile_is_implied(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    write_profile(profiles / "someone", targeting="profile:\n  name: someone\n")
    write_profile(profiles / "example", targeting="profile:\n  scaffold: true\n")

    assert resolve_profile_dir(profiles_dir=profiles, environ={}) == profiles / "someone"


def test_two_real_profiles_refuse_to_be_guessed_between_and_list_the_choices(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    write_profile(profiles / "ada", targeting="")
    write_profile(profiles / "grace", targeting="")

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(profiles_dir=profiles, environ={})

    message = str(error.value)
    assert "more than one Profile exists" in message
    assert "available: ada, grace" in message
    assert PROFILE_ENV in message


def test_the_environment_names_a_profile_when_more_than_one_exists(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    write_profile(profiles / "ada", targeting="")
    write_profile(profiles / "grace", targeting="")

    resolved = resolve_profile_dir(profiles_dir=profiles, environ={PROFILE_ENV: "grace"})

    assert resolved == profiles / "grace"


def test_an_unknown_profile_name_lists_the_real_ones(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    write_profile(profiles / "ada", targeting="")

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir("nobody", profiles_dir=profiles, environ={})

    assert "no Profile named 'nobody'" in str(error.value)
    assert "available: ada" in str(error.value)


def test_no_profile_at_all_says_how_to_make_one(tmp_path: Path) -> None:
    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(profiles_dir=tmp_path / "profiles", environ={})

    assert "no Profile exists" in str(error.value)


@pytest.mark.parametrize("named", ["", "   ", "/etc", "../", "..", ".", "profiles/ada", "~/ada"])
def test_a_profile_path_is_refused_where_a_profile_name_belongs(tmp_path: Path, named: str) -> None:
    """`--profile ''` used to resolve to '.', loading a Profile with no Hard Filter."""
    profiles = tmp_path / "profiles"
    write_profile(profiles / "ada", targeting="")

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(named, profiles_dir=profiles, environ={})

    message = str(error.value)
    assert "is empty" in message or "takes the name of a Profile" in message


def test_an_empty_environment_variable_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    write_profile(profiles / "ada", targeting="")

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(profiles_dir=profiles, environ={PROFILE_ENV: ""})

    assert f"${PROFILE_ENV} is empty" in str(error.value)


def test_a_profile_directory_outside_the_checkout_is_accepted(tmp_path: Path, monkeypatch) -> None:
    """The Install's data directory is the boundary, not the checkout (ADR-0002)."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = write_profile(tmp_path / "elsewhere", targeting="profile:\n  name: ada\n")
    monkeypatch.chdir(checkout)

    assert resolve_profile_dir(directory=outside, environ={}) == outside


def test_a_directory_that_is_not_a_profile_is_refused_rather_than_loaded_empty(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "not-a-profile").mkdir()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(directory="not-a-profile", environ={})

    message = str(error.value)
    assert "is not a Profile directory" in message
    assert "pass every Posting" in message


def test_a_profile_directory_inside_the_checkout_is_accepted(tmp_path: Path, monkeypatch) -> None:
    write_profile(tmp_path / "elsewhere" / "ada", targeting="profile:\n  name: ada\n")
    monkeypatch.chdir(tmp_path)

    assert resolve_profile_dir(directory="elsewhere/ada", environ={}) == Path("elsewhere/ada")


def test_a_malformed_profile_names_itself_instead_of_becoming_ambiguity(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    write_profile(profiles / "ada", targeting="")
    write_profile(profiles / "broken", targeting="match:\n  threshold: [\n")

    with pytest.raises(ProfileError) as error:
        resolve_profile_dir(profiles_dir=profiles, environ={})

    message = str(error.value)
    assert str(profiles / "broken") in message
    assert "is not valid YAML" in message
    assert "more than one Profile exists" not in message


def test_a_filter_policy_with_nothing_enabled_is_refused_rather_than_passing_everything(
    tmp_path: Path,
) -> None:
    """The fail-open shape: a complete rule block and no `enabled:` list."""
    directory = write_profile(
        tmp_path / "unenabled",
        targeting=(
            "filters:\n"
            "  role_target:\n"
            "    exclude:\n"
            "      terms: [sales]\n"
        ),
    )

    with pytest.raises(ProfileError) as error:
        load_profile(directory)

    message = str(error.value)
    assert "targeting.yaml: filters.enabled is required" in message
    assert "every Posting would pass unfiltered" in message


def test_a_leftover_scoring_block_is_refused(tmp_path: Path) -> None:
    directory = write_profile(
        tmp_path / "half", targeting="scoring:\n  applicant: someone who exists.\n"
    )

    with pytest.raises(ProfileError) as error:
        load_profile(directory)

    assert "targeting.yaml: scoring is retired (decision 17)" in str(error.value)
    assert "delete the scoring: block" in str(error.value)


def test_the_shipped_profiles_all_load() -> None:
    for directory in sorted((REPOSITORY / "profiles").iterdir()):
        profile = load_profile(directory)

        assert profile.name


def test_search_eligibility_schema_loads_and_auto_enables_the_rule(tmp_path: Path) -> None:
    directory = write_profile(
        tmp_path / "timed",
        targeting=(
            "search:\n"
            "  remote: required\n"
            "  salary_floor:\n"
            "    amount: 70000\n"
            "    currency: USD\n"
            "    period: year\n"
            "  timing:\n"
            "    allowed_seasons: [summer]\n"
            "    expected_graduation: 2027-05-20\n"
            "    expected_graduation_confirmed: true\n"
            "    enrollment: current\n"
            "    reference_date: 2026-09-04\n"
        ),
    )

    profile = load_profile(directory)

    assert profile.search.salary_floor == 70000
    assert profile.search.salary_currency == "USD"
    assert profile.search.salary_period == "year"
    assert profile.search.timing.allowed_seasons == ("summer",)
    assert profile.search.timing.expected_graduation == date(2027, 5, 20)
    assert profile.search.timing.expected_graduation_precision == "day"
    assert profile.filters.enabled == ("eligibility",)
    assert profile.filters.search == profile.search


def test_graduation_month_keeps_month_precision(tmp_path: Path) -> None:
    directory = write_profile(
        tmp_path / "month",
        targeting=(
            "search:\n"
            "  timing:\n"
            "    expected_graduation: '2027-05'\n"
            "    expected_graduation_confirmed: true\n"
        ),
    )

    timing = load_profile(directory).search.timing

    assert timing.expected_graduation == date(2027, 5, 1)
    assert timing.expected_graduation_precision == "month"


def test_confirmed_graduation_without_a_date_is_rejected(tmp_path: Path) -> None:
    directory = write_profile(
        tmp_path / "broken",
        targeting=(
            "search:\n"
            "  timing:\n"
            "    expected_graduation_confirmed: true\n"
        ),
    )

    with pytest.raises(ProfileError, match="expected_graduation.*required"):
        load_profile(directory)


def test_a_role_target_band_must_name_rungs_that_exist(tmp_path: Path) -> None:
    directory = tmp_path / "profile"
    directory.mkdir()
    (directory / "targeting.yaml").write_text(
        "filters:\n"
        "  enabled: [role_target]\n"
        "  role_target:\n"
        "    levels:\n"
        "      - name: entry\n"
        "        title_terms: [junior]\n"
        "    accept: [entry, mid]\n",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="must name a rung"):
        load_profile(directory)


def test_a_profile_with_no_role_target_kills_nothing_on_role(tmp_path: Path) -> None:
    directory = tmp_path / "profile"
    directory.mkdir()
    (directory / "targeting.yaml").write_text(
        "filters:\n  enabled: [role_target]\n", encoding="utf-8"
    )

    policy = load_profile(directory).filters

    assert not policy.role_target.configured
    assert role_target_filter({"title": "Senior Director, Sales"}, policy)[0] == "pass"


def test_only_the_leading_segment_of_a_title_offers_a_rung() -> None:
    """``alternatives`` returns roles, not roles plus the department beside them.

    The qualifier used to be re-attached to every offer, so the ladder scanned a
    department name as though it named the role: "Research Associate, Head and
    Neck Oncology" was read for a rung across the words "Head and Neck", and
    "Clinical Research Coordinator, CTO" across a Clinical Trials Office. What a
    title says after its first comma, bracket or dash is context — the team, the
    product, the site, the shift — and no Profile's rung vocabulary can be made
    safe while it is read as a job title.
    """
    rules = load_profile(REPOSITORY / "profiles" / "example").filters.role_target

    assert rules.alternatives("Technical Leader (Cambridge)") == ("Technical Leader",)
    assert rules.alternatives("Scientist I, External Manufacturing (CMO)") == ("Scientist I",)
    assert rules.alternatives("Research Associate, Head and Neck Oncology") == (
        "Research Associate",
    )
    assert rules.alternatives("Sr. Manager/ Associate Director - Boston") == (
        "Sr. Manager",
        "Associate Director",
    )


def test_a_title_that_opens_with_a_bracket_still_names_a_role() -> None:
    """The boundary is the first one with a role in front of it.

    "(Senior) Director, Portfolio Strategy" and "[Remote] Principal Software
    Developer" prefix the role rather than following it, so taking the first
    boundary unconditionally leaves the leading segment empty.

    Asserted as what it is: three corpus titles open with a bracket, and this
    docstring used to say they "would reach the judge for want of a role to
    read". They would not. ``alternatives`` already falls back to the whole
    title when it splits nothing out, so without this guard the ladder reads
    "(Senior) Director, Portfolio Strategy, Life Sciences" entire — and still
    kills it. Measured over the corpus, the guard moves 0 verdicts and 0 reasons.
    It is kept because it makes ``alternatives`` return a role rather than a
    title plus its department, which is the whole point of the function, not
    because it rescues a Posting.
    """
    rules = load_profile(REPOSITORY / "profiles" / "example").filters.role_target

    assert rules.alternatives("(Senior) Director, Portfolio Strategy") == ("(Senior) Director",)
    assert rules.alternatives("[Remote] Principal Software Developer - Oracle Health") == (
        "[Remote] Principal Software Developer",
    )


def test_a_profile_that_registers_boards_and_names_none_of_them_loads(tmp_path: Path) -> None:
    """Absent means "not configured", never "invalid" — even here.

    A Profile may register boards and write no ``sources.names`` at all: Discover
    polls them, Dedup resolves no ATS employer, and every ATS Posting simply
    keeps the pass it earned on its own. That is a degraded Dedup, not a broken
    Profile, and refusing to run would be refusing over a missing field. What
    the loader rejects is the *half*-written registry — see the test below.
    """
    profile = load_profile(
        write_profile(
            tmp_path / "unnamed",
            targeting="sources:\n  boards:\n    ashby: [benchling, asimov]\n",
        )
    )

    assert profile.sources.names == {}
    assert profile.sources.boards == {"ashby": ("benchling", "asimov")}


def test_registering_boards_and_naming_none_of_them_warns_and_still_loads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Loading is not the same as loading silently.

    The measurement behind the half-written-registry error was run against a
    registry with *zero* names, which is this state — so the harm the error
    prevents is exactly the harm this state still carries. It stays legal
    because absent means "not configured", and it is now said out loud: how
    many boards went unnamed, and that duplicate detection can therefore kill a
    different Posting than it should.
    """
    profile = load_profile(
        write_profile(
            tmp_path / "unnamed",
            targeting="sources:\n  boards:\n    ashby: [benchling, asimov]\n    greenhouse: [asimov]\n",
        )
    )
    warning = capsys.readouterr().err

    assert profile.sources.names == {}
    assert "warning:" in warning
    # Two distinct tokens across three registrations, counted as tokens.
    assert "registers 2 boards" in warning
    assert "names none of them" in warning
    assert "kill a different Posting than it should" in warning


def test_a_fully_named_registry_warns_about_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The warning has to be quiet in the configured case or it is noise."""
    load_profile(
        write_profile(
            tmp_path / "named",
            targeting=(
                "sources:\n"
                "  names:\n"
                "    benchling: Benchling\n"
                "  boards:\n"
                "    ashby: [benchling]\n"
            ),
        )
    )

    assert capsys.readouterr().err == ""


def test_a_profile_registering_no_boards_at_all_warns_about_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing registered is nothing to name, and nothing to say."""
    load_profile(write_profile(tmp_path / "bare", targeting=""))

    assert capsys.readouterr().err == ""


def test_a_board_registered_but_left_unnamed_is_refused_before_it_can_cost_an_opening(
    tmp_path: Path,
) -> None:
    """The narrow case, and why it is an error rather than a warning.

    Dedup reaches an ATS Posting's employer through ``sources.names`` alone, so
    an unnamed board's Postings join no duplicate group. That reads like the
    conservative direction — nothing of theirs is ever merged away — but it is
    not, because location compatibility is deliberately non-transitive: the
    member that drops out was also the one keeping two mutually-compatible
    Postings in separate groups. Replaying the pass over randomized corpora, a
    missing name never raises the number of Postings killed and in about 4% of
    corpora *changes which* Posting is killed, so a Posting the complete
    registry would have kept is killed as a duplicate instead. The Owner is
    shown a queue of the right length with a real opening missing from it, and
    nothing anywhere says so. A registry that names some boards and not others
    is a registry someone is maintaining and has fallen behind on, which is what
    happened when the board list went from 13 boards to 35.
    """
    directory = write_profile(
        tmp_path / "half",
        targeting=(
            "sources:\n"
            "  names:\n"
            "    benchling: Benchling\n"
            "  boards:\n"
            "    ashby: [benchling]\n"
            "    workday: [amgen.wd1~Careers]\n"
        ),
    )

    with pytest.raises(ProfileError) as error:
        load_profile(directory)

    message = str(error.value)
    assert "sources.names must name every board this Profile registers" in message
    assert "['amgen.wd1~Careers']" in message
    assert "benchling" not in message


def test_every_committed_profile_names_every_board_it_registers() -> None:
    """The rule above, held against the Profiles that actually ship.

    The loader enforces this on any Profile it is handed; this asserts the ones
    in the repository satisfy it today, so a board added to ``sources.boards``
    without its display name fails here as well as at load time.
    """
    for directory in sorted((REPOSITORY / "profiles").iterdir()):
        if not directory.is_dir():
            continue
        registry = load_profile(directory).sources
        registered = {token for tokens in registry.boards.values() for token in tokens}

        assert not registry.names or not registered - set(registry.names), directory.name


def _education_profile(directory: Path, block: str) -> Path:
    return write_profile(
        directory,
        resume="",
        constraints="",
        targeting=(
            "filters:\n"
            "  enabled: [education_fit]\n"
            "  education_fit:\n"
            f"{block}"
        ),
    )


def test_a_profile_states_what_the_owner_holds_and_what_is_in_progress(tmp_path: Path) -> None:
    profile = _education_profile(
        tmp_path / "graduate", "    holds: bachelor\n    in_progress: master\n"
    )
    rules = load_profile(profile).filters.education_fit
    assert (rules.holds, rules.in_progress) == ("bachelor", "master")
    assert rules.kill_completed_degree is False
    assert rules.reads_degrees and rules.attainment_configured


def test_the_two_education_models_cannot_both_be_set(tmp_path: Path) -> None:
    """One says the Owner has completed no degree; the other says which one.

    Both at once is a Profile stating two incompatible things about the same
    Owner, and guessing which was meant is how a Hard Filter starts deciding
    something nobody wrote down.
    """
    profile = _education_profile(
        tmp_path / "both", "    kill_completed_degree: true\n    holds: bachelor\n"
    )
    with pytest.raises(ProfileError) as error:
        load_profile(profile)
    assert "kill_completed_degree" in str(error.value)
    assert "set one model or the other" in str(error.value)


def test_a_qualification_in_progress_may_not_sit_below_one_already_held(tmp_path: Path) -> None:
    profile = _education_profile(
        tmp_path / "backwards", "    holds: master\n    in_progress: bachelor\n"
    )
    with pytest.raises(ProfileError) as error:
        load_profile(profile)
    assert "must not be below holds (master)" in str(error.value)


def test_a_qualification_the_ladder_does_not_name_is_refused_by_name(tmp_path: Path) -> None:
    """Not silently ignored: an unread rung is an education rule that stopped running."""
    profile = _education_profile(tmp_path / "unknown", "    holds: postdoc\n")
    with pytest.raises(ProfileError) as error:
        load_profile(profile)
    assert "must be one of high_school, associate, bachelor, master, doctorate" in str(error.value)


def test_an_education_block_that_says_nothing_kills_nothing(tmp_path: Path) -> None:
    """Absent means "not configured", never "the Owner holds nothing"."""
    rules = load_profile(_education_profile(tmp_path / "silent", "    {}\n")).filters.education_fit
    assert (rules.holds, rules.in_progress, rules.kill_completed_degree) == (None, None, False)
    assert not rules.reads_degrees and not rules.configured


LADDER = """
filters:
  enabled: [role_target]
  role_target:
    label: seniority band
    levels:
      - name: entry
        title_terms: [coordinator]
      - name: mid
        title_terms: [manager]
{accept}{exclude}
"""


@pytest.mark.parametrize(
    "accept",
    ["", "    accept: []\n", "    accept:\n"],
    ids=["absent", "empty-list", "null"],
)
@pytest.mark.parametrize(
    "exclude",
    ["", "    exclude:\n      terms: [recruiter]\n"],
    ids=["no-exclude", "with-exclude"],
)
def test_a_ladder_the_profile_accepts_no_rung_of_is_refused(
    tmp_path: Path, accept: str, exclude: str
) -> None:
    """A Profile that writes a ladder and accepts nothing on it contradicts itself.

    ``accept`` empty is not "the bottom rung only", which is what the filter used
    to read it as — it killed every Posting above rung 0 and named a band that
    did not exist. The filter now abstains, and this refuses the Profile before
    anyone has to rely on that.

    Both fields are named, because the fix is to say which list was meant. The
    refusal keys on ``levels`` and ``accept`` alone: ``exclude`` describes
    functions rather than rungs and is read whether or not a ladder exists, so
    the same broken ladder is refused with or without it.
    """
    directory = write_profile(
        tmp_path / f"broken{len(accept)}{len(exclude)}",
        targeting=LADDER.format(accept=accept, exclude=exclude),
    )

    with pytest.raises(ProfileError) as error:
        load_profile(directory)

    message = str(error.value)
    assert "filters.role_target.accept" in message
    assert "levels" in message
    assert "'entry', 'mid'" in message


def test_a_profile_that_declares_no_ladder_at_all_still_loads(tmp_path: Path) -> None:
    """The line between "not configured" and "contradictory".

    Absent means "not configured" and must never make a stage refuse to run. A
    Profile with no ``role_target`` block, an empty one, or one that names
    excluded functions without a ladder has said nothing about seniority — that
    is a legitimate state, it loads, and it kills nothing on the rung. Only a
    Profile that *writes* ``levels:`` and then accepts no rung of it is refused,
    because writing a ladder is a statement and "I will take nothing on it" is
    the statement that contradicts it.
    """
    for name, targeting in {
        "none": "filters:\n  enabled: [role_target]\n",
        "empty-block": "filters:\n  enabled: [role_target]\n  role_target: {}\n",
        "empty-levels": "filters:\n  enabled: [role_target]\n  role_target:\n    levels: []\n",
        "exclude-only": (
            "filters:\n  enabled: [role_target]\n  role_target:\n"
            "    exclude:\n      terms: [recruiter]\n"
        ),
    }.items():
        policy = load_profile(write_profile(tmp_path / name, targeting=targeting)).filters

        assert policy.role_target.band is None, name
        assert role_target_filter({"title": "Senior Director, Sales"}, policy)[0] == "pass", name
