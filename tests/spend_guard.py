"""One guard: no test in this suite ever pays for a completion.

The failure this exists to stop has already happened. A module that does
``from venator.llm.runtime import run_process`` binds its **own** name at import
time, so ``venator.llm.adapter.run_process`` and
``venator.llm.runtime.run_process`` are two separate names for one function.
Patching the second leaves the first pointing at the real spawn: the patch
reports success, the test passes, and real judging runs go out on the Owner's
own subscription. Five of them did.

So this does not patch a name a caller might not be looking at. It wraps the
one place a child process is actually born — ``subprocess.run``, looked up on
the module at call time by every caller in this repository — and refuses,
loudly, the moment the argument vector names a runtime that costs money. Every
route reaches it: a lane called with no stubbed runner, a module whose binding
somebody forgot, a helper written next year.

Three properties, and they are what make it a guard rather than a hope:

* It is **autouse and session-scoped** where it is installed, so a test does
  not opt in. A test that forgets to stub the boundary fails here instead of
  quietly spending money.
* It **fails rather than substitutes**. Returning a canned answer would let a
  test that patched nothing pass, and pass wrongly; the point is to make the
  omission visible.
* It refuses on *which command* is being started, not on spawning at all. The
  suite starts ``sys.executable`` all over the place and must go on doing so.

**Why this is a module and one one-line conftest per directory rather than one
``tests/conftest.py``.** That file may not exist:
``tests/ci/test_never_submit_coverage_guard.py`` pins its absence, because
pytest loads a root-level conftest while collecting ``tests/browser/`` and a
``pytest_collection_modifyitems`` hook there could deselect the browser tests
out of both legs of the never-submit reconciliation at once. A conftest in
``tests/llm/``, ``tests/match/``, ``tests/schedule/`` or ``tests/resume/`` is on
neither of the paths that guard checks, and the guard's own docstring says as
much about a sibling directory. The invariant stays exactly as strong as it was.
``tests/resume/`` is the newest of the four and is here because
``venator.resume.parse`` asks the Owner's runtime to read an uploaded PDF.

A test that genuinely wants to see what argv would have been spawned patches
``subprocess.run`` itself, function-scoped, which replaces this wrapper for its
own duration and is restored afterwards. That is the existing convention in
``tests/llm/test_windows_spawn.py`` and it keeps working.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

#: The commands that spend somebody's money. Matched on the basename, so an
#: absolute path to the same binary — which is what ``resolve_executable``
#: returns on Windows — is caught too.
BILLED_COMMANDS = frozenset({"claude", "claude.exe", "codex", "codex.exe"})

REFUSAL = (
    "a test was about to start `{runtime}` for real, which spends the Owner's "
    "subscription. The process boundary was not stubbed on the path this call "
    "took. Remember that a module which imported run_process by name holds its "
    "own binding: patching venator.llm.runtime.run_process does not reach "
    "venator.llm.adapter.run_process. Pass a runner, or patch the name the "
    "caller actually resolves — and assert it took, by identity."
)


def billed_runtime(argv: object) -> str:
    """The runtime this argument vector would start, or "" for anything else."""
    if isinstance(argv, str):
        candidate = argv
    elif isinstance(argv, (list, tuple)) and argv:
        first = argv[0]
        candidate = first if isinstance(first, str) else ""
    else:
        candidate = ""
    name = candidate.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name if name in BILLED_COMMANDS else ""


@pytest.fixture(autouse=True, scope="session")
def never_spawn_a_billed_runtime() -> Iterator[None]:
    """Make an unintended completion impossible, not merely unlikely."""
    real = subprocess.run

    def guarded(*args: Any, **kwargs: Any) -> Any:
        runtime = billed_runtime(args[0] if args else kwargs.get("args"))
        if runtime:
            raise AssertionError(REFUSAL.format(runtime=runtime))
        return real(*args, **kwargs)

    subprocess.run = guarded
    try:
        yield
    finally:
        subprocess.run = real
