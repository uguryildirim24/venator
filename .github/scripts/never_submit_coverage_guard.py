#!/usr/bin/env python3
"""Refuse a run in which the never-submit browser tests did not actually execute.

Why this exists, in one paragraph. ``tests/browser/`` is the only place the
never-submit guards meet a real browser: ``install_dry_run_guards`` aborting
state-changing HTTP at the network layer, the init script that cancels every
``submit`` event in the capture phase, the refusal to execute a FillPlan that
does not carry a literal ``never_submit: true``, and the skipped file field.
Everywhere else those guards are asserted against a stub. CLAUDE.md calls the
never-submit invariant a designed property "enforced in code and covered by
tests" — and that second half is only true on the runs where the tests run.

Nothing used to check that they did. CI passed ``-v`` so the log named every
test, on the reasoning that a named PASSED line is proof; but no machine reads
the log, and the log is only consulted when something is already known to be
wrong. Which leaves several ordinary edits that turn the coverage off while
every check stays green:

  * a ``@pytest.mark.skip`` or a ``skipif`` on the module — added to get a red
    branch moving and never taken off again;
  * a ``conftest.py`` or an ``importorskip`` that turns a missing browser
    binary from a failure into a skip, which is the shape this exact suite is
    one line away from at all times;
  * ``--ignore=tests/browser`` or a ``-m`` selection in ``addopts``, which
    deselects them for everyone and looks like a speed-up in the diff;
  * moving or renaming the directory in a refactor, so nothing collects there
    any more.

Every one of those leaves a green suite and an unexercised guard. This script
is the machine that reads the result.

WHAT THIS CHECKS
----------------

Exactly two things, of the JUnit XML written by the suite run that gates the
merge:

1. Every test id in ``EXPECTED_BROWSER_TEST_IDS`` — ``classname::name``, with
   the parametrised cases counted one apiece — is in the report exactly once,
   and nothing else under ``tests/browser/`` is.
2. Not one of them was skipped, and not one errored before its body ran.

A test case that *failed* counts as having run: it reached the guard and the
guard said no. That failure is the suite's to report, and it does, because a
failing test makes the pytest step red on its own.

Names rather than a bare count, because a count is satisfied by any eight
things. A rename, a split, a move or a swap that nets back to eight would pass a
counting check while the coverage underneath it changed; matching the ids says
which eight.

And one thing this deliberately does not claim, because it cannot observe it: it
reads a report, not a browser. It can say a test case ran under a given name and
was not skipped. It cannot say what that test case did — eight same-named stubs
that launch nothing would satisfy it. What those tests assert is theirs to
assert, and the pull request that changes them is where a person reads them.

WHY THE EXPECTED SET IS HARDCODED
---------------------------------

Because a self-adjusting expectation checks nothing. Collecting the directory at
runtime and comparing that to the tests that ran would pass a report in which
the whole directory was deselected — nothing collected, nothing missing.
Reading the expectation out of the same report it is judging is circular in the
same way. The list here is a constant a person has to open this file and edit,
and the failure message says so, because the edit is the decision: adding a
browser test or removing one is fine and takes a line; discovering you have to
change this list in order to get a green check is the moment to notice that the
never-submit coverage just changed.

The zero case is called out separately and is never a pass. "No browser tests
were found, so there is nothing to check" is precisely the failure this guard
exists for — a moved directory, a renamed one, an emptied one, a deselection —
and it is the failure that looks most like success.

WHERE IT RUNS
-------------

As a step of the existing ``python — pytest`` job, reading a report the same
step's pytest wrote. Two deliberate choices in that:

**A step, not a job.** The repository's required checks are configured in
branch protection by the Owner, and a fifth check name would be theirs to add
before this could gate anything. ``pnpm test`` was added inside the existing
``ui`` job for exactly this reason. A step inside ``python — pytest`` gates the
merge the day it lands.

**The real run, not a second one.** Re-running ``pytest tests/browser`` here
would prove that the browser tests pass when invoked directly, which is not the
question. The question is whether they ran in the invocation that gates the
merge — the one carrying the repository's own ``addopts``, markers and
conftest. So the guard reads that run's own ``--junitxml`` report. Parsing the
XML rather than the terminal output is not a preference: ``-v`` output is a
human format with no stability promise, and a skip and a pass differ there by
one word in a coloured line.

An absent or unparseable report is a failure, never a pass. There is no reading
of a missing report that says the guards were exercised.

Usage:  never_submit_coverage_guard.py REPORT_XML
Exit:   0 — the browser tests ran, none skipped.
        1 — they did not, or not all of them.
        2 — the check could not be carried out (which is also a failure).
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

#: Every test case ``tests/browser/`` contributes to a full run, as the
#: ``classname::name`` pair pytest writes into the JUnit report. Five test
#: functions, one of them parametrised four ways — the parameter id is part of
#: the name, so each of the four is pinned separately.
#:
#: **Edit this deliberately, and only alongside a real change to that
#: directory.** It is hardcoded rather than derived so that turning the
#: never-submit coverage off cannot be done without touching these lines, and so
#: that touching them is visible in a diff. Never change it to make a red check
#: green: a red check here means what ran on the machine no longer matches what
#: a person last agreed to, and the question to answer is which of the two is
#: wrong.
EXPECTED_BROWSER_TEST_IDS: frozenset[str] = frozenset(
    {
        f"tests.browser.test_browser_driver::{name}"
        for name in (
            "test_recon_inventories_all_contract_field_kinds",
            "test_fill_executes_plan_accounts_for_every_entry_and_never_submits",
            "test_unsafe_submit_selector_is_failed_without_activation",
            "test_non_exact_select_value_is_skipped_without_guessing",
            "test_plan_without_literal_never_submit_is_refused[None]",
            "test_plan_without_literal_never_submit_is_refused[False]",
            "test_plan_without_literal_never_submit_is_refused[1]",
            "test_plan_without_literal_never_submit_is_refused[true]",
        )
    }
    | {
        "tests.browser.test_handoff::" + name
        for name in (
            "test_handoff_keeps_a_visible_browser_open_and_fills_confirmed_fields",
            "test_second_handoff_reports_a_locked_dedicated_profile",
            "test_unsupported_source_opens_manual_fallback_without_uploading",
            "test_production_form_can_send_manual_post_with_current_documents",
            "test_failed_worker_preserves_ready_error_until_caller_reads_it",
            "test_assistance_cannot_escape_the_confirmed_document[redirect]",
            "test_assistance_cannot_escape_the_confirmed_document[apply-link]",
            "test_assistance_cannot_escape_the_confirmed_document[wrong-job]",
            "test_assistance_cannot_escape_the_confirmed_document[untrusted-parent]",
            "test_document_identity_rejects_lookalikes_and_other_jobs[http]",
            "test_document_identity_rejects_lookalikes_and_other_jobs[lookalike]",
            "test_document_identity_rejects_lookalikes_and_other_jobs[unsupported-host]",
            "test_document_identity_rejects_lookalikes_and_other_jobs[other-board]",
            "test_document_identity_rejects_lookalikes_and_other_jobs[other-job]",
        )
    }
    | {
        "tests.browser.test_handoff_headless::test_handoff_headless_option_is_honoured",
    }
)

#: How many test cases that is. Derived from the list above rather than written
#: out again, because two hardcoded numbers that can disagree are worse than
#: one: the set is the thing a person edits, and the count follows it.
EXPECTED_BROWSER_TESTS: int = len(EXPECTED_BROWSER_TEST_IDS)

#: The dotted module prefix pytest writes into a test case's ``classname`` for
#: anything under ``tests/browser/``. The JUnit schema has no path attribute, so
#: this is how a browser test is recognised. The trailing dot matters: a
#: directory renamed to ``tests/browser_driver/`` reports
#: ``tests.browser_driver.…`` and does not match, which is the intended answer —
#: a rename is a change to what this guard was told to look for, and it should
#: have to be told again.
BROWSER_CLASSNAME_PREFIX = "tests.browser."

#: Where the workflow tells pytest to write the report, quoted in the failure
#: text so the fix is legible from the log alone.
REPORT_FLAG = "--junitxml"


class GuardError(RuntimeError):
    """The check could not be carried out, which is not the same as passing."""


def annotate(message: str) -> None:
    """A GitHub Actions error annotation when running there, plain text otherwise."""
    print(f"::error::{message}" if os.environ.get("GITHUB_ACTIONS") else f"error: {message}")


def load_report(path: Path) -> ElementTree.Element:
    """The report's root element, or a GuardError naming why there is none."""
    if not path.exists():
        raise GuardError(
            f"no test report at {path} — the suite either did not run or was not asked "
            f"for one with {REPORT_FLAG}"
        )
    try:
        return ElementTree.parse(path).getroot()
    except ElementTree.ParseError as error:
        raise GuardError(f"the test report at {path} is not parseable XML: {error}") from error
    except OSError as error:
        raise GuardError(f"the test report at {path} could not be read: {error}") from error


def browser_cases(root: ElementTree.Element) -> list[ElementTree.Element]:
    """Every ``<testcase>`` in the report whose module lives under ``tests/browser/``."""
    return [
        case
        for case in root.iter("testcase")
        if (case.get("classname") or "").startswith(BROWSER_CLASSNAME_PREFIX)
    ]


def outcome(case: ElementTree.Element) -> str:
    """One word for what became of a test case.

    ``skipped`` and ``error`` both mean the body did not run: a skip never
    starts it, and an error is a fixture or collection failure that aborts
    before it. ``failure`` means it ran and the assertion did not hold, which is
    coverage doing its job and the suite's own red to report.
    """
    if case.find("skipped") is not None:
        return "skipped"
    if case.find("error") is not None:
        return "error"
    if case.find("failure") is not None:
        return "failure"
    return "passed"


REMEDY = """
What to do about it.

  If you moved, renamed or deleted tests/browser/, or added, removed, renamed
  or reparametrised a test in it: that is a change to the never-submit
  coverage, so update EXPECTED_BROWSER_TEST_IDS in
  .github/scripts/never_submit_coverage_guard.py in the same commit, and say in
  the pull request what the new set covers. The list is hardcoded on purpose —
  editing it is the deliberate act this check exists to force, and
  EXPECTED_BROWSER_TESTS is derived from it so the two cannot drift apart.

  If a test is skipped: take the skip off, or fix what made it necessary. The
  most likely cause is a missing Chromium — the workflow installs the headless
  shell before the suite for exactly this reason, and tests/browser/ carries no
  conftest, no skipif and no importorskip so that a missing binary fails
  loudly instead of quietly skipping. If a skip has appeared there, that
  property has been given up.

  If the tests were deselected: check pytest's addopts in pyproject.toml and
  the workflow's own pytest invocation for an --ignore, a -m, a -k or a
  narrowed path argument that no longer reaches tests/browser/.

  What is at stake, so the size of it is clear. tests/browser/ is the only
  place the never-submit guards are exercised against a real browser: the
  aborted POST/PUT/PATCH/DELETE, the cancelled submit event, the refusal to
  execute a FillPlan without a literal never_submit, the skipped file field.
  With these tests not running, nothing in CI demonstrates that a dry run is
  still a dry run. Do not silence this check to get a branch moving.
"""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} REPORT_XML", file=sys.stderr)
        return 2

    report = Path(argv[1])
    root = load_report(report)
    cases = browser_cases(root)

    print(f"Test report:      {report}")
    print(f"Expecting:        {EXPECTED_BROWSER_TESTS} test case(s) under tests/browser/, by name")
    print(f"Found:            {len(cases)}\n")

    outcomes = [(f"{case.get('classname', '?')}::{case.get('name', '?')}", outcome(case)) for case in cases]
    for name, result in sorted(outcomes):
        print(f"  {result:<8} {name}")

    if not cases:
        annotate(
            "the never-submit guards went unexercised: this run collected no tests at all "
            "under tests/browser/, so nothing in CI demonstrates that a dry run cannot submit"
        )
        print(
            "\nZero browser tests is not 'nothing to check' — it is the failure this guard "
            "exists for. The directory has been moved, renamed, emptied or deselected."
        )
        print(REMEDY)
        return 1

    unexercised = sorted(name for name, result in outcomes if result in ("skipped", "error"))
    if unexercised:
        annotate(
            f"the never-submit guards went unexercised: {len(unexercised)} of {len(cases)} "
            "browser test(s) were skipped or errored before running"
        )
        print("\nThese never ran:")
        for name in unexercised:
            print(f"  {name}")
        print(REMEDY)
        return 1

    ran = [name for name, _ in outcomes]
    missing = sorted(EXPECTED_BROWSER_TEST_IDS.difference(ran))
    unexpected = sorted(set(ran).difference(EXPECTED_BROWSER_TEST_IDS))
    repeated = sorted({name for name in ran if ran.count(name) > 1})

    if missing or unexpected or repeated:
        if missing:
            annotate(
                f"the never-submit guards went unexercised: {len(missing)} of the "
                f"{EXPECTED_BROWSER_TESTS} browser test(s) this check was told to expect did "
                "not run under that name"
            )
        else:
            annotate(
                "the never-submit browser coverage changed: tests/browser/ reported test "
                "case(s) this check was not told to expect"
            )
        if missing:
            print("\nExpected, and not in the report:")
            for name in missing:
                print(f"  {name}")
        if unexpected:
            print("\nIn the report, and not expected:")
            for name in unexpected:
                print(f"  {name}")
        if repeated:
            print("\nIn the report more than once:")
            for name in repeated:
                print(f"  {name}")
        print(
            "\nThe expected set is hardcoded in this script and nobody moved it. Either "
            "tests/browser/ changed and EXPECTED_BROWSER_TEST_IDS was not updated with it, or "
            "something is deselecting part of the directory."
        )
        print(REMEDY)
        return 1

    print(
        f"\nAll {EXPECTED_BROWSER_TESTS} expected test cases under tests/browser/ ran in this "
        "run, and none was skipped. That is what the report shows; what those tests assert is "
        "theirs to assert."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except GuardError as failure:
        annotate(str(failure))
        print(
            "\nThis check cannot pass without reading the suite's own report, so it fails "
            "rather than waving the run through. An absent or unreadable report says nothing "
            "about whether the never-submit guards were exercised."
        )
        sys.exit(2)
