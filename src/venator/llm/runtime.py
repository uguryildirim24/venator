"""What a runtime is asked for, what comes back, and where the process boundary is.

The pipeline calls one function (`venator.llm.complete`); a *lane* is one way of
answering it — the local `claude` command, the Codex command, or a bring-your-own
key endpoint. This module holds only the shapes they share and the single seam
every child process goes through, so selection can be exercised in tests with no
binary installed anywhere.

Nothing here falls through from one lane to another: `claude` is the default,
and a different lane runs only when someone names it (see venator/llm/adapter.py
for why). So the two failure kinds below no longer steer control flow — both end
the run. They survive because they steer the *message*, and the person reading
it needs to know which of two different things to go and do:

  LaneUnavailable  the selected lane could not run here at all — the command is
                   not on PATH, or it ran and said it has no usable login. It
                   carries a remedy, because "install it" and "sign in to it"
                   are not the same errand.
  LaneFailed       the lane ran, was signed in, and still produced no answer.
                   There is no remedy to offer: the runtime's own words are the
                   diagnosis.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import Any, Protocol

DEFAULT_TIMEOUT = 1200.0

#: What `errors="replace"` in run_process leaves behind when a byte does not
#: decode. The lanes refuse a reply containing it: see run_process below.
REPLACEMENT_CHARACTER = "\ufffd"

#: The prefix on the scratch directory every child process is started in. Named
#: so that a directory left behind by a cleanup that could not finish says whose
#: it was: see neutral_working_directory.
SCRATCH_PREFIX = "venator-llm-"

# What Windows' CreateProcess can start on its own, and what it cannot.
#
# CreateProcessW with a NULL application name appends `.exe` to an
# extension-less command and searches PATH. It never reads PATHEXT — that is a
# cmd.exe and shutil.which behaviour — and it cannot start a batch file at all:
# Microsoft's own page says "To run a batch file, you must start the command
# interpreter". So the two routes to a `claude` on Windows end up very
# differently placed. Anthropic's native installer leaves a real
# `claude.exe`, which the bare name already finds, and shutil.which buys
# nothing there. An npm install leaves `claude.cmd`, which the bare name misses
# — and which shutil.which happily returns, because PATHEXT contains `.CMD`.
# Handing that back to subprocess would turn one honest failure into a
# different one, so anything that is not one of these is diagnosed here instead
# of spawned. The rule is stated as a list of what *can* be started rather than
# a list of shims to watch for, because the list of shims is open-ended: an npm
# `.cmd`, a `.bat`, a PowerShell `.ps1` and a plain extension-less file are all
# things shutil.which will hand back and CreateProcess will not run.
WINDOWS_EXECUTABLE_IMAGES: tuple[str, ...] = (".exe", ".com")


@dataclass(frozen=True)
class Request:
    """One prompt, plus the knobs every lane understands.

    ``schema`` is a JSON Schema the reply must match. It is a *request*, not a
    promise: only a lane that can hand the schema to the runtime it spawns will
    apply it, and a lane that cannot says so rather than dropping it quietly —
    see ``Answer.schema_enforced`` below. Absent means the caller asked for no
    shape at all.
    """

    prompt: str
    model: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    schema: Mapping[str, Any] | None = None
    document_only: bool = False


@dataclass(frozen=True)
class Answer:
    """What one lane produced, and whether the reply shape was enforced at the call.

    ``schema_enforced`` is true only when the runtime itself was handed the
    schema and returned something it had already validated against it. It is
    false both when nothing was asked for and when something was asked for and
    this lane could not apply it — and those two are told apart by
    ``schema_note``, which is empty in the first case and carries one plain
    sentence in the second. The caller prints that sentence, so a run whose
    shape is guarded only by the prompt and the validator never *looks* like a
    run the runtime constrained.
    """

    text: str
    schema_enforced: bool = False
    schema_note: str = ""


@dataclass(frozen=True)
class Completion:
    """The answer, and which lane produced it — the caller is never left guessing.

    It carries ``Answer``'s two schema fields unchanged, for the same reason:
    the one thing a caller must not have to infer is whether the shape it asked
    for was actually imposed on the reply.
    """

    text: str
    lane: str
    schema_enforced: bool = False
    schema_note: str = ""


@dataclass(frozen=True)
class Invocation:
    """A child process about to be started, fully described.

    ``cwd`` is the directory the child runs in, and the one value it may never
    hold is the directory this process happens to be sitting in. A coding agent
    started with no working directory of its own inherits the parent's, and both
    runtimes this package spawns read project instructions out of the directory
    they wake up in — Claude Code walks up from it collecting ``CLAUDE.md``,
    Codex does the same for its own file. Run a completion from a checkout and
    that checkout's own instructions are prepended to the prompt, billed to the
    Owner's subscription, from a file that changes on most commits and does not
    exist on an Install that has no clone. Two people's answers would then differ
    by where they happened to stand.

    So ``None`` — which is what every lane passes, because no lane has any
    business choosing — means ``run_process`` makes a fresh empty directory for
    that one call and removes it afterwards. There is no value meaning "inherit",
    and a lane cannot get one by leaving the field alone. A path here is a
    deliberate act by a caller that has a reason, and today nothing in this
    package has one.
    """

    argv: tuple[str, ...]
    stdin: str
    environment: Mapping[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    cwd: str | None = None


@dataclass(frozen=True)
class Result:
    """What that child process left behind."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Invocation], Result]


class LaneUnavailable(Exception):
    """The selected lane cannot run here. Nothing else is tried in its place.

    `kind` is the same distinction the prose makes, in a form a caller can
    branch on: venator.llm.probe hands it to the dashboard's onboarding step,
    which has to show a person either an install button or a sign-in
    instruction. Reading it off the wording of `reason` would be a parser
    nobody wants to own.
    """

    NOT_INSTALLED = "not_installed"
    NOT_SIGNED_IN = "not_signed_in"
    NOT_CONFIGURED = "not_configured"
    # The command is here, and it is here in a shape this platform cannot start
    # — today only the npm `.cmd` shim on Windows. Distinct from NOT_INSTALLED
    # because the remedy is different and because telling someone who has
    # installed a thing that it is not installed sends them round the same loop
    # again. `ui/shared/onboarding.ts` carries the matching state.
    NOT_SPAWNABLE = "not_spawnable"

    def __init__(
        self,
        lane: str,
        reason: str,
        remedy: str,
        kind: str = "unavailable",
    ) -> None:
        super().__init__(f"{lane}: {reason}")
        self.lane = lane
        self.reason = reason
        self.remedy = remedy
        self.kind = kind


class LaneFailed(RuntimeError):
    """The lane ran and produced no usable answer."""


class NoRuntimeAvailable(RuntimeError):
    """The selected runtime could not run.

    The message names which runtime was asked, what it was missing, the fix, and
    that nothing else was tried on the person's behalf.
    """


class Lane(Protocol):
    """One way of answering a Request."""

    name: str

    def complete(
        self,
        request: Request,
        runner: Runner,
        environ: Mapping[str, str],
    ) -> Answer:
        """Return the model's text, or raise LaneUnavailable / LaneFailed.

        The return is an ``Answer`` rather than a bare string so the one lane
        that can enforce ``Request.schema`` and the two that cannot report that
        from the same place the call was made. A lane deciding it here and a
        caller working it out separately would be two facts that can disagree.
        """


@dataclass(frozen=True)
class LaneStatus:
    """What one lane reports about itself when asked, for a caller to render.

    Structured on purpose: the dashboard's "connect a runtime" step needs to
    branch, not to read prose. `state` is one of

        available       invoked, and it answered
        not_installed   the command is not on PATH here
        not_spawnable   the command is here, in a shape this platform cannot
                        start as a child process — today the npm `.cmd` shim on
                        Windows. It was found and deliberately not run
        not_signed_in   the command is installed and reported no usable login
        not_configured  the key lane's opt-in variables are not all set
        configured      the key lane is set up, and was deliberately not
                        contacted — doing so would spend the person's money
                        without being asked
        timed_out       it was invoked and did not answer inside the timeout
        failed          it ran, was signed in, and errored for another reason

    That list is the same list as `coordination/CONTRACTS.md`'s and
    `ui/shared/onboarding.ts`'s, and
    `tests/llm/test_lane_states_are_one_list.py` is what keeps it so. A state
    this side can produce and the dashboard has never heard of renders as
    nothing at all.

    `ready` is the one-bit summary a button can be enabled from. `caveat` is
    non-empty when the lane's own invocation is itself unverified. `default`
    marks the lane that runs when nobody names one.
    """

    lane: str
    state: str
    ready: bool
    detail: str
    remedy: str = ""
    caveat: str = ""
    default: bool = False

    def as_dict(self) -> dict[str, str | bool]:
        """The shape the CLI prints and the dashboard consumes."""
        result: dict[str, str | bool] = {
            "lane": self.lane,
            "state": self.state,
            "ready": self.ready,
            "detail": self.detail,
            "remedy": self.remedy,
            "caveat": self.caveat,
            "default": self.default,
        }
        return result


class ProbeableLane(Protocol):
    """A lane that can also report on itself without being asked for a completion."""

    name: str

    def complete(
        self,
        request: Request,
        runner: Runner,
        environ: Mapping[str, str],
    ) -> Answer: ...

    def probe(self, runner: Runner, environ: Mapping[str, str]) -> LaneStatus:
        """Report whether this lane could answer, having found out by asking it."""


class UnspawnableCommand(Exception):
    """The command is on PATH, in a shape Windows cannot start as a child.

    Deliberately not a LaneUnavailable: this module knows nothing about lanes or
    remedies. It travels the same way FileNotFoundError does — out of
    run_process, to be turned into a LaneUnavailable by the lane that knows what
    to tell a person about *that* command. `found` is the filename only, never
    the directory: this text is shown on screen and written to logs, and the
    absolute path carries someone's home directory.
    """

    def __init__(self, command: str, found: str) -> None:
        self.command = command
        self.found = found
        super().__init__(
            f"{command} is on PATH as {found}, which Windows cannot start as a "
            "child process"
        )


def resolve_executable(
    command: str,
    *,
    os_name: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """argv[0], resolved the way the platform actually starts a process.

    On POSIX this returns the command unchanged, so nothing about the existing
    spawn moves. On Windows it prefers a real executable image, because that is
    the only thing CreateProcess can start:

    * an `.exe`/`.com` on PATH wins, and is returned as an absolute path;
    * nothing on PATH at all returns the bare name, so the spawn still raises
      FileNotFoundError and the lane still says "not installed", which is true;
    * anything else on PATH under that name — an npm `.cmd`, a `.bat`, a
      `.ps1`, or a file with no extension at all — raises UnspawnableCommand,
      because "not installed" would be a lie to someone who installed it. The
      test is what CreateProcess can start, not a list of shim extensions: an
      extension-less file is found by shutil.which and is exactly as
      unstartable as a `.cmd`, and telling that person to install what they
      have installed sends them round the same loop.

    Running a shim through COMSPEC is deliberately not attempted. It means
    handing the argv to cmd.exe's grammar, where Python adds no escaping of its
    own (CPython documents this under Security Considerations, gh-114539), and
    batch quoting is exactly the kind of thing that cannot be got right from a
    machine that cannot run it. The npm route is diagnosed, not supported.

    `os_name` and `which` are seams for tests. None of this has ever been run on
    Windows; it is reasoned from CreateProcessW's and shutil.which's documented
    behaviour and exercised on Linux by simulation.
    """
    if (os.name if os_name is None else os_name) != "nt":
        return command
    for suffix in WINDOWS_EXECUTABLE_IMAGES:
        image = which(command + suffix)
        if image:
            return image
    found = which(command)
    if found and PureWindowsPath(found).suffix.lower() not in WINDOWS_EXECUTABLE_IMAGES:
        raise UnspawnableCommand(command, PureWindowsPath(found).name)
    return command


@contextlib.contextmanager
def neutral_working_directory(chosen: str | None) -> Iterator[str]:
    """The directory one child process runs in, and nobody else's.

    A caller that named one gets it. Otherwise a fresh empty directory is made
    for this call and removed when it ends — including when the child times out
    or the spawn raises, because the removal is the context manager's and not a
    line at the bottom of a happy path.

    Empty is the whole point: the runtimes read instructions out of the
    directory they start in, so the one that costs nothing and says nothing is a
    directory with nothing in it. It is deliberately not any directory
    ``venator.paths`` resolves — inside a checkout that root *is* the directory
    holding ``CLAUDE.md`` — and deliberately not the home directory, where
    user-scope memory lives.

    ``ignore_cleanup_errors`` is a judgment about which failure to prefer. A
    scratch directory that will not delete — a Windows handle still open on it
    is the way that happens — must not raise past a completion the Owner has
    already paid for, so a leaked directory under the system temporary directory
    is accepted instead. It carries SCRATCH_PREFIX, so anyone who finds one
    knows what left it.
    """
    if chosen is not None:
        yield chosen
        return
    with tempfile.TemporaryDirectory(
        prefix=SCRATCH_PREFIX, ignore_cleanup_errors=True
    ) as scratch:
        yield scratch


def run_process(invocation: Invocation) -> Result:
    """The one place this package starts a child process.

    Deliberately thin: it does not build the environment (the lane does, from
    venator.llm.environment) and it does not interpret failure (the lane does).
    FileNotFoundError, PermissionError and UnspawnableCommand are left to propagate
    so a lane can turn "the command is not installed" (or "it is installed as
    something Windows cannot start") into LaneUnavailable, and so a test can
    stage those conditions without a filesystem.

    It does decide one thing, because this is the only place that can decide it
    once for every lane: *where* the child runs. See Invocation.cwd and
    neutral_working_directory. Doing it here rather than in each lane is what
    stops a lane added later from inheriting the parent's directory by simply
    not thinking about it.
    """
    command, *arguments = invocation.argv
    with neutral_working_directory(invocation.cwd) as scratch:
        completed = subprocess.run(
            [resolve_executable(command), *arguments],
            input=invocation.stdin,
            capture_output=True,
            # `encoding` is named rather than left to `text=True`, which decodes
            # with locale.getencoding() — the ANSI code page on a stock Windows
            # install, not UTF-8, until PEP 686 lands. A runtime's reply is JSON
            # from a Node process and arrives as UTF-8, so cp1252 either mangles it
            # into mojibake or raises UnicodeDecodeError out of communicate(), past
            # every handler here. On POSIX, where the locale is already UTF-8, that
            # half changes nothing.
            #
            # `errors` is a real change, and on POSIX too: `text=True` carried the
            # implicit errors="strict", so a byte that used to end the run with a
            # UnicodeDecodeError now decodes to U+FFFD. That is deliberate. A
            # UnicodeDecodeError is raised inside communicate(), where no lane can
            # say which command failed or what to do about it, and on Windows the
            # ANSI code page makes it reachable on an ordinary reply rather than a
            # corrupt one. So this boundary takes the damaged text in hand instead
            # of a traceback from inside the standard library.
            #
            # Lossy is only tolerable because it is not silent. A reply carrying
            # REPLACEMENT_CHARACTER is refused by the lane that asked for it
            # (venator.llm.lanes), before anything parses it — a mangled reply that
            # is still valid JSON would otherwise be appended to data/decisions/,
            # which is append-only, and the corruption would be permanent.
            encoding="utf-8",
            errors="replace",
            env=dict(invocation.environment),
            # Never inherited. A child with no `cwd` of its own starts where this
            # process stands, and where this process stands is somebody's checkout.
            cwd=scratch,
            timeout=invocation.timeout,
        )
    return Result(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
