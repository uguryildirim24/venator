"""The three lanes, and what each one needs before it will run.

`claude` is the default and the only lane the pipeline has ever used. The other
two exist, and neither is ever reached for on the person's behalf — see
venator/llm/adapter.py: a lane runs because it is the default or because
VENATOR_LLM_RUNTIME names it, never because the one before it stumbled.

1. `claude`  the local command, unmodified, using whatever subscription the
             person is already signed in with. The default.
2. `codex`   the Codex command, same shape: a local binary the person signed
             into themselves. Available, not blessed, and verified against an
             authenticated codex-cli 0.151.0 run. It is also
             the one lane that is an agent rather than a completion, which is
             why its argv pins a read-only sandbox; see CodexLane.
3. `api`     a bring-your-own key endpoint over plain HTTP. Inert until the
             person configures it explicitly; see ApiKeyLane.

Whether a lane can run is decided behaviourally — the lane is invoked and its
own answer is read. Nothing here inspects a credential store, opens a login
file, or reads a vendor key out of the environment to decide whether a lane
"looks" usable. That distinction is the point: the person signs in to the
unmodified binary, and this adapter only ever asks it to run.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from venator.llm.environment import child_environment
from venator.llm.runtime import (
    REPLACEMENT_CHARACTER,
    Answer,
    Invocation,
    LaneFailed,
    LaneStatus,
    LaneUnavailable,
    Request,
    Runner,
    UnspawnableCommand,
)

# `--bare` is deliberately absent and must stay absent: it does not read the
# subscription login at all, which turns the default lane into an unbilled
# stranger or an API charge depending on the day. tests/llm/test_lanes.py
# asserts no lane's argv carries it.
CLAUDE_COMMAND = "claude"
CODEX_COMMAND = "codex"

# The lane that runs when nobody names one. Not a first rung on a ladder: there
# is no ladder, and this is simply the runtime. The variable beside it is the
# only way any other lane ever runs.
DEFAULT_RUNTIME = "claude"
RUNTIME_VARIABLE = "VENATOR_LLM_RUNTIME"

# What a runtime says when it ran but has no usable login.
#
# This list used to be long, because it used to decide whether to *fall through*
# to somebody else's account. It no longer decides anything of the kind — the
# run ends either way — so its only remaining job is choosing which of two
# remedies the person reads: "install it" or "sign in to it". That is worth
# keeping, because those are different errands. It is also why the list is now
# short and literal.
#
# Phrases that were dropped on purpose: "unauthorized", "invalid api key",
# "authentication_error", "credit balance is too low", "command not found".
# Each can mean something other than "you are not signed in" — a proxy refusing
# a hop, a tool call failing mid-run, a balance to top up, a broken wrapper
# script — and each has a different fix. A confident wrong remedy is worse than
# no remedy: when nothing matches here, the failure carries the runtime's own
# words instead, which is the honest answer.
SETUP_MARKERS: tuple[str, ...] = (
    "not logged in",
    "not authenticated",
    "please log in",
    "please login",
    "login required",
    "run /login",
    "run `claude login`",
    "run `codex login`",
    "no credentials",
    "credentials not found",
)

# The remedy a person is told, in one place each. venator.llm.probe hands
# these to the dashboard's onboarding step and NoRuntimeAvailable renders them
# in a failure, so the wording cannot drift between what an error says and what
# a screen says: there is only one copy.
CLAUDE_INSTALL_REMEDY = (
    "install Claude Code on this machine and sign in to it yourself. Venator "
    "never signs you in, and never sees your password or your login."
)
CLAUDE_SIGN_IN_REMEDY = (
    "open Claude Code and sign in with the /login command, then start this again"
)


def unspawnable_remedy(command: str) -> str:
    """What to do when `command` is on PATH in a shape Windows cannot start.

    Built per command rather than written twice, so the two command lanes say
    the same true thing about different binaries. It names no path: this text is
    shown on screen and written to logs, and a path would carry someone's
    home directory.

    It also does not assert *which* unstartable shape was found. The npm shim is
    the case that will actually happen and is worth naming as the likely cause,
    but an extension-less file on PATH reaches here too, and a remedy that told
    that person "what is on PATH is a batch shim" would be a confident false
    statement about their own machine.
    """
    return (
        f"reinstall {command} as a real Windows program. What is on PATH is "
        "not something Windows can start on its own — an npm install leaves a "
        "batch shim, and Windows can only start one of those through the "
        "command interpreter, which Venator will not do on your behalf because "
        "it would hand your prompt to a shell that reparses it. "
        f"{command}'s own Windows installer leaves a real .exe. Then start "
        "this again."
    )


CODEX_INSTALL_REMEDY = (
    "install the Codex CLI and sign in to it yourself. You only need this if "
    "you want Venator to use Codex — the runtime it uses by default does not."
)
# `codex login` is the step as documented; it has not been run from here,
# because signing anyone into a vendor account is not this package's to do.
# So the sentence points at `codex --help` rather than insisting.
CODEX_SIGN_IN_REMEDY = (
    "sign in to the Codex CLI yourself: open a terminal, run `codex login`, and "
    "answer whatever it asks. Then start this again. If your copy of it calls "
    "that step something else, `codex --help` lists what it is called."
)

# The two flags that make the Codex lane safe to point at this checkout, and
# the reason they are not optional.
#
# Codex is not a completion endpoint. It is an agent with a workspace and
# file-writing tools, and the directory it would be pointed at is this one —
# which holds `data/`, an append-only store that is also the transport for the
# whole corpus (docs/adr/0001-git-as-transport.md). A lane that can edit a day
# file is a lane that can rewrite history nobody can get back. So:
#
#   -s read-only   the sandbox may read the checkout and may not write to it
#   -a never       and it may not stop to ask a human for permission to try,
#                  because there is no human watching a scheduled judging run
#
# Both flags are global Codex options, so they must be before `exec`. Neither is
# configurable and neither has a debugging escape hatch. This is the
# same posture as the never-submit invariant: enforced in code, covered by a
# test (tests/llm/test_lanes.py), not left to whoever refactors next. Changing
# it is the owner's decision, not a default anyone should be able to drift.
CODEX_SAFETY_FLAGS: tuple[str, ...] = ("-s", "read-only", "-a", "never")

# Hygiene, verified against codex-cli 0.149.1 and 0.151.0 rather than assumed:
#   --color never          ANSI escapes reach stderr even when piped; under the
#                          default `auto` a failure would carry raw escapes into
#                          the screen and the logs
#   --skip-git-repo-check  codex refuses to run outside a directory it trusts
#   --ephemeral            otherwise it persists session state from a Venator
#                          run into the person's own Codex history
CODEX_HYGIENE_FLAGS: tuple[str, ...] = (
    "--color",
    "never",
    "--skip-git-repo-check",
    "--ephemeral",
)

CODEX_MODEL_VARIABLE = "VENATOR_LLM_CODEX_MODEL"

# What a signed-out codex actually says, observed rather than guessed: with no
# credentials every run ends at
#   401 Unauthorized: Missing bearer or basic authentication in header
# on stderr (exit 1), or a terminal {"type": "turn.failed", ...} on stdout under
# --json. The full phrase is the marker, not the bare "401 unauthorized": a
# proxy in front of the endpoint can produce one of those without anyone being
# signed out, and a confident wrong remedy is worse than the runtime's words.
#
# This separation is heuristic for Codex and cannot be made otherwise: codex
# exits 1 for an auth failure, an untrusted directory and a failed turn alike,
# so the exit code carries no information and the text is all there is.
CODEX_SIGNED_OUT_MARKERS: tuple[str, ...] = (
    "missing bearer or basic authentication",
)

API_URL_VARIABLE = "VENATOR_LLM_API_URL"
API_MODEL_VARIABLE = "VENATOR_LLM_API_MODEL"
API_KEY_NAME_VARIABLE = "VENATOR_LLM_API_KEY_VAR"
API_HEADER_VARIABLE = "VENATOR_LLM_API_KEY_HEADER"
API_PREFIX_VARIABLE = "VENATOR_LLM_API_KEY_PREFIX"

# One remedy for both ways of getting VENATOR_LLM_API_KEY_VAR wrong, because
# there is one thing to do about either. It names no value read from the
# environment: it is rendered into a failure that gets committed.
API_KEY_PASTED_REMEDY = (
    "put your key in a variable of your own, then point "
    f"{API_KEY_NAME_VARIABLE} at that variable's name rather than at the key: "
    f"export MY_OWN_KEY=<your key>, then export {API_KEY_NAME_VARIABLE}"
    "=MY_OWN_KEY"
)

# A POSIX environment variable name, and nothing that could be a pasted key.
VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# CSI and OSC escape sequences, as they arrive on a terminal-shaped stderr.
#
# The OSC branch is bounded to one line and tolerates a missing terminator. Two
# reasons, both real on a stderr this code then slices: an OSC payload that
# itself contains an ESC used to make the whole branch fail, leaving the raw
# `ESC \` string terminator behind, and a sequence cut off mid-write by a
# killed process never had a terminator to find. A lone ST is stripped on its
# own for the same reason.
ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"         # CSI
    r"|\x1b\][^\x07\n]*?(?:\x07|\x1b\\|$)"  # OSC: BEL- or ST-terminated, or truncated
    r"|\x1b\\",                          # a string terminator with nothing before it
    re.MULTILINE,
)


# What venator.llm.probe asks a runtime, and what that costs.
#
# There is no way to learn whether someone is signed in except to ask the
# runtime — reading a credential store to find out is exactly what this package
# refuses to do — so a probe is a real completion on the person's own
# subscription. It is made as small as one can be: the prompt below is six
# words, the reply asked for is one, and no --model is passed, so a probe costs
# on the order of twenty input tokens and a handful of output tokens per lane
# probed. Small, but not zero, and it launches a real subprocess each time:
# probing is an onboarding step someone chooses, not something to poll.
PROBE_PROMPT = "Reply with only the word: ok"
PROBE_TIMEOUT = 120.0

# Asking the runtime to enforce the reply's shape, and what that is worth.
#
# `claude --json-schema <schema>` is a content constraint rather than an
# envelope: the command injects a tool whose input schema is the one given,
# tells the model to call it exactly once, validates the tool input with Ajv,
# and puts the validated object on its own `structured_output` field of the
# `--output-format json` envelope. `result` still carries the text.
#
# What that guarantees to this package is precise and worth stating exactly,
# because it is easy to over-read: the caller receives **either an object that
# validated against the schema, or an explicit failure**. It is not constrained
# decoding — the model can still emit a wrong shape, and what stops that shape
# reaching here is the command validating it and asking again. When the asking
# runs out, the envelope says so (`SCHEMA_RETRIES_SUBTYPE`) and this lane fails
# rather than handing back the unvalidated text sitting beside it.
#
# The schema must have an object at its root. That is not a preference: the
# schema becomes a tool's `input_schema`, and an array root is refused by the
# API itself — an observed `400 tools.N.custom.input_schema.type: Input should
# be 'object'`, with the run charged for and no reply. A root object also keeps
# the command's own strict-schema derivation working; a schema it cannot derive
# from is quietly downgraded to client-side validation alone, and nothing in
# the envelope says it happened. Callers build their schemas accordingly
# (`venator.resume.parse.RESUME_SCHEMA`).
JSON_SCHEMA_FLAG = "--json-schema"

# `result.subtype` when the command validated, re-asked, and gave up.
SCHEMA_RETRIES_SUBTYPE = "error_max_structured_output_retries"

# How long to wait on `claude --help`, which is the one question this package
# asks a runtime that costs nothing: no completion, no network, no login.
HELP_TIMEOUT = 60.0

# The three ways a run that asked for a schema can come back with no validated
# object. Each is a fixed sentence, and none of them quotes a single byte the
# command produced. That rule is the key lane's (see ApiKeyLane) and it applies
# here for the same reason: a runtime failure is shown on screen and written
# to logs, so whatever a failure carries is what a person sees.
SCHEMA_RETRIES_EXHAUSTED = (
    "the runtime was given a schema for the reply, kept re-asking for one that "
    "matched it, and ran out of attempts. There is no validated object to read, "
    "and the unvalidated text beside it is not a substitute. Nothing was "
    "recorded from this reply."
)
SCHEMA_ABSENT_FROM_REPLY = (
    "the runtime was given a schema for the reply and reported success without "
    "returning an object validated against it. There is nothing to read, and "
    "the unvalidated text beside it is not a substitute. Nothing was recorded "
    "from this reply."
)
SCHEMA_NO_ENVELOPE = (
    "the runtime was given a schema for the reply and did not return the JSON "
    "envelope that would carry the validated object. Nothing was recorded from "
    "this reply."
)

# What the caller prints so a run never *looks* constrained when it is not.
# One sentence, chosen by the lane that actually made the call, and empty when
# nobody asked for a shape at all.
SCHEMA_ENFORCED_NOTE = (
    "reply shape enforced at the call: the runtime was given the schema and "
    "returned an object it had already validated against it."
)
SCHEMA_FLAG_MISSING_NOTE = (
    f"reply shape NOT enforced at the call: this copy of the {CLAUDE_COMMAND} "
    f"command offers no {JSON_SCHEMA_FLAG} option, so the shape stated in the "
    "prompt and whatever the caller validates afterwards are the only guard. "
    "The run continues on those. A newer Claude Code has the option and "
    "nothing else has to change to use it."
)


def schema_unenforced_note(lane: str) -> str:
    """What a lane that was not given the schema says about itself.

    Deliberately says the schema was not *given* to the runtime rather than
    that the runtime could not have taken one. Codex has a verified
    `--output-schema` option of its own. This package does not use it. A note
    that claims the runtime cannot take a schema would be false.
    """
    return (
        f"reply shape NOT enforced at the call: the {lane} runtime is not given "
        "the schema, so the shape stated in the prompt and whatever the caller "
        "validates afterwards are the only guard. The run continues on those."
    )


# Verified in local Claude --help. Safe mode disables instructions, hooks,
# plugins and MCP; the empty tools argument disables the built-in tools.
CLAUDE_DOCUMENT_ONLY_FLAGS = ("--safe-mode", "--tools", "", "--no-session-persistence")


def require_claude_document_only(runner: Runner, environ: Mapping[str, str]) -> None:
    try:
        result = runner(Invocation(
            argv=(CLAUDE_COMMAND, "--help"), stdin="",
            environment=child_environment(environ), timeout=HELP_TIMEOUT,
        ))
        help_text = result.stdout + "\n" + result.stderr
        supported = result.returncode == 0 and all(
            flag in help_text for flag in CLAUDE_DOCUMENT_ONLY_FLAGS if flag
        )
    except (OSError, UnspawnableCommand, subprocess.TimeoutExpired):
        supported = False
    if not supported:
        raise LaneUnavailable(
            "claude", "text-only drafting controls could not be verified; no prompt was sent",
            "Use a Claude CLI with --safe-mode, --tools and --no-session-persistence, "
            "or explicitly select the API provider.",
        )


def supports_json_schema(runner: Runner, environ: Mapping[str, str]) -> bool:
    """Whether the installed `claude` offers --json-schema, found out by asking it.

    An older command — anything before the option existed — ignores an unknown
    flag or refuses to start, and either way a run that assumed enforcement
    would be a run that got none while saying it had it. So the flag is never
    passed on faith: `claude --help` is spawned first and its own text is read.
    That question costs nothing at all — no completion, no network, no login —
    which is what makes it affordable to ask before every constrained call
    rather than cached somewhere that can go stale.

    Anything that stops the question being answered reads as "no". The
    completion that follows spawns the same command through the same seam and
    will hit the same problem, and it is that call which knows how to turn a
    missing or unstartable `claude` into a LaneUnavailable with a remedy on it.
    Nothing is decided here beyond whether to pass a flag this command has not
    been seen to accept.
    """
    invocation = Invocation(
        argv=(CLAUDE_COMMAND, "--help"),
        stdin="",
        environment=child_environment(environ),
        timeout=HELP_TIMEOUT,
    )
    try:
        result = runner(invocation)
    except (OSError, UnspawnableCommand, subprocess.TimeoutExpired):
        return False
    return JSON_SCHEMA_FLAG in result.stdout or JSON_SCHEMA_FLAG in result.stderr


def _declines(result_stderr: str, result_stdout: str) -> bool:
    haystack = f"{result_stderr}\n{result_stdout}".lower()
    return any(marker in haystack for marker in SETUP_MARKERS)


def _probe_command(
    lane: ClaudeLane | CodexLane,
    runner: Runner,
    environ: Mapping[str, str],
    *,
    caveat: str = "",
) -> LaneStatus:
    """Ask a command lane whether it works, by making it work.

    Availability is never inferred: `available` is returned only after this lane
    actually ran and actually answered. Nothing here reads a config file, opens
    a credential store, or checks whether a binary happens to sit on PATH.

    The invocation is the ordinary one — same argv, same allowlisted child
    environment — so what the probe reports is what a real run would get,
    including the case where a key in the shell would have changed the answer.
    Being ordinary is also what makes it side-effect free: it is one completion,
    it signs nobody in, and it touches no credential store.
    """
    request = Request(prompt=PROBE_PROMPT, model=None, timeout=PROBE_TIMEOUT)
    try:
        lane.complete(request, runner, environ)
    except LaneUnavailable as unavailable:
        return LaneStatus(
            lane=lane.name,
            state=unavailable.kind,
            ready=False,
            detail=unavailable.reason,
            remedy=unavailable.remedy,
            caveat=caveat,
        )
    except subprocess.TimeoutExpired:
        return LaneStatus(
            lane=lane.name,
            state="timed_out",
            ready=False,
            detail=(
                f"{lane.name} was started and had not answered after "
                f"{PROBE_TIMEOUT:.0f}s"
            ),
            remedy=(
                f"run `{lane.name}` once by hand in a terminal and answer "
                "anything it asks. A first launch often waits at a sign-in "
                "prompt, which this check cannot see or answer for you."
            ),
            caveat=caveat,
        )
    except LaneFailed as failed:
        return LaneStatus(
            lane=lane.name,
            state="failed",
            ready=False,
            detail=str(failed),
            remedy=(
                f"{lane.name} is installed and signed in, and something else went "
                "wrong. The exit status is in the detail above — run it by hand in "
                "a terminal to see the rest."
            ),
            caveat=caveat,
        )
    return LaneStatus(
        lane=lane.name,
        state="available",
        ready=True,
        detail=f"{lane.name} ran and answered",
        caveat=caveat,
    )


@dataclass(frozen=True)
class ClaudeLane:
    """The local `claude` command.

    argv is `claude -p [--model M] --output-format json` with the prompt on
    stdin.
    """

    name: str = "claude"

    def argv(self, request: Request, *, schema: bool = False) -> tuple[str, ...]:
        """The argv, with the schema on it only when the command was seen to take it.

        `schema` is passed in rather than read off `request.schema` so this
        function cannot append a flag the installed command does not have. The
        caller has already asked the command (`supports_json_schema`), and that
        answer is the only thing that puts the flag here.

        The schema is serialised compactly and with its keys in a fixed order,
        so the same schema always produces the same argv — an argument that
        varies by dict ordering is one nobody can compare across two runs.
        """
        model = ("--model", request.model) if request.model else ()
        constraint = (
            (JSON_SCHEMA_FLAG, json.dumps(request.schema, sort_keys=True, separators=(",", ":")))
            if schema and request.schema is not None
            else ()
        )
        controls = CLAUDE_DOCUMENT_ONLY_FLAGS if request.document_only else ()
        return (CLAUDE_COMMAND, "-p", *model, "--output-format", "json", *constraint, *controls)

    def complete(
        self,
        request: Request,
        runner: Runner,
        environ: Mapping[str, str],
    ) -> Answer:
        if request.document_only:
            require_claude_document_only(runner, environ)
        constrained = request.schema is not None and supports_json_schema(runner, environ)
        invocation = Invocation(
            argv=self.argv(request, schema=constrained),
            stdin=request.prompt,
            environment=child_environment(environ),
            timeout=request.timeout,
        )
        try:
            result = runner(invocation)
        except UnspawnableCommand as unspawnable:
            raise LaneUnavailable(
                self.name,
                f"{CLAUDE_COMMAND} is installed, but Windows cannot start it as a "
                "child process, so this run was not attempted",
                unspawnable_remedy(CLAUDE_COMMAND),
                LaneUnavailable.NOT_SPAWNABLE,
            ) from None
        except (FileNotFoundError, PermissionError) as error:
            raise LaneUnavailable(
                self.name,
                f"the {CLAUDE_COMMAND} command could not be run ({type(error).__name__})",
                CLAUDE_INSTALL_REMEDY,
                LaneUnavailable.NOT_INSTALLED,
            ) from None
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(CLAUDE_COMMAND, request.timeout) from None
        except OSError as error:
            raise LaneFailed(f"{self.name} could not complete ({type(error).__name__})") from None
        # Stripped here for the same reason the Codex lane strips: this text is
        # shown on screen and written to logs, and raw escape sequences there
        # are a mess. This is the lane that actually runs by default, so it
        # needs it more, not less.
        stderr = _plain(result.stderr)
        if result.returncode != 0:
            if _declines(stderr, result.stdout):
                raise LaneUnavailable(
                    self.name,
                    f"{CLAUDE_COMMAND} is installed but reported no usable login",
                    CLAUDE_SIGN_IN_REMEDY,
                    LaneUnavailable.NOT_SIGNED_IN,
                )
            if constrained:
                # A schema failure exits non-zero with an empty stderr, so the
                # generic message below would say "exited 1: " and nothing else.
                # Ask the envelope first; it stays quiet when the reply is not
                # that envelope at all, and then the runtime's own words on
                # stderr are still the better diagnosis.
                envelope = _decoded_envelope(result.stdout)
                if envelope is not None:
                    _refuse_structured_failure(envelope, self.name)
            raise LaneFailed(
                f"{CLAUDE_COMMAND} -p exited {result.returncode}; no reply was accepted"
            )
        _refuse_undecodable(result.stdout, self.name)
        if constrained:
            return Answer(
                text=_structured_output(result.stdout, self.name),
                schema_enforced=True,
                schema_note=SCHEMA_ENFORCED_NOTE,
            )
        return Answer(
            text=_envelope_text(result.stdout, self.name),
            schema_note=(
                SCHEMA_FLAG_MISSING_NOTE if request.schema is not None else ""
            ),
        )

    def probe(self, runner: Runner, environ: Mapping[str, str]) -> LaneStatus:
        return _probe_command(self, runner, environ)


def _refuse_undecodable(stdout: str, lane: str) -> None:
    """Refuse a reply that did not survive being decoded.

    `run_process` decodes a child's stdout with ``errors="replace"`` rather than
    letting a UnicodeDecodeError out of ``communicate()``, where nothing can say
    which command failed. That trade is only safe if the damage is caught before
    anything reads the text: a reply mangled into U+FFFD can still be perfectly
    valid JSON, and the corrupted reason inside it would be appended to
    ``data/decisions/`` — append-only, committed, permanent (ADR-0001). A run
    that fails loudly can be run again; a corrupted Filter Decision cannot be
    taken back out.

    The message is constructed from a fixed sentence and a count, and nothing
    the child produced is interpolated into it. A lane failure is shown on
    screen and written to logs, and bytes that did not decode are exactly
    the bytes nobody can vouch for.
    """
    damaged = stdout.count(REPLACEMENT_CHARACTER)
    if damaged:
        raise LaneFailed(
            f"{lane} returned a reply that is not valid UTF-8: {damaged} "
            "character(s) did not decode, so what arrived is not what the "
            "runtime sent. Nothing was recorded from it."
        )


def _envelope_text(stdout: str, lane: str) -> str:
    """Unwrap `--output-format json`, which carries the reply under `result`."""
    try:
        envelope = json.loads(stdout)
    except ValueError as error:
        raise LaneFailed(f"{lane} returned output that is not JSON") from None
    if not isinstance(envelope, dict) or "result" not in envelope:
        raise LaneFailed(f"{lane} returned a JSON envelope with no result")
    if envelope.get("is_error") is True:
        raise LaneFailed(f"{lane} reported an unsuccessful completion")
    return str(envelope["result"])


def _decoded_envelope(stdout: str) -> dict[str, Any] | None:
    """The `--output-format json` envelope, or None when this is not one.

    None means "do not draw a conclusion from this", never "it was fine": every
    caller here treats it as the absence of an answer rather than the presence
    of a good one.
    """
    try:
        envelope = json.loads(stdout)
    except ValueError:
        return None
    return envelope if isinstance(envelope, dict) else None


def _refuse_structured_failure(envelope: dict[str, Any], lane: str) -> None:
    """Refuse an envelope that carries no object validated against the schema.

    Two shapes, both of which the command can produce with a reply sitting
    beside them that nothing checked:

      * `subtype` says the command validated, re-asked, and ran out of tries
      * anything else, `success` included, with `structured_output` absent or
        null — the documented case, and the reason a bare `subtype == success`
        is not enough to trust

    The unvalidated `result` text is never used as a stand-in for either. That
    is the whole point of having asked for a schema: a caller that falls back to
    the text on failure has the guarantee it started without, and has it
    silently. Both messages are fixed sentences and neither quotes anything the
    command produced — the failure is shown on screen and written to logs.
    """
    if envelope.get("subtype") == SCHEMA_RETRIES_SUBTYPE:
        raise LaneFailed(f"{lane}: {SCHEMA_RETRIES_EXHAUSTED}")
    if envelope.get("is_error") is True:
        raise LaneFailed(f"{lane} reported an unsuccessful completion")
    if envelope.get("structured_output") is None:
        raise LaneFailed(f"{lane}: {SCHEMA_ABSENT_FROM_REPLY}")


def _structured_output(stdout: str, lane: str) -> str:
    """The validated object `--json-schema` puts on its own envelope field.

    Returned re-serialised as JSON text, so a lane's answer is a string
    whichever way it was produced and the caller parses one shape. Nothing is
    reformatted on the way through: this is the object the command validated,
    written back out.
    """
    envelope = _decoded_envelope(stdout)
    if envelope is None:
        raise LaneFailed(f"{lane}: {SCHEMA_NO_ENVELOPE}")
    _refuse_structured_failure(envelope, lane)
    return json.dumps(envelope["structured_output"])


# Verified by local `codex exec --help` and `codex features list`; web_search
# and mcp_servers are documented in the official configuration reference.
# This narrows document generation, not an assurance that every present/future
# tool is absent. Managed configuration can still apply. The installed CLI
# accepts unified_exec=false but reports it effectively true in `features list`;
# shell_tool=false does take effect. Retain the sandbox and do not describe
# this collection of switches as a guarantee that no execution tool exists.
CODEX_DOCUMENT_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "memories",
    "multi_agent", "browser_use", "computer_use", "image_generation",
    "view_image", "code_mode", "code_mode_host", "skill_search",
)
CODEX_DOCUMENT_FLAGS = (
    "--ignore-user-config", "--ignore-rules",
    "-c", 'web_search="disabled"', "-c", "mcp_servers={}",
    *(part for feature in CODEX_DOCUMENT_DISABLED_FEATURES
      for part in ("--disable", feature)),
)


@dataclass(frozen=True)
class CodexLane:
    """The Codex command, non-interactive, prompt on stdin.

    The flag surface was checked against real `codex-cli` versions 0.149.1,
    0.150.1, and 0.151.0. Several plausible-looking guesses did not survive:

      * `--output-format` does not exist on codex at all (exit 2, which suggests
        `--output-schema`). Only the Claude lane uses that flag.
      * `--json` emits **JSONL** — one event object per line — and there is no
        `result` envelope anywhere in it. `_codex_text` reads the stream; there
        is deliberately no `json.loads(stdout)["result"]` fallback near it.
      * `-p` is a silent trap. On codex it is `--profile`, not "print": a run
        with `-p sonnet` was *accepted*, did not complain about the nonexistent
        profile, and quietly used the default model anyway. It also collides
        with venator's own Profile vocabulary. Long `--model` only, never `-p`.
      * Model names are not validated locally — a wrong one parses fine and
        fails at runtime — so a clean parse proves nothing about the model.
      * `-s read-only -a never` are global options. In particular, `-a` after
        `exec` is rejected with exit 2. The safety pair must precede `exec`.

    An authenticated run against codex-cli 0.151.0 verified the success path.
    `--json` emitted an `item.completed` event whose item had type
    `agent_message` and a text field. `-o FILE` wrote only the final text.
    `--output-schema` constrained a reply even when the prompt requested a
    conflicting value. This package does not yet pass that schema option.

    Codex also wants `bubblewrap` on PATH; without it a run fails, and this lane
    reports that as `failed` with codex's own words rather than guessing.

    The model is not taken from the caller's hint: a model name meaningful to
    one runtime means nothing to another, so this lane uses whatever the person
    signed in with unless VENATOR_LLM_CODEX_MODEL names one.
    """

    name: str = "codex"

    def argv(
        self, environ: Mapping[str, str], *, document_only: bool = False,
    ) -> tuple[str, ...]:
        """The one and only place a Codex invocation is built.

        There is no second path, and no parameter that can drop a flag: the
        safety pair goes in here or the lane does not run. `complete` below
        spawns exactly what this returns, and tests/llm/test_lanes.py asserts
        both halves of that — that the flags are present, and that nothing
        rebuilds the argv without them.
        """
        model = environ.get(CODEX_MODEL_VARIABLE, "").strip()
        # Long flag only. `-p` means --profile here and would fail silently.
        chosen_model = ("--model", model) if model else ()
        return (
            CODEX_COMMAND,
            *CODEX_SAFETY_FLAGS,
            "exec",
            "--json",
            *CODEX_HYGIENE_FLAGS,
            *(CODEX_DOCUMENT_FLAGS if document_only else ()),
            *chosen_model,
            "-",
        )

    def complete(
        self,
        request: Request,
        runner: Runner,
        environ: Mapping[str, str],
    ) -> Answer:
        invocation = Invocation(
            argv=self.argv(environ, document_only=request.document_only),
            stdin=request.prompt,
            environment=child_environment(environ),
            timeout=request.timeout,
        )
        try:
            result = runner(invocation)
        except UnspawnableCommand as unspawnable:
            raise LaneUnavailable(
                self.name,
                f"{CODEX_COMMAND} is installed, but Windows cannot start it as a "
                "child process, so this run was not attempted",
                unspawnable_remedy(CODEX_COMMAND),
                LaneUnavailable.NOT_SPAWNABLE,
            ) from None
        except (FileNotFoundError, PermissionError) as error:
            raise LaneUnavailable(
                self.name,
                f"the {CODEX_COMMAND} command could not be run ({type(error).__name__})",
                CODEX_INSTALL_REMEDY,
                LaneUnavailable.NOT_INSTALLED,
            ) from None
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(CODEX_COMMAND, request.timeout) from None
        except OSError as error:
            raise LaneFailed(f"{self.name} could not complete ({type(error).__name__})") from None
        stderr = _plain(result.stderr)
        if result.returncode != 0:
            # codex exits 1 for an auth failure, an untrusted directory and a
            # failed turn alike, so the code says nothing and only the text can
            # separate "sign in" from "something else went wrong".
            if _codex_declines(stderr, result.stdout):
                raise LaneUnavailable(
                    self.name,
                    f"{CODEX_COMMAND} is installed but reported no usable login",
                    CODEX_SIGN_IN_REMEDY,
                    LaneUnavailable.NOT_SIGNED_IN,
                )
            raise LaneFailed(
                f"{CODEX_COMMAND} exec exited {result.returncode}; no reply was accepted"
            )
        _refuse_undecodable(result.stdout, self.name)
        return Answer(
            text=_codex_text(result.stdout, self.name),
            schema_note=(
                schema_unenforced_note(self.name) if request.schema is not None else ""
            ),
        )

    def probe(self, runner: Runner, environ: Mapping[str, str]) -> LaneStatus:
        return _probe_command(self, runner, environ)


def _agent_message(event: object) -> str:
    """Pull the assistant's text out of one Codex event, tolerating its shapes."""
    if not isinstance(event, dict):
        return ""
    body = event.get("item") if isinstance(event.get("item"), dict) else event
    if not isinstance(body, dict):
        return ""
    if body.get("type") not in ("agent_message", "assistant_message", "message"):
        return ""
    for key in ("text", "message", "content"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _codex_text(stdout: str, lane: str) -> str:
    """Take the last assistant message out of the JSONL event stream.

    `--json` emits one event object per line and no envelope, so there is
    nothing here that reaches for a `result` key. Three outcomes, and the
    difference between the last two matters:

      * an assistant message was found — that is the answer
      * stdout is not the event stream at all (no JSON lines) — hand it back
        whole rather than lose a reply that was already paid for
      * stdout *is* the event stream and carries no assistant message — that is
        a failure, and saying so beats handing the caller a wall of events to
        choke on. A signed-out run ends this way, with a terminal `turn.failed`.
    """
    lines = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    if any(isinstance(event, dict) and event.get("type") in ("turn.failed", "error")
           for event in (_loads_or_none(line) for line in lines)):
        raise LaneFailed(f"{lane} reported an unsuccessful completion")
    messages = [
        text for line in lines for text in (_agent_message(_loads_or_none(line)),) if text
    ]
    if messages:
        return messages[-1]
    if not lines and stdout.strip():
        return stdout.strip()
    raise LaneFailed(
        f"{lane}: --json produced {len(lines)} events and no assistant message"
    )


def _codex_declines(stderr: str, stdout: str) -> bool:
    """Whether codex said it has no usable login — the observed phrases only."""
    haystack = f"{stderr}\n{stdout}".lower()
    return any(marker in haystack for marker in (*SETUP_MARKERS, *CODEX_SIGNED_OUT_MARKERS))


def _plain(text: str) -> str:
    """Strip ANSI escapes, which reach stderr even piped and even under --color never.

    A failure this lane produces is shown on screen and written to logs, and
    raw escape sequences there are a mess. Removal only: nothing is added or
    rewritten.
    """
    return ANSI.sub("", text)


def _loads_or_none(line: str) -> object:
    try:
        return json.loads(line)
    except ValueError:
        return None


Poster = Callable[[str, Mapping[str, str], dict[str, Any], float], tuple[int, str]]


def _post_json(
    url: str,
    headers: Mapping[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, str]:
    """The only HTTP this package does, over the dependency the repo already has."""
    import httpx

    response = httpx.post(url, headers=dict(headers), json=payload, timeout=timeout)
    return response.status_code, response.text


@dataclass(frozen=True)
class ApiKeyLane:
    """A bring-your-own key endpoint. Opt-in, and inert until it is opted into.

    Three variables must all be set, and one of them names *which* variable
    holds the key rather than the key being looked for under a vendor's name.
    That is the whole design: a key sitting in ANTHROPIC_API_KEY or
    OPENAI_API_KEY never activates this lane, because the person has to say, in
    so many words, that it is the one to bill. And even then the lane runs only
    because VENATOR_LLM_RUNTIME=api named it — configuring it does not make it
    the fallback for anything.

        VENATOR_LLM_API_URL     the endpoint, chosen by the person
        VENATOR_LLM_API_MODEL   the model to ask for there
        VENATOR_LLM_API_KEY_VAR the name of the variable holding their key

    The wire format is the OpenAI-shaped chat-completions body, which is what
    most endpoints a person would point this at accept. No SDK, no vendor-native
    second shape, and no credential ever reaches a child process.

    **This is the only lane that handles a secret, so it is the only one whose
    failures are built rather than quoted.** The failure is shown on screen
    and written to logs: a key that reaches them is visible. So nothing this
    endpoint says — no
    body, no header, no transport error text, not even the URL, which can
    carry a key in a query string — is ever interpolated into an exception.
    Every failure below is a fixed sentence with an integer or an exception
    class name in it. Scrubbing provider text would be a filter somebody has to
    keep correct forever; a constructed message cannot leak by construction.
    """

    name: str = "api"
    poster: Poster = field(default=_post_json, repr=False)

    def settings(self, environ: Mapping[str, str]) -> tuple[str, str, str]:
        """The endpoint, the model and the key — or LaneUnavailable saying why not.

        Shared by `complete` and `probe` so the two can never disagree about
        whether this lane is set up.
        """
        url = environ.get(API_URL_VARIABLE, "").strip()
        model = environ.get(API_MODEL_VARIABLE, "").strip()
        key_name = environ.get(API_KEY_NAME_VARIABLE, "").strip()
        missing = [
            name
            for name, value in (
                (API_URL_VARIABLE, url),
                (API_MODEL_VARIABLE, model),
                (API_KEY_NAME_VARIABLE, key_name),
            )
            if not value
        ]
        if missing:
            raise LaneUnavailable(
                self.name,
                "not set up (" + ", ".join(missing) + " unset)",
                "set " + ", ".join(missing) + " in your shell if you want Venator "
                "to use an API key you pay for. Leave them unset and this runtime "
                "stays switched off.",
                LaneUnavailable.NOT_CONFIGURED,
            )
        if not VARIABLE_NAME.fullmatch(key_name):
            # The value is not echoed back, deliberately: the most likely reason
            # it is not a variable name is that it is the key itself, pasted one
            # variable too early.
            raise LaneUnavailable(
                self.name,
                f"{API_KEY_NAME_VARIABLE} must hold the *name* of the variable "
                "that holds your key, not the key itself. What it holds is not a "
                "name a variable can have. Its value is not shown here on "
                "purpose: this message gets saved to a file that is committed.",
                API_KEY_PASTED_REMEDY,
                LaneUnavailable.NOT_CONFIGURED,
            )
        key = environ.get(key_name, "").strip()
        if not key:
            # The name is not echoed back either, for the same reason the branch
            # above does not echo it: a key that happens to be a valid variable
            # name — gsk_..., hf_..., ghp_..., a hex key starting with a letter —
            # reaches this branch instead of that one, and this message is
            # shown on screen and written to logs. The person can read their
            # own environment; the message only has to say what is wrong.
            raise LaneUnavailable(
                self.name,
                f"{API_KEY_NAME_VARIABLE} should hold the *name* of the variable "
                "that holds your key. The variable it names is empty — or what it "
                "holds is the key itself, pasted one variable too early. Its "
                "value is not shown here on purpose: this message gets saved to "
                "a file that is committed.",
                API_KEY_PASTED_REMEDY,
                LaneUnavailable.NOT_CONFIGURED,
            )
        return url, model, key

    def probe(self, runner: Runner, environ: Mapping[str, str]) -> LaneStatus:
        """Report whether this lane is set up — without contacting the endpoint.

        The command lanes are probed by running them, because a subscription
        completion is small and already paid for. This one is not: a request
        here bills a card, at a price only the endpoint knows, for a question
        nobody asked. So the probe stops at configuration and says so, and
        `ready` stays false: `available` is reserved for a lane that actually
        answered, and this one was never asked.
        """
        try:
            self.settings(environ)
        except LaneUnavailable as unavailable:
            return LaneStatus(
                lane=self.name,
                state=unavailable.kind,
                ready=False,
                detail=unavailable.reason,
                remedy=unavailable.remedy,
            )
        return LaneStatus(
            lane=self.name,
            state="configured",
            ready=False,
            detail=(
                "set up, and deliberately not contacted: even a test request to "
                f"the endpoint in {API_URL_VARIABLE} would spend your money to "
                "answer a question you did not ask"
            ),
            remedy=(
                f"set {RUNTIME_VARIABLE}=api in your shell when you want this "
                "endpoint to answer what Venator asks a model: drafting application "
                "documents or reading a resume. The first of those is the first "
                "request this runtime will ever make, and the first thing it will "
                "cost you."
            ),
        )

    def complete(
        self,
        request: Request,
        runner: Runner,
        environ: Mapping[str, str],
    ) -> Answer:
        url, model, key = self.settings(environ)
        header = environ.get(API_HEADER_VARIABLE, "").strip() or "Authorization"
        prefix = environ.get(API_PREFIX_VARIABLE, "Bearer ")
        headers = {header: f"{prefix}{key}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        try:
            status, body = self.poster(url, headers, payload, request.timeout)
        except Exception as error:
            raise LaneFailed(_transport_failure(self.name, error)) from None
        if status != 200:
            raise LaneFailed(_endpoint_failure(self.name, status, len(body)))
        # The one place provider text is carried at all — it is the answer, and
        # it becomes the caller's result: a drafted document or a parsed resume
        # that is written to disk. Redaction here is
        # belt to the constructed-message braces above: an endpoint that echoes
        # the key into the completion itself still cannot get it committed.
        #
        # Request.schema is deliberately not turned into a wire-format field
        # here. This lane speaks to whatever endpoint a person named, structured
        # output is spelled differently by every one of them, and guessing would
        # send a body that is silently ignored while this side reported the
        # shape as enforced. It says it did not enforce it, which is true.
        return Answer(
            text=_redact(_chat_text(body, self.name), key),
            schema_note=(
                schema_unenforced_note(self.name) if request.schema is not None else ""
            ),
        )


NOT_RECORDED = (
    "What the endpoint said is not shown here: this message is displayed and "
    "logged, and a reply can quote your key straight back at you. Make the "
    "request yourself if you need to read it."
)


def _endpoint_failure(lane: str, status: int, length: int) -> str:
    """A refusal, described entirely in integers this code chose."""
    return (
        f"{lane}: the endpoint named by {API_URL_VARIABLE} answered HTTP {status} "
        f"({length} bytes). {NOT_RECORDED}"
    )


def _transport_failure(lane: str, error: BaseException) -> str:
    """A failed request, described by exception class and nothing else.

    Not `str(error)`: an httpx error quotes the URL it was given, and a URL is
    somewhere a person can put a key.
    """
    return (
        f"{lane}: the request to the endpoint named by {API_URL_VARIABLE} failed "
        f"before any answer came back ({type(error).__name__}). {NOT_RECORDED}"
    )


def _redact(text: str, key: str) -> str:
    """Keep a key out of anything that can be printed, logged or committed."""
    return text.replace(key, "<redacted>") if key else text


def _chat_text(body: str, lane: str) -> str:
    try:
        answer = json.loads(body)
        content = answer["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise LaneFailed(
            f"{lane}: the endpoint named by {API_URL_VARIABLE} answered HTTP 200 "
            f"with {len(body)} bytes that are not the chat-completions shape. "
            f"{NOT_RECORDED}"
        ) from None
    return str(content)


# Every lane a person may name. Not a priority order — venator/llm/adapter.py
# runs exactly one of these, DEFAULT_RUNTIME unless VENATOR_LLM_RUNTIME says
# otherwise, and never reaches past it.
LANES: tuple[ClaudeLane | CodexLane | ApiKeyLane, ...] = (
    ClaudeLane(),
    CodexLane(),
    ApiKeyLane(),
)
