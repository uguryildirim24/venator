"""Run Hard Filters over Postings not decided under the current Profile version.

Usage:
    python -m venator.match.run [--profile NAME]
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Sequence, TypeAlias

from venator.match.budget import hard_filter_budget
from venator.discover.store import posting_revision
from venator.match.dedup import DUPLICATE_RULE, duplicate_decisions
from venator.match.store import (
    append_decisions,
    claim_decisions_dir,
    filters_version,
    hard_decision_keys,
    load_latest_decisions,
    load_postings,
)
from venator.paths import STORE_HELP, StoreRootError, resolve_store_paths
from venator.profile import (
    Profile,
    add_profile_argument,
    announce_profile,
    profile_for_constraints,
)
from venator.profile.schema import FilterPolicy
from venator.qualify.jev_effective import (
    apply_reserved_facts,
    current_filter_version,
    hard_decision_is_fresh,
    jev_promotion_state,
    label_fallback_reason,
    resolve_postings,
)
from venator.qualify.jev_contract import JevContext
from venator.qualify.jev_release import JEV_RELEASE
from venator.qualify.store import as_of_month as parse_as_of_month, promotion_events, verify_store

FILTER_BUDGET_SECONDS = 10.0
"""Wall-clock ceiling for one Posting. Whole corpora run in well under a second.

``venator.match.budget`` is what enforces it, and why it takes a module of its
own: ``patterns`` and ``restriction_patterns`` are Owner-written regular
expressions, one typo (``(a+)+$``) turns a scheduled loop into a process that
never returns, and the mechanism that stops that is not the same mechanism on
every platform. Zero, or less, is the one way to run without a ceiling.
"""


#: What one Posting's fit decision carries out of ``decide``: the verdict, the
#: rule that fired, the reason, and the facts the rules derived on a pass.
#: ``facts`` is ``None`` where this run derived none — a Posting whose standing
#: decision was taken on trust and predates the field — which is distinct from
#: ``{}``, a Profile with no Hard Filter passing on nothing at all.
FitDecision: TypeAlias = tuple[str, str | None, str, Mapping[str, str] | None]


def decide(
    postings: Sequence[dict],
    profile: Profile,
    *,
    settled: Mapping[str, FitDecision] = MappingProxyType({}),
    filter_policy: FilterPolicy | None = None,
    jev_by_key: Mapping[str, JevContext | None] = MappingProxyType({}),
) -> Iterator[tuple[str, str, str | None, str, Mapping[str, str] | None]]:
    """Every Filter Decision this Profile makes over these Postings, in order.

    The fit filters run first, over every Posting, and Dedup runs second over
    the survivors alone. A Posting that fails a fit filter is recorded as that
    fit kill and is never considered for Dedup — not as an optimization, but
    because a Posting the Owner could not apply to is not a legitimate survivor
    of a duplicate group.

    Restricting the candidate set to fit-passers is strictly the conservative
    direction: Dedup can no longer remove the last reachable member of a group.
    It costs nothing that mattered: it happens before any Posting reaches the
    dashboard.

    ``settled`` is how a Posting already decided under this ``filters_version``
    avoids being filtered again: it maps that Posting's key to the verdict, rule,
    reason and facts its standing decision records, and the fit pass takes them
    on trust instead of re-running the Owner's regular expressions. That is not
    only a saving — running the fit filters over the whole corpus every time
    also re-ran every Owner-written pattern every time, so a single
    catastrophically backtracking pattern tripped the Hard Filter ceiling on
    every run, aborted ``main`` before it appended anything, and left *new*
    Postings permanently undecided. A Posting whose standing decision is a ``duplicate``
    kill is never settled: duplicate-ness is a fact about the corpus and the
    corpus grows, so that Posting is filtered again and may return to a pass.

    A settled Posting is still yielded and still grouped. It has to be: the
    reason a new Posting can be recognized as a duplicate of one decided last
    week is that last week's Posting is in the candidate set, so ``settled`` is
    a mapping of decisions rather than a set of keys to drop. What it skips is
    the regex work, not the Posting. The caller compares what comes back
    against the standing decision and appends only what moved, so a fully
    decided corpus yields one decision per Posting, appends none, and matches nothing.

    Each Posting gets exactly one decision: a fit kill, a ``duplicate`` kill, or
    a pass. Yielded rather than returned so the caller decides which of these
    Postings are new; both passes still have to see all of them.

    A settled Posting carries whatever facts its standing decision recorded, and
    ``None`` where it recorded none. Re-deriving them would mean re-running the
    Owner's regular expressions over a Posting whose decision this run already
    trusts — the exact work ``settled`` exists to skip — to produce a field that
    decides nothing.
    """
    fit: list[tuple[str, str, str | None, str, Mapping[str, str] | None]] = []
    # The ceiling is opened once for the whole fit pass rather than once per
    # Posting: on the platforms it is enforced by a worker process, one pass is
    # one process, and a run whose corpus is entirely settled starts none.
    with hard_filter_budget(
        FILTER_BUDGET_SECONDS, profile.targeting_path, filter_policy or profile.filters
    ) as filtered:
        for posting in postings:
            key = posting["key"]
            standing = settled.get(key)
            if standing is not None:
                fit.append((key, *standing))
                continue
            verdict, rule, reason, facts = filtered(posting, jev_by_key.get(key))
            fit.append((key, verdict, rule, reason, facts))
    survivors = [
        posting for posting, (_, verdict, _, _, _) in zip(postings, fit) if verdict == "pass"
    ]
    duplicates = duplicate_decisions(survivors, profile.sources.names)
    for key, verdict, rule, reason, facts in fit:
        if (duplicate_of := duplicates.get(key)) is not None:
            # A duplicate kill replaces the fit pass, so the facts that pass
            # derived describe a decision this run is not recording.
            yield key, "kill", DUPLICATE_RULE, duplicate_of, None
            continue
        yield key, verdict, rule, reason, facts


def settled_decisions(
    latest: Mapping[tuple[str, str], dict], already_decided: Iterable[str]
) -> dict[str, FitDecision]:
    """The standing fit decisions ``decide`` may take on trust this run.

    One entry per Posting already hard-filtered under the current
    ``filters_version`` whose standing decision is a well-formed *fit* decision.
    A ``duplicate`` kill is deliberately left out — it is a statement about the
    corpus rather than about the Posting, and the Posting behind it has to be
    filtered again to find out whether it still stands.
    """
    trusted: dict[str, FitDecision] = {}
    for key in already_decided:
        standing = latest.get((key, "hard_filter"))
        if standing is None:
            continue
        verdict, rule, reason = standing.get("verdict"), standing.get("rule"), standing.get("reason")
        if rule == DUPLICATE_RULE or not isinstance(verdict, str) or not isinstance(reason, str):
            continue
        if rule is not None and not isinstance(rule, str):
            continue
        # A row written before ``facts`` existed has none, and that is unknown
        # rather than empty — the same rule ``profile_id`` follows
        # (coordination/CONTRACTS.md). Carried forward as it stands, never
        # invented, so nothing this run appends can claim a reading it did not
        # take.
        facts = standing.get("facts")
        trusted[key] = (verdict, rule, reason, facts if isinstance(facts, dict) else None)
    return trusted


def _decision_still_stands(
    previous: dict | None,
    verdict: str,
    rule: str | None,
    reason: str,
    facts: Mapping[str, str] | None = None,
    *,
    jev_mode: bool = False,
) -> bool:
    """Whether the Posting's standing decision says exactly what this run says.

    A ``filters_version`` stamps everything the Profile brings to a decision, so
    one decision per version would be enough if a decision were a fact about one
    Posting. Two of them are not. Whether a Posting is a duplicate is a fact
    about the *corpus*, and the corpus grows: the employer's own record of a job
    first seen as an aggregator snippet arrives a week later and the survivor
    flips to the richer copy; a Posting decided alone stops being alone. And now
    that Dedup ranks only fit-passers, a group's survivor losing its pass — its
    stored Posting re-fetched with a description that trips a fit filter — hands
    the group to a Posting that was standing as a duplicate. Neither movement is
    visible to a version stamp.

    The facts are deliberately **not** in that comparison in deterministic
    mode. They record what the rules read, never what they decided. In jev
    mode the reserved ``jev`` / ``jev_input`` pair is binding freshness
    metadata and is compared; an unavailable input binding is never settled.
    Other fact changes alone keep their observational semantics.
    """
    if previous is None:
        return True
    if (
        previous.get("verdict") != verdict
        or previous.get("rule") != rule
        or previous.get("reason") != reason
    ):
        return False
    if not jev_mode:
        return True
    previous_facts = previous.get("facts") if isinstance(previous.get("facts"), dict) else {}
    current = dict(facts or ())
    if current.get("jev_input") == "unavailable" or previous_facts.get("jev_input") == "unavailable":
        return False
    return previous_facts.get("jev") == current.get("jev") and previous_facts.get("jev_input") == current.get("jev_input")


def run(
    postings_dir: Path | None = None,
    decisions_dir: Path | None = None,
    constraints_path: Path | None = None,
    *,
    profile: Profile | None = None,
    qualifications_dir: Path | None = None,
    as_of_month: str | None = None,
) -> dict[str, int | str]:
    stores = resolve_store_paths(postings_dir=postings_dir, decisions_dir=decisions_dir)
    postings_dir = stores["postings_dir"]
    decisions_dir = stores["decisions_dir"]
    if profile is None:
        profile = profile_for_constraints(constraints_path)
    filter_policy = profile.filters
    jev_mode = filter_policy.qualification_mode == "jev"
    jev_by_key: dict[str, JevContext | None] = {}
    resolutions: dict = {}
    promotion = None
    release = JEV_RELEASE if jev_mode else None
    if jev_mode:
        if as_of_month is None:
            raise ValueError("jev qualification requires --as-of")
        qualifications_dir = resolve_store_paths(qualifications_dir=qualifications_dir)["qualifications_dir"]
        verify_store(qualifications_dir, profile.identifier)
        promotion = jev_promotion_state(promotion_events(qualifications_dir), profile.identifier)
        version = current_filter_version(profile, promotion, release)
    else:
        version = filters_version(
            constraints_path or profile.constraints_path,
            profile.targeting_path,
        )
    claim_decisions_dir(decisions_dir, profile.identifier)
    postings = load_postings(postings_dir)
    latest = load_latest_decisions(decisions_dir)
    revisions = {posting["key"]: posting_revision(posting) for posting in postings}
    if jev_mode:
        resolutions = resolve_postings(
            postings,
            profile,
            as_of_month=as_of_month or "",
            qualifications_dir=qualifications_dir,
            release=release,
        )
        jev_by_key = {key: resolution.context for key, resolution in resolutions.items()}
    already_decided = {
        key for key in hard_decision_keys(decisions_dir, version)
        if latest.get((key, "hard_filter"), {}).get("posting_version") == revisions.get(key)
    }
    if jev_mode:
        trusted: set[str] = set()
        for key in already_decided:
            standing = latest.get((key, "hard_filter"))
            resolution = resolutions[key]
            if standing is None:
                continue
            if standing.get("verdict") == "pass":
                if hard_decision_is_fresh(
                    standing,
                    resolution,
                    version=version,
                    posting_revision=revisions.get(key),
                    jev_mode=True,
                ):
                    trusted.add(key)
            elif reserved_tokens_match(standing, resolution):
                trusted.add(key)
        already_decided = trusted
    settled = settled_decisions(latest, already_decided)
    decisions = []
    passes = 0
    kills = 0
    duplicates = 0

    for key, verdict, rule, reason, facts in decide(
        postings,
        profile,
        settled=settled,
        filter_policy=filter_policy,
        jev_by_key=jev_by_key,
    ):
        if jev_mode:
            resolution = resolutions[key]
            reason = label_fallback_reason(reason, resolution)
            facts = apply_reserved_facts(verdict, facts, resolution)
        standing = latest.get((key, "hard_filter"))
        if key in already_decided and _decision_still_stands(
            standing, verdict, rule, reason, facts, jev_mode=jev_mode
        ):
            continue
        passes += verdict == "pass"
        kills += verdict == "kill"
        duplicates += rule == DUPLICATE_RULE
        recorded_facts = None
        if jev_mode:
            recorded_facts = dict(facts) if facts is not None else None
        elif verdict == "pass" and facts is not None:
            recorded_facts = dict(facts)
        decisions.append(
            {
                "posting_key": key,
                "stage": "hard_filter",
                "verdict": verdict,
                "rule": rule,
                "reason": reason,
                **({"facts": recorded_facts} if recorded_facts is not None else {}),
                "profile_id": profile.identifier,
                "filters_version": version,
                "posting_version": revisions[key],
                "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )

    append_decisions(decisions_dir, decisions)
    return {
        "processed": len(decisions),
        "pass": passes,
        "kill": kills,
        "duplicate": duplicates,
        "skipped": len(postings) - len(decisions),
        "filters_version": version,
    }


def reserved_tokens_match(decision: dict | None, resolution) -> bool:
    if decision is None:
        return False
    facts = decision.get("facts") if isinstance(decision.get("facts"), dict) else {}
    reserved = apply_reserved_facts(str(decision.get("verdict") or "kill"), None, resolution)
    if reserved["jev_input"] == "unavailable":
        return False
    return facts.get("jev") == reserved["jev"] and facts.get("jev_input") == reserved["jev_input"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postings-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--decisions-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--qualifications-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--as-of")
    parser.add_argument("--constraints", type=Path, default=None)
    add_profile_argument(parser)
    args = parser.parse_args()
    try:
        profile = profile_for_constraints(
            args.constraints, requested=args.profile, directory=args.profile_dir
        )
    except ValueError as error:
        parser.error(str(error))
    announce_profile(profile)
    today = args.as_of or date.today().isoformat()
    try:
        month = parse_as_of_month(today)
    except ValueError as error:
        parser.error(str(error))
    try:
        stores = resolve_store_paths(
            postings_dir=args.postings_dir, decisions_dir=args.decisions_dir,
            qualifications_dir=args.qualifications_dir,
        )
    except StoreRootError as error:
        parser.error(str(error))
    try:
        summary = run(
            stores["postings_dir"], stores["decisions_dir"], args.constraints, profile=profile,
            qualifications_dir=stores["qualifications_dir"], as_of_month=month,
        )
    # OSError alongside ValueError: the Profile claim reads and writes
    # ``data/decisions/.profile``, and a stamp that is a directory or holds
    # bytes that are not UTF-8 must report cleanly. The Hard Filter ceiling
    # arrives here too — ``TimeoutError`` and ``venator.match.budget``'s own
    # ``BudgetError`` are both ``OSError``, deliberately, so a Posting that
    # exhausts its ceiling is a sentence rather than a stack.
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(
        f"hard filters [{summary['filters_version']}]: {summary['processed']} decided "
        f"({summary['pass']} pass, {summary['kill']} kill, of which "
        f"{summary['duplicate']} duplicate); {summary['skipped']} already decided"
    )


if __name__ == "__main__":
    main()
