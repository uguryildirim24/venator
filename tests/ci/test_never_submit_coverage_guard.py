"""The CI guard that refuses a run in which the never-submit tests did not run.

``.github/scripts/never_submit_coverage_guard.py`` reads the JUnit report the
gating pytest invocation writes and fails unless every test case under
``tests/browser/`` actually executed. Its whole value is in the failing
direction, so that is what is pinned here: a skipped test, a moved directory, a
deselection, a renamed test, a truncated report and an absent one each have to
be a failure, and none of them may be mistaken for "nothing to check".

The one passing case is pinned too, because a check that cannot go green is a
check somebody deletes.

``EXPECTED_BROWSER_TEST_IDS`` is deliberately hardcoded in the guard, so this
file also asserts that the set still matches what ``tests/browser/`` collects on
this machine. That assertion is the mechanism working as designed: the day the
directory gains, loses or renames a test, this fails here — locally, before CI —
and someone has to look at the list and agree with it.

That reconciliation is the guard's second leg, and it is only worth having if it
is independent of the first. Without that independence one edit moves both legs
at once — the deselection shrinks the report the guard reads, and shrinks the
collection the constant is reconciled against, so the two agree and the pair
proves nothing. There are three roads to that one edit, and each has a test
below holding it shut:

* a deselection in ``pyproject.toml``'s ``addopts`` — the collection passes
  ``-o addopts=``, which empties the ini option for that invocation only;
* a deselection injected through ``PYTEST_ADDOPTS``, which pytest applies on top
  of the ini value and which ``-o`` does not touch — the collection is handed an
  explicit environment with that variable removed, rather than inheriting one;
* a ``pytest_collection_modifyitems`` hook in a ``conftest.py``, which is a
  plugin hook and so obeys neither of the above — pinned instead by asserting
  that the repository carries no ``conftest.py`` in either parent directory
  pytest loads on the way down, nor anywhere under ``tests/browser/``, which is
  the rest of what it loads while collecting there.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from xml.sax.saxutils import quoteattr

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_PATH = REPO_ROOT / ".github" / "scripts" / "never_submit_coverage_guard.py"
BROWSER_TESTS = REPO_ROOT / "tests" / "browser"


def load_guard() -> ModuleType:
    """Import the guard by path — it is a CI script, not part of the package."""
    spec = importlib.util.spec_from_file_location("never_submit_coverage_guard", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = load_guard()

EXPECTED_IDS = sorted(guard.EXPECTED_BROWSER_TEST_IDS)


# --- building a report of the shape pytest writes ---------------------------


def case_xml(test_id: str, inner: str = "") -> str:
    """One ``<testcase>`` for ``classname::name``, as the JUnit report spells it."""
    classname, _, name = test_id.rpartition("::")
    return (
        f"<testcase classname={quoteattr(classname)} name={quoteattr(name)} "
        f'time="0.5">{inner}</testcase>'
    )


def browser_ids(count: int) -> list[str]:
    """``count`` ids under ``tests/browser/``: the expected ones first, then extras."""
    ids = EXPECTED_IDS[:count]
    extras = count - len(ids)
    return ids + [
        f"{guard.BROWSER_CLASSNAME_PREFIX}test_browser_driver::test_extra_{index}"
        for index in range(extras)
    ]


def browser_cases(count: int, *, inner: str = "") -> str:
    """``count`` test cases under ``tests/browser/``, each with ``inner`` in it."""
    return "".join(case_xml(test_id, inner) for test_id in browser_ids(count))


def report(tmp_path: Path, cases: str) -> Path:
    """A minimal JUnit report of the shape pytest writes, holding ``cases``."""
    path = tmp_path / "pytest-report.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests"><testsuite name="pytest">'
        f"{cases}"
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def run(path: Path) -> int:
    return guard.main([str(GUARD_PATH), str(path)])


# --- collecting the directory, without inheriting anyone's deselection ------

#: ``-o addopts=`` empties the configured ``addopts`` for this invocation only.
#: It is the whole reason this reconciliation is worth anything: see the module
#: docstring, and the test at the bottom of this section.
ADDOPTS_NEUTRALISER = ("-o", "addopts=")

#: The other half of the same lever. pytest applies ``PYTEST_ADDOPTS`` on top of
#: the ini ``addopts``, and ``-o addopts=`` does not touch it — ``-o`` overrides
#: ini settings, and this is an environment variable. A subprocess that inherited
#: this process's environment would therefore inherit any deselection injected
#: through it, in the gating run and in this reconciliation alike, which is the
#: one edit moving both legs that the pair exists to rule out. So the collection
#: is handed an explicit environment with the variable removed.
INHERITED_ADDOPTS_VARS = ("PYTEST_ADDOPTS",)

#: A ``--collect-only -q`` line: ``path/to/test_x.py::TestClass::test_y``.
COLLECTED = re.compile(r"^(?P<path>\S+\.py)::(?P<rest>\S.*)$")


def collect_command(target: Path, *, neutralise_addopts: bool = True) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        str(target),
        "--collect-only",
        "-q",
        "--no-header",
        *(ADDOPTS_NEUTRALISER if neutralise_addopts else ()),
    ]


def collect_env(
    *, neutralise_addopts: bool = True, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """This process's environment, minus the variables that inject pytest flags."""
    environment = {**os.environ, **(extra or {})}
    if neutralise_addopts:
        for name in INHERITED_ADDOPTS_VARS:
            environment.pop(name, None)
    return environment


def collect_ids(
    target: Path,
    *,
    cwd: Path,
    neutralise_addopts: bool = True,
    extra_env: Mapping[str, str] | None = None,
) -> list[str]:
    """The ``classname::name`` ids pytest collects under ``target``.

    The conversion mirrors what pytest writes into a JUnit report: the module
    path becomes the dotted classname, any class in between joins it, and the
    last segment — parameter id included — is the name.
    """
    collected = subprocess.run(
        collect_command(target, neutralise_addopts=neutralise_addopts),
        cwd=cwd,
        env=collect_env(neutralise_addopts=neutralise_addopts, extra=extra_env),
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    ids = []
    for line in collected.stdout.splitlines():
        match = COLLECTED.match(line.strip())
        if match is None:
            continue
        module = match["path"].replace("\\", "/").removesuffix(".py").replace("/", ".")
        *enclosing, name = match["rest"].split("::")
        ids.append(f"{'.'.join([module, *enclosing])}::{name}")
    return ids


# --- the guard is where the workflow looks for it ---------------------------


def test_the_guard_script_is_where_the_workflow_looks_for_it() -> None:
    assert GUARD_PATH.exists()
    workflow = (GUARD_PATH.parents[1] / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert ".github/scripts/never_submit_coverage_guard.py" in workflow


def test_the_workflow_asks_pytest_for_the_report_the_guard_reads() -> None:
    """The guard is inert unless the suite is actually told to write a report."""
    workflow = (GUARD_PATH.parents[1] / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--junitxml" in workflow


# --- the expected set is real -----------------------------------------------


def test_the_hardcoded_ids_match_what_the_directory_collects() -> None:
    """The list and the directory have to agree, and a person reconciles them.

    Failing here is the guard doing its job one step earlier than CI: a browser
    test was added, removed or renamed, and ``EXPECTED_BROWSER_TEST_IDS`` has
    not been updated to say so.
    """
    collected = collect_ids(BROWSER_TESTS, cwd=REPO_ROOT)
    assert set(collected) == set(EXPECTED_IDS), (
        f"tests/browser/ collects {sorted(collected)} but the CI guard expects "
        f"{EXPECTED_IDS}. If that change was intended, edit EXPECTED_BROWSER_TEST_IDS "
        f"in {GUARD_PATH.name} deliberately."
    )
    assert len(collected) == guard.EXPECTED_BROWSER_TESTS


def test_the_reconciliation_is_independent_of_a_configured_deselection(tmp_path: Path) -> None:
    """The two legs must not be movable with one edit.

    The guard reads the gating run's report, and the test above reconciles the
    hardcoded set against what the directory collects. If the collection obeyed
    ``addopts`` too, a ``-k`` in ``pyproject.toml`` that deselects most of
    ``tests/browser/`` would shrink both of them together: the report would hold
    the survivors, the constant would be edited down to match, and every check
    would be green with the never-submit guards mostly unexercised.

    ``-o addopts=`` is what stops that, so this pins that it is load-bearing —
    in a throwaway project rather than by editing this repository's own
    ``pyproject.toml``, which no test may do.
    """
    project = tmp_path / "project"
    tests = project / "tests" / "browser"
    tests.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = \"-k 'not two and not three'\"\n",
        encoding="utf-8",
    )
    (tests / "test_stub.py").write_text(
        "def test_one() -> None: ...\n"
        "def test_two() -> None: ...\n"
        "def test_three() -> None: ...\n",
        encoding="utf-8",
    )

    assert "-o" in collect_command(BROWSER_TESTS)
    assert "addopts=" in collect_command(BROWSER_TESTS)

    inheriting = collect_ids(tests, cwd=project, neutralise_addopts=False)
    assert inheriting == ["tests.browser.test_stub::test_one"], inheriting

    neutralised = collect_ids(tests, cwd=project)
    assert neutralised == [
        "tests.browser.test_stub::test_one",
        "tests.browser.test_stub::test_two",
        "tests.browser.test_stub::test_three",
    ], neutralised


def test_the_reconciliation_is_independent_of_an_injected_pytest_addopts(tmp_path: Path) -> None:
    """The same lever, pulled through the environment instead of a file.

    ``-o addopts=`` empties the ini option and nothing else. pytest applies
    ``PYTEST_ADDOPTS`` on top of it, and a subprocess inheriting this process's
    environment inherits that too — so a ``-k`` or a ``--deselect`` set in the
    ``python — pytest`` job's ``env:`` would shrink the report the guard reads
    *and* the collection the constant is reconciled against, together. That is
    the one edit moving both legs, arriving by a different road. The collection
    is given an explicit environment with the variable removed.
    """
    project = tmp_path / "project"
    tests = project / "tests" / "browser"
    tests.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tests / "test_stub.py").write_text(
        "def test_one() -> None: ...\ndef test_two() -> None: ...\n", encoding="utf-8"
    )

    injected = {"PYTEST_ADDOPTS": "-k 'not two'"}
    assert "PYTEST_ADDOPTS" not in collect_env(extra=injected)
    assert collect_env(neutralise_addopts=False, extra=injected)["PYTEST_ADDOPTS"] == "-k 'not two'"

    inheriting = collect_ids(tests, cwd=project, neutralise_addopts=False, extra_env=injected)
    assert inheriting == ["tests.browser.test_stub::test_one"], inheriting

    neutralised = collect_ids(tests, cwd=project, extra_env=injected)
    assert neutralised == [
        "tests.browser.test_stub::test_one",
        "tests.browser.test_stub::test_two",
    ], neutralised


def test_no_conftest_can_deselect_the_directory_out_from_under_both_legs() -> None:
    """A ``conftest.py`` is the third road to the same place, and neither flag shuts it.

    ``-o addopts=`` overrides ini settings and the explicit environment drops
    ``PYTEST_ADDOPTS``; a ``pytest_collection_modifyitems`` hook is neither. It
    is obeyed by the gating run and by this reconciliation's collection alike,
    so a hook that deselects browser tests would shrink both legs at once and
    the pair would prove nothing.

    pytest loads a ``conftest.py`` from two sets of directories, not one: the
    ancestors of the rootdir-relative path it is handed — here the repository
    root and ``tests/`` — *and* every directory it descends into while
    collecting, at any depth under the argument. A hook in either kind is given
    the whole item list, so ``tests/browser/fixtures/conftest.py`` — a
    directory that already exists — deselects exactly as well as one at the
    root: measured on this repository, ``pytest tests/browser --collect-only -q
    -o addopts=`` reports 7 of 8, and the whole-tree collection the gating run
    performs reports 1173 of 1174. The set checked here is therefore those two
    parents plus every ``conftest.py`` anywhere under ``tests/browser/``.

    A sibling that is on neither path is deliberately not checked, because it
    is not this road: ``tests/ci/conftest.py`` leaves ``tests/browser``
    collecting all eight, so it can only shrink the gating run, and the
    reconciliation above catches that.

    The repository has no ``conftest.py`` at all today, and this pins that:
    adding one under any of these paths is a deliberate act, and whoever adds
    it has to come here and re-think what this pair is holding.

    Residue, measured rather than feared: ``PYTEST_PLUGINS`` passes through
    ``collect_env`` untouched and a module named there can carry a
    ``pytest_collection_modifyitems`` hook, but it is not a both-legs route as
    things stand — the gating run's ``uv run pytest`` is a console script with
    no repository root on ``sys.path`` and dies with ``ImportError: Error
    importing plugin``, while this reconciliation's ``python -m pytest``
    imports it and loses the test; making one module work for both would mean
    putting the hook inside the installed package, which is already inside the
    residue this pull request names.
    """
    parents = [REPO_ROOT / "conftest.py", REPO_ROOT / "tests" / "conftest.py"]
    descended_into = sorted(BROWSER_TESTS.rglob("conftest.py"))
    present = [str(path) for path in [*parents, *descended_into] if path.exists()]
    assert present == [], (
        f"{present} exists, and pytest loads it when collecting tests/browser/. A "
        "pytest_collection_modifyitems hook there can deselect browser tests from the "
        "gating run and from this reconciliation at the same time, which is the one "
        "edit moving both legs that this pair exists to rule out — and neither "
        "-o addopts= nor the cleared PYTEST_ADDOPTS stops it. If a conftest.py is "
        "genuinely wanted here, that is a deliberate act: re-think this check rather "
        "than deleting this assertion — the alternative that keeps the legs "
        "independent is to stop this reconciliation's collection from obeying "
        "conftests at all, by adding --noconftest to collect_command."
    )


# --- the passing case -------------------------------------------------------


def test_a_full_run_with_nothing_skipped_passes(tmp_path: Path) -> None:
    assert run(report(tmp_path, browser_cases(guard.EXPECTED_BROWSER_TESTS))) == 0


def test_a_failing_browser_test_still_counts_as_having_run(tmp_path: Path) -> None:
    """A failure means the guard was exercised and said no. The suite reports that."""
    cases = browser_cases(guard.EXPECTED_BROWSER_TESTS - 1) + case_xml(
        EXPECTED_IDS[-1], '<failure message="assert False">boom</failure>'
    )
    assert run(report(tmp_path, cases)) == 0


def test_other_tests_in_the_report_are_ignored(tmp_path: Path) -> None:
    """The suite is 400-odd tests; only tests/browser/ answers this question."""
    other = '<testcase classname="tests.match.test_filters" name="test_a"/>'
    cases = other * 40 + browser_cases(guard.EXPECTED_BROWSER_TESTS)
    assert run(report(tmp_path, cases)) == 0


def test_the_passing_message_claims_only_what_the_report_shows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard counts and names test cases. It cannot see a browser, so it may
    not say it saw one: same-named stubs that launch nothing produce this same
    report, and a message that claimed otherwise would be false on that run."""
    run(report(tmp_path, browser_cases(guard.EXPECTED_BROWSER_TESTS)))
    printed = capsys.readouterr().out
    assert "ran in this run, and none was skipped" in printed
    assert "real browser" not in printed.split("What to do about it")[0]


# --- the failing cases, which are the point ---------------------------------


def test_a_skipped_browser_test_fails(tmp_path: Path) -> None:
    cases = browser_cases(guard.EXPECTED_BROWSER_TESTS - 1) + case_xml(
        EXPECTED_IDS[-1], '<skipped type="pytest.skip" message="no browser"/>'
    )
    assert run(report(tmp_path, cases)) == 1


def test_every_browser_test_skipped_fails(tmp_path: Path) -> None:
    """A module-level skip marker, which is the likeliest way this happens."""
    cases = browser_cases(
        guard.EXPECTED_BROWSER_TESTS,
        inner='<skipped type="pytest.skip" message="skipped by mark"/>',
    )
    assert run(report(tmp_path, cases)) == 1


def test_an_errored_browser_test_fails(tmp_path: Path) -> None:
    """A fixture or collection error aborts before the body: the guard never ran."""
    cases = browser_cases(guard.EXPECTED_BROWSER_TESTS - 1) + case_xml(
        EXPECTED_IDS[-1], '<error message="fixture failed">boom</error>'
    )
    assert run(report(tmp_path, cases)) == 1


def test_no_browser_tests_at_all_fails(tmp_path: Path) -> None:
    """The failure that looks most like success: nothing collected, nothing to check."""
    other = '<testcase classname="tests.match.test_filters" name="test_a"/>' * 40
    assert run(report(tmp_path, other)) == 1


def test_a_renamed_directory_fails(tmp_path: Path) -> None:
    """``tests/browser_driver/`` reports a different module prefix and does not count."""
    renamed = (
        '<testcase classname="tests.browser_driver.test_browser_driver" name="test_a"/>'
        * guard.EXPECTED_BROWSER_TESTS
    )
    assert run(report(tmp_path, renamed)) == 1


@pytest.mark.parametrize("count", [1, 4, 7, 9, 20])
def test_any_count_other_than_the_expected_one_fails(tmp_path: Path, count: int) -> None:
    assert count != guard.EXPECTED_BROWSER_TESTS
    assert run(report(tmp_path, browser_cases(count))) == 1


# --- the right number of the wrong tests ------------------------------------


def test_a_renamed_browser_test_fails_even_though_the_count_is_right(tmp_path: Path) -> None:
    """Eight of something is not eight of these.

    A rename, a split into two cheaper tests, or a test moved into
    ``tests/browser/`` while a never-submit one leaves it all net to eight. A
    count is satisfied by any of them; the pinned ids are not.
    """
    cases = browser_cases(guard.EXPECTED_BROWSER_TESTS - 1) + case_xml(
        f"{guard.BROWSER_CLASSNAME_PREFIX}test_browser_driver::test_renamed"
    )
    assert run(report(tmp_path, cases)) == 1


def test_a_reparametrised_browser_test_fails(tmp_path: Path) -> None:
    """The parameter id is part of the name, so dropping a case is visible.

    ``test_plan_without_literal_never_submit_is_refused`` is parametrised over
    the four shapes of a plan that does not carry a literal ``never_submit:
    true``. Dropping one of the four and adding a fifth case elsewhere keeps the
    count at eight.
    """
    kept = [
        test_id
        for test_id in EXPECTED_IDS
        if not test_id.endswith("test_plan_without_literal_never_submit_is_refused[False]")
    ]
    cases = "".join(case_xml(test_id) for test_id in kept) + case_xml(
        f"{guard.BROWSER_CLASSNAME_PREFIX}test_browser_driver::test_something_else"
    )
    assert run(report(tmp_path, cases)) == 1


def test_the_same_test_reported_twice_does_not_make_up_the_count(tmp_path: Path) -> None:
    """Eight cases, seven names. A duplicate is not the eighth test running."""
    cases = "".join(case_xml(test_id) for test_id in EXPECTED_IDS[:-1]) + case_xml(EXPECTED_IDS[0])
    assert run(report(tmp_path, cases)) == 1


def test_the_mismatch_names_the_tests_that_did_not_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = browser_cases(guard.EXPECTED_BROWSER_TESTS - 1) + case_xml(
        f"{guard.BROWSER_CLASSNAME_PREFIX}test_browser_driver::test_renamed"
    )
    run(report(tmp_path, cases))
    printed = capsys.readouterr().out
    assert EXPECTED_IDS[-1] in printed
    assert "test_renamed" in printed
    assert "unexercised" in printed


# --- the check cannot be carried out, which is also a failure ---------------


def test_an_absent_report_is_not_a_pass(tmp_path: Path) -> None:
    with pytest.raises(guard.GuardError):
        run(tmp_path / "nothing-here.xml")


def test_an_unparseable_report_is_not_a_pass(tmp_path: Path) -> None:
    path = tmp_path / "pytest-report.xml"
    path.write_text("<testsuites><testsuite>truncated", encoding="utf-8")
    with pytest.raises(guard.GuardError):
        run(path)


def test_an_empty_report_is_not_a_pass(tmp_path: Path) -> None:
    path = tmp_path / "pytest-report.xml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(guard.GuardError):
        run(path)


def test_the_wrong_number_of_arguments_is_a_failure() -> None:
    assert guard.main([str(GUARD_PATH)]) == 2


# --- the message a person reads when they have just broken it ---------------


def test_the_failure_says_the_guards_went_unexercised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(report(tmp_path, browser_cases(0)))
    printed = capsys.readouterr().out
    assert "never-submit" in printed
    assert "unexercised" in printed


def test_the_failure_says_the_expected_set_is_a_deliberate_edit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(report(tmp_path, browser_cases(guard.EXPECTED_BROWSER_TESTS - 1)))
    printed = capsys.readouterr().out
    assert "EXPECTED_BROWSER_TEST_IDS" in printed
    assert "EXPECTED_BROWSER_TESTS" in printed
    assert "deliberate" in printed
