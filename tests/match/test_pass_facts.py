"""What the store now records about a Posting that *passed*.

A Filter Decision has always explained a kill and never a pass. The kill names
the wording it fired on; the pass named the rules it survived and stopped there.
So the corpus could answer "why was this Posting rejected" and could not answer
"why did this one get through", and every audit under ``docs/research/`` has had
to reconstruct the second answer by reading ``venator.match.filters``.

That asymmetry is backwards. A wrong pass costs one more Posting to review. A wrong *rule*
— one that places no title it reads, one that waives every restriction it finds
— passes everything, costs the whole queue, and shows up nowhere, because the
only trace it leaves is the pass it wrote no reason for.

So each rule now derives a short token on a pass and the row carries them.
Three properties make that safe to add to an append-only store that ships in the
repository (docs/adr/0001-git-as-transport.md), and all three are pinned here:

* it changes no verdict, rule or kill reason — ``test_filters_golden`` replays
  all fixture Postings against a fixture that needed no regeneration;
* it is outside ``filters_version``, so it replays nothing; and
* it is tokens, not prose, so it can be counted, and it costs 123 bytes on a
  pass row and nothing at all on a kill.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from venator.match.filters import (
    apply_filters,
    education_fit_reading,
    role_target_reading,
    work_authorization_reading,
)
from venator.match.run import decide, run
from venator.profile import load_profile
from venator.profile.schema import EducationFitPolicy, FilterPolicy, Matcher, RoleTargetPolicy

REPOSITORY = Path(__file__).parents[2]
CORPUS = REPOSITORY / "data" / "postings" / "2026-08-18.jsonl"

ROLE_TARGETING = """\
profile:
  name: role-fixture
filters:
  enabled: [role_target]
  role_target:
    levels:
      - name: intern
        title_terms: [intern]
      - name: senior
        title_terms: [senior, director]
    accept: [intern]
"""

#: Tokens that name a reading rather than a rung. ``role_target`` records the
#: Profile's own name for the level a title placed at, so these three are
#: reserved and a Profile that named a rung any of them would be
#: indistinguishable from a Posting the ladder declined to place. No committed
#: Profile does, and
#: ``test_no_committed_profile_names_a_rung_that_shadows_a_reserved_token``
#: keeps it that way rather than leaving it assumed.
RESERVED = ("unplaced", "unconfigured", "unbanded")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def only_day_file(decisions_dir: Path) -> Path:
    """The single day file ``append_decisions`` wrote, whatever today's date is."""
    days = sorted(decisions_dir.glob("*.jsonl"))
    assert len(days) == 1, days
    return days[0]


def owner_profile():
    return load_profile(REPOSITORY / "profiles" / "example")


def role_profile(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "targeting.yaml").write_text(ROLE_TARGETING, encoding="utf-8")
    return load_profile(directory)


def corpus_facts() -> dict[str, dict[str, int]]:
    """Every pass fact the Owner's Profile derives over the committed corpus."""
    counts: dict[str, dict[str, int]] = {}
    for _, verdict, _, _, facts in decide(read_jsonl(CORPUS), owner_profile()):
        if verdict != "pass" or facts is None:
            continue
        for rule, fact in facts.items():
            counts.setdefault(rule, {})[fact] = counts.setdefault(rule, {}).get(fact, 0) + 1
    return counts


def test_a_kill_carries_no_facts_because_the_rules_after_it_never_ran() -> None:
    """Recording tokens for rules that never saw the Posting would be an invention."""
    profile = owner_profile()
    killed = {"key": "source:board:1", "title": "Senior Director", "location": "San Diego, CA"}

    verdict, rule, _, facts = apply_filters(killed, profile.filters)

    assert (verdict, rule) == ("kill", "role_target")
    assert facts == {}


def test_a_profile_with_no_hard_filter_passes_with_an_empty_mapping_not_a_missing_one() -> None:
    """Absent must keep meaning "this row predates the field", and only that.

    An unconfigured Profile kills nothing and still records a Filter Decision
    (CLAUDE.md). It derives no facts either — but "no rule ran" is itself a
    finding, and writing nothing for it would make absence mean two things at
    once: a Profile with no Hard Filter, and a row written before this existed.
    """
    verdict, rule, _, facts = apply_filters({"key": "source:board:1", "title": "Intern"})

    assert (verdict, rule) == ("pass", None)
    assert facts == {}


def test_a_degree_the_rule_read_and_did_not_require_is_recorded_as_one() -> None:
    policy = owner_profile().filters
    optional = {"description_html": "<p>A Bachelor's degree is preferred but not required.</p>"}
    nothing = {"description_html": "<p>Join a growing laboratory team.</p>"}

    assert education_fit_reading(optional, policy).fact == "optional_degree"
    assert education_fit_reading(nothing, policy).fact == "clear"


def test_the_widened_model_tells_a_cleared_degree_apart_from_one_it_could_not_place() -> None:
    """Three readings, three tokens — the point of recording a pass at all.

    A Profile that says which qualification the Owner holds passes a Posting for
    two quite different reasons, and they need opposite audits. ``degree_met`` is
    a stated requirement the Profile placed at or below what the Owner holds: the
    rule read the bar and the Owner clears it. ``degree_unplaced`` is a stated
    requirement the Profile could *not* place — a level the Posting never named,
    or one at a qualification still in progress — so the pass is an abstention.

    Collapsing either into ``clear`` would say the Posting asked for no
    qualification at all, which is false of both, and would leave the abstentions
    uncountable — the exact blindness this field exists to remove.
    """
    policy = FilterPolicy(education_fit=EducationFitPolicy(holds="bachelor", in_progress="master"))

    met = {"description_html": "<p>Requirements: Bachelor's degree in Biology required.</p>"}
    offer = {"description_html": "<p>Requirements: BS/MS in Biology required.</p>"}
    in_progress = {"description_html": "<p>Requirements: Master's degree required.</p>"}
    unreadable = {"description_html": "<p>Requirements: a degree in a related field is required.</p>"}
    nothing = {"description_html": "<p>Join a growing laboratory team.</p>"}
    above = {"description_html": "<p>Requirements: PhD in Biology required.</p>"}

    assert education_fit_reading(met, policy).fact == "degree_met"
    # Read at its easiest branch, so this is a placement and not an abstention.
    assert education_fit_reading(offer, policy).fact == "degree_met"
    assert education_fit_reading(in_progress, policy).fact == "degree_unplaced"
    assert education_fit_reading(unreadable, policy).fact == "degree_unplaced"
    assert education_fit_reading(nothing, policy).fact == "clear"
    # A kill carries no fact: the reason already names the evidence.
    killed = education_fit_reading(above, policy)
    assert (killed.verdict, killed.fact) == ("kill", None)


def test_a_truncated_posting_is_recorded_as_unread_rather_than_as_cleared() -> None:
    """39.3 % of this corpus is a 500-character aggregator snippet.

    Silence from a filter that could not reach the requirements section is
    ignorance, not a finding — the distinction the reason strings already draw,
    now drawn in a field that can be counted.
    """
    policy = owner_profile().filters
    snippet = {"snippet": True, "description_html": "<p>A growing team…</p>"}

    assert work_authorization_reading(snippet, policy).fact == "unassessed"
    assert education_fit_reading(snippet, policy).fact == "unassessed"


def test_a_rule_with_no_policy_records_that_rather_than_a_finding() -> None:
    """``unconfigured`` is the third thing a pass can mean, and the quietest.

    A Profile that configures a rule and one that leaves it empty both pass, and
    both used to write the same word. The difference is the one that matters
    when a Hard Filter silently stops killing anything: an empty rule is not a
    Posting that cleared a bar, it is a bar nobody set. No committed Profile
    leaves either of these empty, so this is the only thing pinning them.
    """
    assert education_fit_reading({"description_html": "<p>PhD required.</p>"}).fact == "unconfigured"
    assert role_target_reading({"title": "Chief Executive Officer"}).fact == "unconfigured"


def test_a_profile_that_names_no_band_is_told_apart_from_a_title_that_places_nowhere() -> None:
    """Two abstentions with opposite remedies, which is why they are two tokens.

    A ladder that accepts no rung places nothing, so it decides nothing and the
    Posting passes (#33). That pass is *not* ``unplaced``: ``unplaced``
    is a title this ladder could not read, and its remedy is a better rung
    vocabulary; ``unbanded`` is a Profile that named no rung to place anything
    in, and its remedy is the Profile. Recorded as one token, a Profile that had
    silently stopped filtering on seniority would hide inside a count of hard
    titles — which is the exact failure this field exists to make countable.

    Reachable from a Profile the loader accepts: one that names functions it does
    not want without naming a ladder. The filter must also survive it — every
    rule owes ``apply_filters`` four values, and a three-value return here is an
    unpacking error on the first Posting rather than a wrong token.
    """
    unbanded = RoleTargetPolicy(
        label="seniority band",
        exclude=Matcher(label="an excluded function", pattern=re.compile(r"\bsales\b", re.I)),
    )
    policy = FilterPolicy(enabled=("role_target",), role_target=unbanded)

    assert unbanded.configured and unbanded.band is None
    assert role_target_reading({"title": "Laboratory Intern"}, policy).fact == "unbanded"
    # And the whole decision still assembles, which the bare tuple did not.
    verdict, rule, _, facts = apply_filters({"key": "source:board:1", "title": "Laboratory Intern"}, policy)
    assert (verdict, rule) == ("pass", None)
    assert facts == {"role_target": "unbanded"}


def test_run_writes_facts_on_a_pass_and_nothing_on_a_kill(tmp_path: Path) -> None:
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(
        postings_dir / "2026-08-18.jsonl",
        [
            {
                "key": "source:board:pass",
                "title": "Laboratory Intern",
                "location": "Boston, MA",
                "description_html": "<p>Join a growing laboratory team.</p>",
            },
            {
                "key": "source:board:kill",
                "title": "Senior Director",
                "location": "San Diego, CA",
                "description_html": "<p>On-site.</p>",
            },
        ],
    )

    run(postings_dir, decisions_dir, profile=role_profile(tmp_path / "profiles" / "matcher"))
    rows = {row["posting_key"]: row for row in read_jsonl(only_day_file(decisions_dir))}

    assert rows["source:board:pass"]["facts"]["role_target"] == "intern"
    assert "facts" not in rows["source:board:kill"]


def test_a_standing_pass_row_without_facts_is_never_replayed_to_acquire_them(tmp_path: Path) -> None:
    """The cost question, pinned: this field must not replay the corpus.

    ``filters_version`` hashes what *decides* a Filter Decision, and a record of
    what was decided is not one of those — the same reasoning that keeps
    ``profile.id`` out of it (``store.NON_DECIDING_BLOCKS``). The version is
    therefore unmoved, and the standing-decision comparison in
    ``_decision_still_stands`` deliberately ignores the facts, so a Posting whose
    row predates the field says exactly what the pipeline says today and is left
    alone. Both halves are needed: a moved version would restale every
    ``llm_score`` row and spend the Owner's subscription, and a facts-aware
    comparison would append 120 rows over this corpus to restate 120 verdicts
    that never moved.
    """
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    profile = role_profile(tmp_path / "profiles" / "matcher")
    write_jsonl(
        postings_dir / "2026-08-18.jsonl",
        [{
            "key": "source:board:pass",
            "title": "Laboratory Intern",
            "location": "Boston, MA",
            "description_html": "<p>Join a growing laboratory team.</p>",
        }],
    )

    first = run(postings_dir, decisions_dir, profile=profile)
    day = only_day_file(decisions_dir)
    written = read_jsonl(day)
    # Rewrite history the one way the store never would, to stand in for a row
    # appended before the field existed. ADR-0001 forbids this in `data/`; the
    # point of doing it in a tmp_path is that the pipeline must treat such a row
    # as ordinary and complete.
    write_jsonl(day, [
        {key: value for key, value in row.items() if key != "facts"} for row in written
    ])

    second = run(postings_dir, decisions_dir, profile=profile)

    assert first["processed"] == 1
    assert second == first | {"processed": 0, "pass": 0, "kill": 0, "duplicate": 0, "skipped": 1}
    assert [row for row in read_jsonl(day) if "facts" in row] == []
