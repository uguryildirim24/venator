"""One interface over the LLM runtimes the pipeline can run on.

    from venator.llm import complete

    answer = complete(prompt, model="sonnet", timeout=1200)
    answer.text   # what the model said
    answer.lane   # which runtime said it

The runtime is the local `claude` command, with the person's own subscription.
The Codex CLI and a bring-your-own key endpoint exist beside it, and neither is
ever reached for on the person's behalf: nothing falls through, and a different
lane runs only when VENATOR_LLM_RUNTIME names it. See venator/llm/adapter.py for
why, and venator/llm/environment.py for why a child process never inherits this
one's environment.

`venator.llm.probe` is deliberately not re-exported here: it is a command-line
entry point (`python -m venator.llm.probe`), and importing it from this package
would make running it that way re-execute an already-imported module. Import it
directly — `from venator.llm.probe import probe_lane`.
"""

from __future__ import annotations

from venator.llm.adapter import RUNTIME_VARIABLE, complete, unavailable_message
from venator.llm.environment import child_environment
from venator.llm.lanes import (
    DEFAULT_RUNTIME,
    LANES,
    ApiKeyLane,
    ClaudeLane,
    CodexLane,
)
from venator.llm.runtime import (
    DEFAULT_TIMEOUT,
    Answer,
    Completion,
    Invocation,
    Lane,
    LaneFailed,
    LaneStatus,
    LaneUnavailable,
    NoRuntimeAvailable,
    ProbeableLane,
    Request,
    Result,
    Runner,
    UnspawnableCommand,
    run_process,
)

__all__ = [
    "DEFAULT_RUNTIME",
    "DEFAULT_TIMEOUT",
    "LANES",
    "RUNTIME_VARIABLE",
    "Answer",
    "ApiKeyLane",
    "ClaudeLane",
    "CodexLane",
    "Completion",
    "Invocation",
    "Lane",
    "LaneFailed",
    "LaneStatus",
    "LaneUnavailable",
    "NoRuntimeAvailable",
    "ProbeableLane",
    "Request",
    "Result",
    "Runner",
    "UnspawnableCommand",
    "child_environment",
    "complete",
    "run_process",
    "unavailable_message",
]
