"""A wall-clock ceiling on one Posting's Hard Filters, on every platform.

``patterns`` and ``restriction_patterns`` are Owner-written regular expressions,
and one typo — the classic ``(a+)+$`` — turns ``venator.schedule.loop`` into a
process that never returns and never says why. The ceiling is what turns that
into a bug report: it cannot name the offending pattern, because the run is
stopped from outside ``re``, but it names the Posting and the file to look in.

WHY THERE ARE TWO ARMS
----------------------

``signal.setitimer`` is the cheapest correct answer and it is what this module
uses wherever it exists: the alarm fires on the main thread, the exception is
raised out of ``re``'s frame, and nothing else in the process moves. That arm is
unchanged from the guard this module replaced.

``signal.SIGALRM`` does not exist on Windows. The guard said so and then
yielded — so the ceiling was inert on the one platform the shipped product is
about to reach, and the test that asserts the ceiling works hung a 30-minute CI
job on a suite that takes 31 seconds. Nothing said anything; that is the whole
defect. An inert guard is worse than no guard, because the repository goes on
believing it has one.

WHY THE SECOND ARM IS A PROCESS AND NOT A THREAD
------------------------------------------------

A watchdog thread looks like the obvious cheap answer and does nothing at all.
CPython's ``re`` holds the GIL for the whole of a match, so a thread waiting to
raise cannot even be scheduled, let alone preempt anything — and
``_thread.interrupt_main`` only takes effect between bytecode instructions,
which one ``re.search`` call is exactly one of. Measured rather than assumed:
a thread told to wake at 0.3s, alongside ``re.search(r"(a+)+$", "a" * 26 + "!")``
on the main thread, first ran at **4.82s** — the instant the match returned.
``tests/match/test_budget.py`` pins that finding so the next person to reach for
a thread here is told why not before they spend the day.

Which leaves a second process. ``apply_filters`` is a pure function of a Posting
and a ``FilterPolicy``, so it moves across a pipe intact: the child answers one
Posting at a time and the parent, which is doing nothing but waiting, enforces
the ceiling with a queue deadline and a kill — neither of which needs the
child's cooperation. One child is started per ``decide`` pass, lazily, and only
when there is a Posting to filter, so a run with nothing new to decide starts no
process at all.

WHY THE CHILD IS A PLAIN SUBPROCESS AND NOT ``multiprocessing``
---------------------------------------------------------------

This was written with ``multiprocessing.get_context("spawn")`` first, and the
corpus replay found the defect inside the fix — which the tests did not, because
every test runner has the one property that hides it. A spawned child
**re-imports the parent's ``__main__`` module**. Under ``pytest`` and under
``python -m venator.match.run`` that is harmless, because both have an
``if __name__ == "__main__":`` guard. Any other caller that reaches
``venator.match.run.run()`` at a module's top level does not, and on Windows —
where spawn is the only method there is — its child re-runs the whole stage,
tries to start a worker of its own, and dies with a ``RuntimeError`` about
bootstrapping. A ceiling whose correctness depends on how its caller's entry
point is written is not a ceiling.

A subprocess of this interpreter running a bootstrap of our own imports nothing
of the caller's. It is handed the parent's ``sys.path`` as its first message —
so a checkout on ``PYTHONPATH``, an editable install and a virtual environment
all resolve to exactly the ``venator`` the parent is running — and then speaks
length-prefixed pickle over its stdin and stdout. The parent reads that pipe on
a thread, which is safe for the reason a watchdog thread is not: the parent is
idle, so the GIL is free.

 A round trip pickles one Posting each way. The process arm adds
a startup cost and enforces the same deadline on Windows.

IF NEITHER ARM CAN BE ARMED
---------------------------

The run stops and says so, naming the platform and what is unprotected. Silently
inert is the outcome this module exists to remove.
"""

from __future__ import annotations

import json
import pickle
import queue
import signal
import struct
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, TypeAlias

from venator.match.filters import Decision, apply_filters
from venator.profile.schema import FilterPolicy
from venator.qualify.jev_contract import JevContext

#: The one callable ``decide`` puts a Posting through, whichever arm is armed.
#: The optional Jev context travels with the Posting; omitting it is the
#: legacy call.
Filtering: TypeAlias = Callable[..., Decision]


class BudgetError(OSError):
    """No ceiling could be armed, or the worker enforcing one was lost.

    An ``OSError`` deliberately, and for the same reason ``TimeoutError`` is
    one: every stage's ``main`` already catches ``OSError`` and reports it as a
    sentence. A fresh exception type at the top of the hierarchy would reach the
    Owner as a traceback, and which stage you happened to run should not decide
    whether you get a sentence or a stack (``tests/match/test_run_pending.py``).
    """


#: What the child runs. It fixes ``sys.path`` from the parent's own before it
#: imports anything of this project, so the worker is provably the same
#: ``venator`` the parent is running rather than whichever one is installed.
BOOTSTRAP = """\
import json, os, sys

# Windows opens the standard handles in binary already, but the frame stream
# cannot survive being wrong about that: a text-mode fd rewrites every
# newline byte, and a pickled Posting is full of them, so the parent would read
# a corrupted frame. Asserted rather than assumed; it costs one call.
if os.name == "nt":
    import msvcrt

    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

sys.path[:0] = json.loads(sys.stdin.buffer.readline().decode("utf-8"))

# stdout is the frame stream, so it is taken away before anything of this
# project is imported: one `print` under `venator.match` would otherwise land in
# the middle of a pickled answer. `standard_output` keeps the original stream
# object alive, because the wrapper closes the buffer underneath it when the
# last reference goes.
standard_output = sys.stdout
answers = standard_output.buffer
sys.stdout = sys.stderr

from venator.match.budget import serve

serve(answers)
"""

#: One frame on the wire: four bytes of big-endian length, then that many bytes
#: of pickle. Both directions, so neither side has to guess where a frame ends.
HEADER = struct.Struct(">I")

#: How long the parent waits for a worker it has asked to stop before killing
#: it. Only ever reached by a child wedged in a runaway match, which is
#: precisely the case that will not answer.
SHUTDOWN_SECONDS = 5.0


def alarm_can_be_armed() -> bool:
    """Whether ``signal.setitimer`` can interrupt the code about to run here.

    Two conditions, and both are about *this* call rather than about the
    platform: ``SIGALRM`` has to exist, and only the main thread may install a
    handler for it. Windows fails the first; a stage driven from a worker thread
    fails the second, on every platform.
    """
    return hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()


def over_budget(seconds: float, posting_key: str, targeting_path: Path) -> str:
    """What both arms say when a Posting exhausts its ceiling. One wording, one place."""
    return (
        f"Hard Filters did not finish within {seconds:g}s on Posting {posting_key!r} — a "
        f"`patterns` or `restriction_patterns` entry in {targeting_path} is almost "
        "certainly backtracking catastrophically (the classic shape is '(a+)+$'); simplify "
        "it, or express the wording as `terms` instead"
    )


def unprotected(seconds: float, targeting_path: Path, why: str) -> str:
    """What the run says when no ceiling can be armed at all.

    Loud, and it names the platform: the failure this replaced was that the
    ceiling was absent and nothing said so.
    """
    return (
        f"Hard Filters cannot be given a {seconds:g}s ceiling on this machine, so they are "
        f"refused rather than run unprotected. Platform {sys.platform!r} has no usable "
        "`signal.setitimer`, and the worker process that would enforce the ceiling instead "
        f"could not be started ({why}). Unprotected, a `patterns` or `restriction_patterns` "
        f"entry in {targeting_path} that backtracks catastrophically would hang this run with "
        "no message at all, which is the failure the ceiling exists to prevent."
    )


@contextmanager
def _alarm(seconds: float, posting_key: str, targeting_path: Path) -> Iterator[None]:
    """The POSIX arm, unchanged: an interval timer that raises out of ``re``'s frame."""

    def expire(number: int, frame: object) -> None:
        raise TimeoutError(over_budget(seconds, posting_key, targeting_path))

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# ---------------------------------------------------------------------------
# The wire, and the child that speaks it.
# ---------------------------------------------------------------------------


def _send(stream: BinaryIO, value: object) -> None:
    payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(HEADER.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def _read_exactly(stream: BinaryIO, size: int) -> bytes | None:
    """``size`` bytes, or None where the pipe ended before they arrived."""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _receive(stream: BinaryIO) -> object | None:
    header = _read_exactly(stream, HEADER.size)
    if header is None:
        return None
    body = _read_exactly(stream, HEADER.unpack(header)[0])
    return None if body is None else pickle.loads(body)


def serve(outgoing: BinaryIO) -> None:
    """The worker's whole life: answer one Posting at a time until asked to stop.

    Runs in the child process, entered from ``BOOTSTRAP``. It decides nothing
    the parent would not have decided — ``apply_filters`` is the same function
    over the same ``FilterPolicy``, which travels as itself — and it holds no
    state between Postings, so an answer never depends on what was asked before
    it.
    """
    incoming = sys.stdin.buffer
    filters = _receive(incoming)
    if not isinstance(filters, FilterPolicy):
        return
    while True:
        message = _receive(incoming)
        if isinstance(message, dict):
            posting, jev = message, None
        elif isinstance(message, tuple) and len(message) == 2 and isinstance(message[0], dict):
            posting, jev = message
            if jev is not None and not isinstance(jev, JevContext):
                return
        else:
            return
        try:
            verdict, rule, reason, facts = apply_filters(posting, filters, jev=jev)
            # ``facts`` as a plain dict: ``Decision``'s default is a
            # ``MappingProxyType``, which does not pickle. Every reader takes it
            # through ``dict(...)`` already, so this is the same mapping.
            answer: tuple[str, object] = ("reading", (verdict, rule, reason, dict(facts)))
        except Exception as error:  # noqa: BLE001 — re-raised in the parent as itself
            answer = ("error", _transportable(error))
        try:
            _send(outgoing, answer)
        except (OSError, ValueError):
            return


def _transportable(error: Exception) -> Exception:
    """The same exception where it can cross a pipe, and its text where it cannot.

    A malformed Owner pattern raises ``re.error`` and the Owner should see
    ``re.error``, not a wrapper. Anything that will not pickle is reported as
    its type and message rather than swallowed.
    """
    try:
        pickle.loads(pickle.dumps(error))
    except Exception:  # noqa: BLE001 — any pickling failure means "send the text"
        return RuntimeError(f"{type(error).__name__}: {error}")
    return error


class _Worker:
    """A child process that filters one Posting at a time, under a ceiling.

    The parent is idle while the child works, so the ceiling is enforced by a
    deadline on a queue and a kill. Neither asks the child to cooperate, which
    is the whole point: a child inside a catastrophically backtracking match
    cannot cooperate with anything.
    """

    def __init__(self, seconds: float, targeting_path: Path, filters: FilterPolicy) -> None:
        self._seconds = seconds
        self._targeting_path = targeting_path
        self._filters = filters
        self._process: subprocess.Popen[bytes] | None = None
        # Created per child rather than per ``_Worker``: a killed child's reader
        # thread can still put its EOF sentinel, and a queue shared with its
        # replacement would hand that sentinel to the next Posting's answer.
        self._answers: queue.Queue[bytes | None] = queue.Queue()

    def _start(self) -> subprocess.Popen[bytes]:
        try:
            if not sys.executable:
                raise RuntimeError("this build names no Python interpreter to re-run")
            process = subprocess.Popen(  # noqa: S603 — this interpreter, a constant bootstrap
                [sys.executable, "-c", BOOTSTRAP],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # A console window would flash on Windows for a desktop Install;
                # stderr is left alone so a child that genuinely crashes says so.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            stdin, stdout = process.stdin, process.stdout
            if stdin is None or stdout is None:
                raise RuntimeError("the worker process was given no pipes")
            # Strings only: an import hook can leave a non-string on `sys.path`,
            # and a path the child cannot be told about is better than a refusal.
            path = [entry for entry in sys.path if isinstance(entry, str)]
            stdin.write(json.dumps(path).encode("utf-8") + b"\n")
            _send(stdin, self._filters)
        except Exception as error:  # noqa: BLE001 — every start failure is the same refusal
            raise BudgetError(
                unprotected(self._seconds, self._targeting_path, type(error).__name__)
            ) from error
        # Read on a thread so the parent can put a deadline on the answer. Safe
        # here for exactly the reason a watchdog thread is not: this thread waits
        # on a pipe and the process it is waiting for is a different one, so
        # nothing it needs is behind a GIL some runaway match is holding.
        answers: queue.Queue[bytes | None] = queue.Queue()
        self._answers = answers
        reader = threading.Thread(target=_pump, args=(stdout, answers), daemon=True)
        reader.start()
        self._process = process
        return process

    def filtered(self, posting: dict, jev: JevContext | None = None) -> Decision:
        key = posting.get("key")
        posting_key = key if isinstance(key, str) else ""
        process = self._process or self._start()
        stdin = process.stdin
        try:
            if stdin is None:
                raise BrokenPipeError("the worker process has no stdin")
            _send(stdin, (posting, jev))
        except (OSError, ValueError, TypeError) as error:
            raise self._lost(posting_key, error) from error
        try:
            body = self._answers.get(timeout=self._seconds)
        except queue.Empty:
            self._kill(process)
            raise TimeoutError(
                over_budget(self._seconds, posting_key, self._targeting_path)
            ) from None
        if body is None:
            raise self._lost(posting_key, EOFError("the worker process ended"))
        kind, payload = pickle.loads(body)
        if kind == "error" and isinstance(payload, BaseException):
            raise payload
        verdict, rule, reason, facts = payload
        return Decision(verdict, rule, reason, facts)

    def _lost(self, posting_key: str, error: BaseException) -> BudgetError:
        """A worker that died mid-Posting is reported, never treated as a verdict.

        Constructed rather than quoted, for the reason ``venator.llm`` gives: a
        message assembled from a fixed sentence and a type name cannot carry
        anything the child happened to be holding into an append-only store.
        """
        return BudgetError(
            f"the worker process enforcing the {self._seconds:g}s Hard Filter ceiling stopped "
            f"answering on Posting {posting_key!r} ({type(error).__name__}) — no Filter "
            "Decision was taken for it and none was recorded"
        )

    def _kill(self, process: subprocess.Popen[bytes]) -> None:
        self._process = None
        process.kill()
        try:
            process.wait(SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover — a kill this ignores is a kernel bug
            pass
        _close(process)

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            try:
                # An empty frame rather than a closed pipe: the child returns
                # from `serve` of its own accord and exits zero.
                _send(process.stdin, None)
            except (OSError, ValueError):
                pass
        try:
            process.wait(SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(SHUTDOWN_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        _close(process)


def _pump(stdout: BinaryIO, answers: queue.Queue[bytes | None]) -> None:
    """Move whole frames off the child's stdout onto ``answers`` until it ends.

    A free function over the queue it was given, rather than a method: a worker
    that was killed for exhausting its ceiling still has this thread running,
    and it must not be able to put anything into a successor's queue.
    """
    while True:
        header = _read_exactly(stdout, HEADER.size)
        body = None if header is None else _read_exactly(stdout, HEADER.unpack(header)[0])
        answers.put(body)
        if body is None:
            return


def _close(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdin, process.stdout):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


@contextmanager
def hard_filter_budget(
    seconds: float, targeting_path: Path, filters: FilterPolicy
) -> Iterator[Filtering]:
    """The callable ``decide`` puts each Posting through, under a ceiling of ``seconds``.

    The targeting path and the policy rather than the ``Profile`` that holds
    them: the policy is what the worker is handed, and a whole ``Profile``
    carries mapping proxies and does not pickle. The path is only ever put in a
    message.

    ``seconds <= 0`` is the one way to run without a ceiling, and it is
    deliberate rather than accidental: a caller asks for it by naming it.
    """
    if seconds <= 0:

        def unbounded(posting: dict, jev: JevContext | None = None) -> Decision:
            return apply_filters(posting, filters, jev=jev)

        yield unbounded
        return
    if alarm_can_be_armed():

        def under_alarm(posting: dict, jev: JevContext | None = None) -> Decision:
            key = posting.get("key")
            with _alarm(seconds, key if isinstance(key, str) else "", targeting_path):
                return apply_filters(posting, filters, jev=jev)

        yield under_alarm
        return
    worker = _Worker(seconds, targeting_path, filters)
    try:
        yield worker.filtered
    finally:
        worker.close()
