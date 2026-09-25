#!/usr/bin/env python3
"""Refuse a pull request that changes filter-deciding code without moving ``filters_version``.

Why this exists, in one paragraph. ``filters_version()`` in
``src/venator/match/store.py`` is stamped on every Filter Decision.
``match.run`` re-decides a Posting whose stored decision carries a different
version. That is the entire replay mechanism: when filter code
changes behaviour and the version does not move, the corpus keeps standing on
verdicts the code would no longer produce, silently and permanently. The signal
is a hex digest nobody reads, so the failure looks exactly like success.

It has nearly landed twice. Two pull requests were each written against a base
at revision 7 and each bumped ``FILTERS_REVISION`` to 8. The first merged; the
second merged too, and git resolved the identical line with no conflict at all.
Left that way the second one's filter change would have re-decided nothing. A
person caught it. Nothing mechanical would have.

WHAT THIS CHECKS
----------------

Two questions decide it, and a third decides how strictly.

1. Did behaviour-relevant content change in a file that can decide a Posting?
   "Behaviour-relevant" is read off the parsed syntax tree with docstrings
   removed, never off bytes: a comment expansion, a docstring rewrite, a
   reformatting or a blank line is not a behaviour change and must not fail this
   check. One of the two pull requests that prompted this guard was a comment
   expansion and nothing else. No — pass, and nothing else runs.

2. Did ``filters_version()`` move, for every Profile under ``profiles/``?
   Yes — pass.

3. Otherwise it depends which kind of file changed, and the two kinds are not
   the same problem.

RULES AND PLUMBING
------------------

``RULE_FILES`` are the modules that *are* the ruleset: the Hard Filters, Dedup,
the policy objects a filter reads, and the loader that builds them from a
Profile. A change to one of these widens or narrows
what the pipeline decides, and **the stored corpus cannot tell you that it
did.** A new Posting may be decided differently even if the current corpus has
no changed verdicts. So for a rule module the
answer is the strict one: the version has to move.

``PLUMBING_FILES`` are the modules that carry the ruleset rather than state it:
the store the decisions are read back from and the CLI pass that orders the
filters. Their effect on a verdict is mechanical, which means the corpus *can* answer for
them — and historically most changes to them have been store claims, path
resolution and warnings that decide nothing. So for a plumbing module the check
does the work rather than demanding a bump on suspicion: it runs the whole
corpus through ``venator.match.run`` at both revisions, into a scratch store it
throws away, and compares the verdicts Posting by Posting. Nothing moved — pass,
with a notice. Something moved — fail, naming how many and which.

Question 2 is asked of ``filters_version()`` itself, computed by running each
side's own code against each side's own Profiles — never of the
``FILTERS_REVISION`` constant. The constant is one input among several. The
Profile's ``constraints.yaml`` and ``targeting.yaml`` are hashed too, less the blocks
``store.NON_DECIDING_BLOCKS`` excludes, so a pull request that edits a Hard
Filter's wording legitimately moves the version without touching the constant,
and reading the constant would fail it for nothing. Equally, a change to
``NON_DECIDING_BLOCKS`` moves the version without touching the constant. Asking
the function is the only way to be right about both.

Every Profile has to move, not merely one, because each Profile's corpus replays
on its own version. A change that bumps the constant moves all of them at once,
which is the ordinary case. A change that only edits one Profile moves
that Profile's and leaves another Profile's where it was — fine on its own, and not fine
alongside a rule change that re-decides marketing's Postings too.

THERE IS NO WAY TO SILENCE IT FROM A BRANCH
-------------------------------------------

Deliberately, and this is the point of the guard rather than a detail of it. The
workflow runs this script **as the base branch carries it**, so a pull request
cannot relax the check that judges it; an improvement to the guard takes effect
for everyone else once the Owner has merged it, and never for its own branch.
Deleting the workflow job is not a way out either — a required check that never
reports blocks the merge rather than passing it.

Which leaves exactly one escape, and it is a person: the Owner merges every pull
request by hand and can merge past a required check. If this check is wrong
about a change, say so on the pull request and let them decide. Do not bump the
revision to make a red check green — that spends the Owner's Claude subscription
re-judging a corpus for nothing — and do not edit the workflow, which is the
repository's own standing rule (CLAUDE.md).

WHAT IT DOES NOT CHECK
----------------------

**Whether a bump is deserved.** Only that one happened. A person still decides
whether the change is worth a replay; this refuses the case where nobody decided
anything at all.

**Anything outside ``RULE_FILES`` and ``PLUMBING_FILES``.** Both lists are in
this file with the reasoning for each entry and each deliberate omission.

``tests/`` is not watched at all: a test-only change decides no Posting.

WHICH TWO REVISIONS
-------------------

Not the merge base, and this is the whole reason the near-miss was a near-miss.
The second pull request branched from revision 7 and bumped to 8. By the time it
was ready, ``main`` already carried 8, because the first one had landed. Its
merge base still carried 7, so *against the merge base its version had moved* —
and the check would have passed while the landed result re-decided nothing.

What decides a replay is what ``main`` carries **before** the merge against what
it carries **after**, so those are the two revisions compared:

  before — the base branch tip the merge was computed against
  after  — the merge result itself

On a pull request event GitHub hands both over: ``GITHUB_SHA`` is the merge of
the branch into the base tip, its first parent is that base tip, and its second
parent is the branch. Reading the pair off one merge commit also keeps them
consistent with each other when ``main`` moves mid-run.

That framing has a second benefit worth naming: the diff between a base tip and
a merge of the branch into it contains the branch's own changes and nobody
else's, so a filter change that landed on ``main`` while this branch was open is
not mistaken for this branch's work.

It is still only as fresh as the merge GitHub computed. If ``main`` takes
another filter change after that, this check answers about the older base tip
until the run is redone — "Require branches to be up to date before merging"
is what forces the redo, and it is the branch protection setting this check
wants alongside it.

Usage:  filters_version_guard.py BEFORE_REV AFTER_REV
Exit:   0 — nothing to do, or the version moved.  1 — the version stood still.
        2 — the check could not be carried out (which is also a failure).
"""

from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

#: The modules that *are* the ruleset. A behaviour change in one of these has
#: to move ``filters_version``, and the stored corpus is not accepted as
#: evidence that it decided nothing — see the module docstring for why, and for
#: the measurement behind it.
#:
#: * ``match/filters.py``  — the Hard Filters themselves.
#: * ``match/dedup.py``    — a Hard Filter under another name; it records at
#:                           stage ``hard_filter`` with ``rule: "duplicate"``.
#: * ``profile/schema.py`` — the policy objects a filter reads.
#: * ``profile/loader.py`` — turns a Profile's YAML into those policy objects,
#:                           defaults included, so what it does not build is a
#:                           rule that never runs.
#:
#: **The false positive this tier accepts, named.** These modules also hold
#: content that is recording rather than deciding — the ``fact`` token a rule
#: reports on a pass, which this repository states outright decides nothing; the
#: ``SOURCE_NAMES`` table a duplicate reason is worded from; a loader warning.
#: A change to any of those is a behaviour change to the syntax tree and this
#: tier fails it. Over the last eighteen merges that is three pull requests, all
#: three of that same shape. There is no reliable way to tell a reason string
#: from a rule constant, and the conservative direction is the one taken: fail,
#: and let the Owner merge past it. Bumping the revision to quiet it is the
#: wrong fix — it replays the whole corpus.
RULE_FILES: tuple[str, ...] = (
    "src/venator/match/filters.py",
    "src/venator/match/dedup.py",
    "src/venator/profile/schema.py",
    "src/venator/profile/loader.py",
)

#: The modules that carry the ruleset rather than state it. A behaviour change
#: in one of these has to move ``filters_version`` **or** survive a replay of
#: the stored corpus showing that no hard-filter verdict moves.
#:
#: * ``match/store.py``    — ``filters_version`` itself, ``NON_DECIDING_BLOCKS``,
#:                           and the effective-state read the replay turns on.
#: * ``match/run.py``      — orders the passes (Dedup over the fit filters'
#:                           survivors, which is load-bearing) and builds the
#:                           Filter Decision row.
#:
#: They are here rather than in ``RULE_FILES`` because that is where the
#: measurement puts them: over the last eighteen merges every change to one of
#: these was a store claim, a path resolution or a warning, and not one moved a
#: verdict. Treating them as rules would have failed four pull requests for
#: nothing. The corpus adjudicates instead, and it can, because what these
#: modules change about a verdict is mechanical rather than a widened rule.
PLUMBING_FILES: tuple[str, ...] = (
    "src/venator/match/store.py",
    "src/venator/match/run.py",
)

#: Everything watched, in the order the report prints it.
WATCHED: tuple[str, ...] = RULE_FILES + PLUMBING_FILES

#: Deliberately absent, and worth stating so the omissions read as decisions.
#:
#: ``profiles/*/constraints.yaml`` and ``profiles/*/targeting.yaml`` are
#: hashed by ``filters_version`` directly, so editing one moves the version by
#: construction and no check could fire on it.

#: How to run a Python that can import ``venator.match.store`` — which needs
#: PyYAML and nothing else. ``--no-project`` keeps uv from installing the
#: checkout, so ``PYTHONPATH`` alone decides which copy of the source is
#: imported; an editable install would put a meta-path finder ahead of it and
#: quietly import the wrong side. Overridable for a machine without uv.
VERSION_PYTHON = shlex.split(
    os.environ.get(
        "VENATOR_GUARD_PYTHON",
        "uv run --quiet --no-project --with pyyaml --python 3.13 python",
    )
)

#: Printed by the subprocess as one JSON object: Profile name to version.
PROBE = """
import json, sys
from pathlib import Path
from venator.match.store import filters_version

versions = {}
for profile_dir in sorted(Path("profiles").iterdir()):
    if (profile_dir / "targeting.yaml").exists():
        versions[profile_dir.name] = filters_version(
            profile_dir / "constraints.yaml", profile_dir / "targeting.yaml"
        )
sys.stdout.write(json.dumps(versions))
"""


class GuardError(RuntimeError):
    """The check could not be carried out, which is not the same as passing."""


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GuardError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def blob(rev: str, path: str) -> str | None:
    """The file's content at ``rev``, or None when it is not there."""
    present = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}:{path}"], capture_output=True, check=False
    )
    if present.returncode != 0:
        return None
    return git("show", f"{rev}:{path}")


def strip_docstrings(tree: ast.AST) -> None:
    """Remove every docstring in place, so rewriting prose is not a change."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            del body[0]


def behaviour(source: str) -> str:
    """A normal form that ignores comments, docstrings, spacing and line numbers.

    Comments never reach the syntax tree at all; docstrings are removed above;
    ``ast.dump`` without attributes carries no positions. What is left is what
    the module *does*, including every non-docstring literal — so a changed
    reason string, a changed threshold and a changed regex all count, which is
    the direction to be conservative in.
    """
    tree = ast.parse(source)
    strip_docstrings(tree)
    return ast.dump(tree)


def changed_behaviour(before_rev: str, after_rev: str) -> list[str]:
    """The watched files whose behaviour differs between the two revisions."""
    changed: list[str] = []
    for path in WATCHED:
        before, after = blob(before_rev, path), blob(after_rev, path)
        if before is None and after is None:
            continue
        if before is None or after is None:
            changed.append(f"{path} (added or removed)")
            continue
        if before == after:
            continue
        if not path.endswith(".py"):
            changed.append(path)
            continue
        try:
            if behaviour(before) != behaviour(after):
                changed.append(path)
        except SyntaxError:
            # Unparseable on one side. Say it changed rather than guess.
            changed.append(f"{path} (could not be parsed)")
    return changed


def checkout(rev: str, repo: Path, workspace: Path, label: str) -> Path:
    """A throwaway worktree holding ``rev``, code and Profiles and corpus alike.

    Named for the side rather than for the revision: two revisions given as
    "<sha>" and "<sha>^1" share their first twelve characters, and naming the
    directory after those collided.
    """
    tree = workspace / label
    git("worktree", "add", "--detach", "--quiet", str(tree), rev, cwd=repo)
    return tree


def run_in(tree: Path, arguments: list[str], *, what: str) -> str:
    """Run a command against ``tree``'s own source, and return its stdout.

    ``PYTHONPATH`` alone decides which copy of ``venator`` is imported, which is
    why nothing here installs the project: an editable install puts a meta-path
    finder ahead of ``PYTHONPATH`` and would import whichever checkout happened
    to be installed rather than the revision under test.
    """
    environment = dict(os.environ, PYTHONPATH=str(tree / "src"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [*VERSION_PYTHON, *arguments],
        cwd=tree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GuardError(f"{what} failed: {(result.stderr.strip() or result.stdout.strip())[-1500:]}")
    return result.stdout


def versions_in(tree: Path, rev: str) -> dict[str, str]:
    """``filters_version`` per Profile, as ``rev`` itself would compute it.

    Both the code and the Profiles come from ``rev``: the answer wanted is the
    version that revision *carries*, and running one side's hashing code over
    the other side's Profiles would answer a question nobody asked.
    """
    printed = run_in(tree, ["-c", PROBE], what=f"computing filters_version at {rev}")
    try:
        parsed = json.loads(printed)
    except json.JSONDecodeError as error:
        raise GuardError(f"filters_version probe at {rev} printed no JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise GuardError(f"filters_version probe at {rev} printed {type(parsed).__name__}, not an object")
    return parsed


def verdicts_in(tree: Path, rev: str, profile: str, postings_dir: Path, scratch: Path) -> dict[str, str]:
    """Hard-filter every stored Posting at ``rev``, and return verdict by Posting.

    ``venator.match.run`` is invoked as a command rather than imported, and that
    is deliberate. This script is run as the *base branch* carries it, against
    a head that may have refactored anything: the CLI is the interface
    ``docs/RUNNING.md`` documents and the one that changes least, so calling it
    is what keeps the check runnable across the pair of revisions it has to
    straddle.

    The corpus is the same file for both sides — the caller passes one
    ``postings_dir`` — so the only thing varying between the two runs is the
    code and the Profile. The scratch store is empty, so nothing is "already
    decided" and every Posting is decided afresh; it is thrown away with the
    workspace and no committed file is touched (``data/`` stays append-only,
    ADR-0001).

    ``reason`` and ``facts`` are deliberately not compared. Both are recording:
    ``facts`` names what a rule read on a pass and this repository states
    outright that it decides nothing, and a reworded reason changes what a row
    says, not what it concluded. Comparing them would fail a pull request that
    added "Workday" to a Source name table, which is a real change this
    repository has already made without a bump.
    """
    store = scratch / f"{tree.name}-{profile}"
    run_in(
        tree,
        [
            "-m",
            "venator.match.run",
            "--profile",
            profile,
            "--postings-dir",
            str(postings_dir),
            "--decisions-dir",
            str(store),
        ],
        what=f"replaying the corpus for Profile {profile} at {rev}",
    )
    verdicts: dict[str, str] = {}
    for line in sorted(store.glob("*.jsonl")):
        for row in line.read_text(encoding="utf-8").splitlines():
            if not row.strip():
                continue
            decision = json.loads(row)
            key = decision.get("posting_key")
            if isinstance(key, str):
                verdicts[key] = f"{decision.get('verdict')}/{decision.get('rule')}"
    if not verdicts:
        raise GuardError(
            f"replaying the corpus for Profile {profile} at {rev} decided no Posting at all — "
            "the check cannot tell whether a verdict moved"
        )
    return verdicts


def annotate(message: str) -> None:
    """A GitHub Actions error annotation when running there, plain text otherwise."""
    print(f"::error::{message}" if os.environ.get("GITHUB_ACTIONS") else f"error: {message}")


RULE_REMEDY = """
Fix: bump FILTERS_REVISION in src/venator/match/store.py.

  Read what the base branch actually carries before choosing the number. Two
  pull requests written against the same base each bump it to the same value,
  and git merges that identical line without a conflict — which is exactly how
  a filter change ends up re-deciding nothing at all. Check first:

      git fetch origin && git show origin/main:src/venator/match/store.py | grep FILTERS_REVISION

  What the bump costs, so it is chosen with the price in view. The Hard Filter
  replay is free and deterministic: `match.run` appends a fresh Filter Decision
  for every Posting, `data/` stays append-only, and nothing is rewritten.

  If this change really decides nothing — a reworded reason, a recording-only
  fact, a warning — then do not bump, because that price is the wrong one to
  pay for it. Say so on the pull request instead and let the Owner merge past
  this check. Do not edit the workflow: this script runs as the base branch
  carries it, so a change to it here would not alter this result anyway.
"""

PLUMBING_REMEDY = """
Fix: bump FILTERS_REVISION in src/venator/match/store.py.

  This is not a suspicion. The Postings listed above are decided differently by
  this change, measured by running the stored corpus through `match.run` at both
  revisions. With filters_version standing still, `match.run` will not ask any
  of them again and they keep verdicts this code no longer produces.

  Read what the base branch actually carries before choosing the number — a
  concurrent pull request may already have taken the next one, and git merges
  the identical line without a conflict:

      git fetch origin && git show origin/main:src/venator/match/store.py | grep FILTERS_REVISION

  The Hard Filter replay that follows is free and deterministic and appends
  rather than rewrites.
"""

RESIDUAL = """
  What was measured: every Posting under data/postings/ decided at both
  revisions, and no hard-filter verdict moved.
"""


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {Path(argv[0]).name} BEFORE_REV AFTER_REV", file=sys.stderr)
        return 2

    before_rev, after_rev = argv[1], argv[2]
    repo = Path(git("rev-parse", "--show-toplevel").strip())

    print(f"What main carries now:       {before_rev}")
    print(f"What it would carry merged:  {after_rev}\n")

    changed = changed_behaviour(before_rev, after_rev)
    if not changed:
        print(
            f"No watched file changed behaviour. ({len(WATCHED)} file(s) watched; "
            "comments, docstrings, formatting and blank lines are not behaviour.)"
        )
        return 0

    rules = [path for path in changed if path.split(" ")[0] in RULE_FILES]
    print("Watched file(s) whose behaviour changed:")
    for path in changed:
        kind = "rule" if path in rules else "plumbing"
        print(f"  [{kind:<8}] {path}")

    try:
        with tempfile.TemporaryDirectory(prefix="filters-version-guard-") as workspace:
            space = Path(workspace)
            scratch = space / "scratch"
            scratch.mkdir()
            trees = {
                before_rev: checkout(before_rev, repo, space, "before"),
                after_rev: checkout(after_rev, repo, space, "after"),
            }
            before = versions_in(trees[before_rev], before_rev)
            after = versions_in(trees[after_rev], after_rev)

            shared = sorted(set(before) & set(after))
            if not shared:
                annotate(
                    "no Profile is present under profiles/ on both sides, so filters_version "
                    "could not be compared for any of them"
                )
                return 2

            print("\nfilters_version per Profile:")
            stalled = []
            for name in shared:
                moved = before[name] != after[name]
                print(f"  {name:<12} {before[name]} -> {after[name]}  {'moved' if moved else 'UNCHANGED'}")
                if not moved:
                    stalled.append(name)
            for name in sorted(set(after) - set(before)):
                print(f"  {name:<12} {'(absent)':>12} -> {after[name]}  new Profile, nothing to replay")

            if not stalled:
                print("\nEvery Profile's filters_version moved. The corpus will re-decide.")
                return 0

            if rules:
                annotate(
                    "the ruleset changed ("
                    + ", ".join(rules)
                    + ") but filters_version did not move for "
                    + ", ".join(f"Profile {name} ({before[name]})" for name in stalled)
                    + " — those Postings would keep verdicts the current rules no longer produce"
                )
                print(RULE_REMEDY)
                return 1

            # Plumbing only: the corpus can answer, so let it.
            postings_dir = trees[after_rev] / "data" / "postings"
            if not postings_dir.exists():
                annotate("there is no corpus under data/postings/ to replay")
                return 2

            print(
                f"\nfilters_version stood still for {', '.join(stalled)}, and only plumbing "
                "changed. Replaying the stored corpus at both revisions."
            )
            moved_verdicts: dict[str, list[str]] = {}
            for name in stalled:
                was = verdicts_in(trees[before_rev], before_rev, name, postings_dir, scratch)
                now = verdicts_in(trees[after_rev], after_rev, name, postings_dir, scratch)
                differing = sorted(key for key in set(was) & set(now) if was[key] != now[key])
                only_now = sorted(set(now) - set(was))
                print(
                    f"  {name:<12} {len(now)} Posting(s) decided, {len(differing)} verdict(s) changed"
                    + (f", {len(only_now)} decided only by the new code" if only_now else "")
                )
                for key in differing[:5]:
                    print(f"      {key}: {was[key]} -> {now[key]}")
                if len(differing) > 5:
                    print(f"      ... and {len(differing) - 5} more")
                if differing or only_now:
                    moved_verdicts[name] = differing + only_now
    except GuardError as error:
        annotate(str(error))
        print(
            "\nThis check cannot pass without measuring both sides, so it fails rather than "
            "waving the change through. Fix the cause, or bump FILTERS_REVISION, or say on the "
            "pull request why it cannot be measured and let the Owner decide."
        )
        return 2
    finally:
        # After the temporary worktrees are gone, not before: prune only removes
        # an administrative entry whose directory has already been deleted.
        subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True, check=False)

    if not moved_verdicts:
        print("\nNo verdict moved: filters_version standing still costs the stored corpus nothing.")
        print(RESIDUAL)
        return 0

    annotate(
        "filters_version did not move, and this change re-decides "
        + "; ".join(f"{len(keys)} of Profile {name}'s Postings" for name, keys in moved_verdicts.items())
        + " — without a bump those Postings are never asked again"
    )
    print(PLUMBING_REMEDY)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except GuardError as failure:
        annotate(str(failure))
        sys.exit(2)
