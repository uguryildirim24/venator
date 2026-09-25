"""The Hard Filter ceiling: that it is armed on every platform, and that it fires.

The failure this file exists to stop is not a wrong verdict. It is a run that
never ends and never says why — an Owner typo in ``patterns`` or
``restriction_patterns``, one of the classic catastrophically backtracking
shapes, and ``venator.schedule.loop`` spinning on it forever with nothing on
stderr. The ceiling used to be armed with ``signal.setitimer``, which does not
exist on Windows, so on the platform the shipped product is about to reach the
guard yielded and did nothing. The test that asserts it works is what hung a CI
job for 29 minutes on a suite that takes half a minute.

So every test here that can be run on both arms is run on both arms, and the
worker arm — the one Windows uses — is exercised on every platform rather than
only where it is the default.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from venator.match.budget import (
    BOOTSTRAP,
    BudgetError,
    _transportable,
    alarm_can_be_armed,
    hard_filter_budget,
)
from venator.match.filters import apply_filters
from venator.profile import Profile, load_profile

#: The classic typo, and an input long enough that matching it takes seconds on
#: any machine. It is not an infinite loop — it is 2**n backtracks, and a real
#: Owner pattern over a real Posting description reaches numbers with no end in
#: sight. Seconds are enough to prove a ceiling of half a second fires.
#:
#: The subject was 24 characters, and that was too close to the bone: 1.63s on
#: the machine this was written on but 0.92s on a GitHub runner, under the 1.0s
#: the watchdog case below needs, so CI went red on somebody else's change that
#: had nothing to do with any of this. The bar is not the thing to move — it is
#: what makes that case mean anything — so the work is. Each extra character
#: doubles the backtracking, so 26 is four times 24: 6.61s here and about 3.7s
#: on the runner that failed. A runner would now have to be nearly four times
#: faster than the fastest one yet seen before that margin is gone again, and
#: the two ways of being wrong here do not cost the same: a thin margin costs
#: another red CI on somebody else's change, a fat one costs five seconds of
#: suite time. Only the watchdog case pays for it either way: the two cases
#: under it are stopped by the ceiling at 0.5s however long the subject would
#: have taken.
RUNAWAY_PATTERN = "(a+)+$"
RUNAWAY_SUBJECT = "a" * 26 + "!"

#: A Profile with every Hard Filter configured, for the arm-against-arm
#: comparison below. Written out here rather than borrowed from ``profiles/``
#: so that what the two arms are compared on cannot drift when a Profile does.
RICH_TARGETING = """\
filters:
  enabled: [work_authorization, education_fit, role_target]
  work_authorization:
    pass_reason: no explicit work-authorization restriction
    restriction_patterns: ['citizens? only', 'active security clearance']
  education_fit:
    holds: bachelor
    experience_years_kill: 8
    experience_years_implausible: 15
  role_target:
    label: seniority band
    levels:
      - name: intern
        title_terms: [intern, internship]
      - name: entry
        title_terms: [entry level, junior]
      - name: mid
        title_terms: []
      - name: senior
        title_terms: [senior, staff, principal]
      - name: executive
        title_terms: [director, vice president]
    accept: [entry, mid]
    exclude:
      label: a function outside this search
      terms: [sales, recruiter]
"""


def profile_at(directory: Path, targeting: str) -> Profile:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "targeting.yaml").write_text(targeting, encoding="utf-8")
    return load_profile(directory)


def runaway_profile(directory: Path) -> Profile:
    return profile_at(
        directory,
        "filters:\n"
        "  enabled: [work_authorization]\n"
        "  work_authorization:\n"
        f"    restriction_patterns: ['{RUNAWAY_PATTERN}']\n",
    )


def test_a_watchdog_thread_cannot_stop_a_runaway_match() -> None:
    """Why the second arm is a process. Measured here rather than believed.

    A watchdog thread is the obvious cheap answer and it does nothing at all:
    CPython's ``re`` holds the GIL for the whole of a match, so a thread told to
    wake up during one cannot even be scheduled — never mind raise into it. This
    pins the measurement so the next person to reach for ``threading.Timer`` or
    ``_thread.interrupt_main`` here reads why not before spending the day on it.

    The assertion is deliberately one-sided: the thread was told to wake at
    0.05s and the match takes seconds, so a thread that ran on time is a real
    change in how CPython schedules and this test should be read again, not
    loosened.

    ``matched_for > 1.0`` under it is a precondition and not the finding: the
    bar the watchdog has to clear is half the match, and a match that finished
    in a moment would let a punctually-scheduled thread clear it and the case
    would pass while proving nothing. So a machine fast enough to trip that
    precondition wants a longer ``RUNAWAY_SUBJECT``, never a lower bar — the
    bar is the separation between 0.05s and half a match, and lowering it
    spends the only thing this case has.
    """
    woke: list[float] = []

    def watchdog() -> None:
        time.sleep(0.05)
        woke.append(time.monotonic())

    thread = threading.Thread(target=watchdog, daemon=True)
    started = time.monotonic()
    thread.start()
    re.search(RUNAWAY_PATTERN, RUNAWAY_SUBJECT)
    matched_for = time.monotonic() - started
    thread.join(timeout=5)

    assert matched_for > 1.0, "the runaway pattern no longer runs long enough to measure"
    assert woke, "the watchdog thread never ran at all"
    assert woke[0] - started > matched_for / 2, (
        "a watchdog thread was scheduled while `re` was matching — if that is really true, "
        "the two-arm ceiling in venator.match.budget can be reconsidered"
    )


@pytest.mark.parametrize("arm", ["alarm", "worker"])
def test_a_runaway_pattern_is_stopped_with_the_posting_and_the_file_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    """The whole point, on both arms. ``worker`` is the arm Windows takes.

    Delete the body of the ceiling — yield and return, which is what the guard
    did on Windows — and this case does not fail. It hangs, which is the failure
    it is written to describe.
    """
    if arm == "alarm" and not alarm_can_be_armed():
        pytest.skip("this platform has no `signal.setitimer`; the worker arm is the only arm")
    monkeypatch.setattr(
        "venator.match.budget.alarm_can_be_armed", lambda: arm == "alarm"
    )
    profile = runaway_profile(tmp_path / "typo")
    posting = {"key": "p:hang", "title": "One", "description_html": f"<p>{RUNAWAY_SUBJECT}</p>"}

    with hard_filter_budget(0.5, profile.targeting_path, profile.filters) as filtered:
        with pytest.raises(TimeoutError) as error:
            filtered(posting)

    message = str(error.value)
    assert "'p:hang'" in message
    assert str(profile.targeting_path) in message
    assert "restriction_patterns" in message


def test_the_worker_arm_decides_exactly_what_the_alarm_arm_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fidelity. A ceiling that moved a verdict would be a worse defect than the hang.

    Every rule the Profile enables, over Postings chosen to reach each of them,
    compared as the whole Filter Decision the store would record — verdict,
    rule, reason, and the facts the rules derived on a pass. ``apply_filters``
    is a pure function of a Posting and a ``FilterPolicy``, which is the whole
    reason it can be answered from another process at all; this is the test that
    says so out loud instead of assuming it.
    """
    profile = profile_at(tmp_path / "sample", RICH_TARGETING)
    postings = [
        {"key": "p:pass", "title": "Research Associate", "location": "Boston, MA",
         "description_html": "<p>A BS and two years of experience.</p>"},
        {"key": "p:authorization", "title": "Research Associate", "location": "Remote, US",
         "description_html": "<p>US citizens only.</p>"},
        {"key": "p:education", "title": "Research Associate", "location": "Cambridge, MA",
         "description_html": "<p>Requires a PhD in biology.</p>"},
        {"key": "p:experience", "title": "Research Associate", "location": "Boston, MA",
         "description_html": "<p>10+ years of experience required.</p>"},
        {"key": "p:ladder", "title": "Vice President of Research", "location": "Boston, MA",
         "description_html": "<p>Lead the function.</p>"},
        {"key": "p:function", "title": "Sales Associate", "location": "Boston, MA",
         "description_html": "<p>Sell things.</p>"},
        {"key": "p:bare", "title": "Research Associate"},
    ]

    monkeypatch.setattr("venator.match.budget.alarm_can_be_armed", lambda: False)
    with hard_filter_budget(10.0, profile.targeting_path, profile.filters) as filtered:
        through_worker = [filtered(posting) for posting in postings]
    in_process = [apply_filters(posting, profile.filters) for posting in postings]

    assert [tuple(one) for one in through_worker] == [
        (verdict, rule, reason, dict(facts)) for verdict, rule, reason, facts in in_process
    ]
    # The comparison above is worth nothing unless these Postings actually reach
    # different rules, so what they reach is pinned rather than trusted. A pass
    # records no rule, which is why the verdicts are pinned beside them.
    assert [decision.rule for decision in through_worker] == [
        None,
        "work_authorization",
        "education_fit",
        "education_fit",
        "role_target",
        "role_target",
        None,
    ]
    # And the facts have to survive the trip, not merely be equal because both
    # sides are empty: a pass carries one token per enabled rule.
    assert through_worker[0].facts == {
        "work_authorization": "clear",
        "education_fit": "met",
        "role_target": "unplaced",
    }


def test_a_ceiling_that_cannot_be_armed_stops_the_run_and_names_the_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one outcome that is not on the table is a ceiling that is quietly absent.

    Neither arm available is a refusal, loud, naming the platform and what is
    unprotected — never a yield that lets the Owner's patterns run with nothing
    watching them.
    """
    monkeypatch.setattr("venator.match.budget.alarm_can_be_armed", lambda: False)

    def no_worker(*arguments: object, **keywords: object) -> object:
        raise OSError("no process may be started here")

    monkeypatch.setattr("venator.match.budget.subprocess.Popen", no_worker)
    profile = runaway_profile(tmp_path / "typo")

    with hard_filter_budget(10.0, profile.targeting_path, profile.filters) as filtered:
        with pytest.raises(BudgetError) as error:
            filtered({"key": "p:1", "title": "One"})

    message = str(error.value)
    assert "10s ceiling" in message
    assert repr(__import__("sys").platform) in message
    assert str(profile.targeting_path) in message
    assert "TimeoutError" not in message
    # An OSError, so every stage's `main` reports it as a sentence rather than
    # letting it out as a traceback.
    assert isinstance(error.value, OSError)


def test_no_worker_is_started_for_a_pass_that_filters_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose corpus is entirely settled pays nothing for the ceiling.

    ``decide`` opens the ceiling once for the whole fit pass, so the worker has
    to be started on the first Posting that needs filtering and not before —
    otherwise every unattended loop over a decided corpus starts a process to do
    nothing.
    """
    started: list[object] = []
    monkeypatch.setattr("venator.match.budget.alarm_can_be_armed", lambda: False)

    real = subprocess.Popen

    def counting(*arguments: object, **keywords: object) -> object:
        started.append(arguments)
        return real(*arguments, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr("venator.match.budget.subprocess.Popen", counting)
    profile = profile_at(tmp_path / "sample", "filters:\n  enabled: [role_target]\n")

    with hard_filter_budget(10.0, profile.targeting_path, profile.filters):
        pass
    assert started == []

    with hard_filter_budget(10.0, profile.targeting_path, profile.filters) as filtered:
        filtered({"key": "p:1", "title": "One", "location": "Boston, MA"})
        filtered({"key": "p:2", "title": "Two", "location": "Boston, MA"})
    # One process for the whole pass, not one per Posting.
    assert len(started) == 1


def test_a_ceiling_of_zero_is_the_one_way_to_run_without_one(tmp_path: Path) -> None:
    """Deliberate rather than accidental: a caller asks for it by naming it."""
    profile = profile_at(tmp_path / "sample", "filters:\n  enabled: [role_target]\n")
    posting = {"key": "p:1", "title": "One", "location": "Boston, MA"}

    with hard_filter_budget(0, profile.targeting_path, profile.filters) as filtered:
        assert tuple(filtered(posting)) == tuple(apply_filters(posting, profile.filters))


def test_a_worker_that_dies_is_reported_and_never_read_as_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Posting with no answer gets no Filter Decision — the run stops instead.

    "No silent drops anywhere" is the rule (CLAUDE.md), and a ceiling that
    enforces itself from another process introduces the one new way to lose a
    Posting: the process ending without answering. It has to be as loud as the
    timeout is.
    """
    monkeypatch.setattr("venator.match.budget.alarm_can_be_armed", lambda: False)
    monkeypatch.setattr("venator.match.budget.BOOTSTRAP", "import sys; sys.exit(3)")
    profile = profile_at(tmp_path / "sample", "filters:\n  enabled: [role_target]\n")

    with hard_filter_budget(10.0, profile.targeting_path, profile.filters) as filtered:
        with pytest.raises(BudgetError) as error:
            filtered({"key": "p:1", "title": "One", "location": "Boston, MA"})

    message = str(error.value)
    assert "'p:1'" in message
    assert "no Filter Decision was taken for it and none was recorded" in message


def test_an_exception_the_worker_raises_reaches_the_caller_as_itself() -> None:
    """The Owner sees the error their Profile caused, not a wrapper around it.

    And where an exception cannot cross a pipe, its type and message do — never
    nothing, and never something invented.
    """

    class Unpicklable(Exception):
        """Defined in a function body, so pickle cannot find it again."""

    assert _transportable(KeyError("description_html")).args == ("description_html",)
    assert type(_transportable(KeyError("x"))) is KeyError
    carried = _transportable(Unpicklable("something local went wrong"))
    assert type(carried) is RuntimeError
    assert str(carried) == "Unpicklable: something local went wrong"


def test_the_worker_bootstrap_is_a_program_and_not_only_a_string() -> None:
    """The child's whole program is a string literal, so nothing type-checks it.

    Written here because it has already happened once during this change: an
    escape in a *comment* inside `BOOTSTRAP` split the line and the child died
    with an `IndentationError` before it imported anything. Compiling it is a
    millisecond and turns that into a normal test failure instead of a worker
    that mysteriously never answers.
    """
    compile(BOOTSTRAP, "<venator.match.budget BOOTSTRAP>", "exec")
    # And it must not import this project before it has been told where to find
    # it, or the worker is whichever `venator` happened to be installed.
    lines = [line.strip() for line in BOOTSTRAP.splitlines() if line.strip()]
    path_set = next(index for index, line in enumerate(lines) if line.startswith("sys.path"))
    imported = next(index for index, line in enumerate(lines) if line.startswith("from venator"))
    assert path_set < imported
