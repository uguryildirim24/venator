"""Which runtime answers, and what to say when it cannot.

`claude` is the runtime. **Nothing falls through to another one.** Moving a run
off the person's Claude subscription and onto a Codex sign-in or a billed key is
exactly the surprise this package exists to prevent — the same surprise the
environment allowlist was written to stop a stray `ANTHROPIC_API_KEY` causing.
An allowlist that forbids the environment doing it, paired with a ladder that
does it deliberately, does not add up; so there is no ladder.

A different lane runs only when `VENATOR_LLM_RUNTIME` names it, in so many
words. That is the whole selection rule:

    (unset)                      -> claude, the local subscription lane
    VENATOR_LLM_RUNTIME=codex    -> codex, because the person said so
    VENATOR_LLM_RUNTIME=api      -> the person's own endpoint and key
    VENATOR_LLM_RUNTIME=nonsense -> ValueError naming what does exist

When the selected lane cannot run — not installed, or installed and not signed
in — the run ends there with NoRuntimeAvailable. The message names the runtime
that was tried, what it was missing, the fix, and that nothing else was reached
for. It is never Venator's decision whose account pays.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from venator.llm.lanes import DEFAULT_RUNTIME, LANES, RUNTIME_VARIABLE
from venator.llm.runtime import (
    DEFAULT_TIMEOUT,
    Completion,
    Lane,
    LaneUnavailable,
    NoRuntimeAvailable,
    Request,
    Runner,
    run_process,
)


def complete(
    prompt: str,
    *,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    schema: Mapping[str, Any] | None = None,
    document_only: bool = False,
    lanes: Sequence[Lane] | None = None,
    runner: Runner | None = None,
    environ: Mapping[str, str] | None = None,
) -> Completion:
    """Ask the selected runtime, and say which one answered.

    `schema` is a JSON Schema the reply should match. It changes nothing about
    which lane runs — selection is still the runtime or the variable, and
    nothing else — and it is never a silent request: the Completion says
    whether the lane that answered actually imposed it, and carries the one
    sentence to print when it did not. A caller that ignores those two fields
    gets exactly the guarantee it had before, which is the prompt's word and
    its own validator.

    ``document_only`` applies the lane's document-generation restrictions.
    Claude disables tools; Codex disables known tool features and user config
    while retaining its read-only sandbox. This is not universal tool isolation.
    It defaults off for existing callers.

    The three keyword seams (`lanes`, `runner`, `environ`) exist so selection is
    testable with no binary and no environment of its own; the pipeline calls
    this with a prompt, a model and, when it wants a shape back, a schema.
    """
    request = Request(
        prompt=prompt,
        model=model,
        timeout=timeout,
        schema=schema,
        document_only=document_only,
    )
    environment = os.environ if environ is None else environ
    run = run_process if runner is None else runner
    registry = {lane.name: lane for lane in (LANES if lanes is None else lanes)}

    named = environment.get(RUNTIME_VARIABLE, "").strip()
    chosen = named or DEFAULT_RUNTIME
    lane = registry.get(chosen)
    if lane is None:
        available = ", ".join(registry)
        if named:
            raise ValueError(
                f"{RUNTIME_VARIABLE}={named!r} names no runtime; available: {available}"
            )
        raise ValueError(
            f"the default runtime {DEFAULT_RUNTIME!r} is not among the lanes given; "
            f"available: {available}"
        )

    try:
        answer = lane.complete(request, run, environment)
    except LaneUnavailable as unavailable:
        others = [name for name in registry if name != chosen]
        raise NoRuntimeAvailable(
            unavailable_message(unavailable, named, others)
        ) from unavailable
    return Completion(
        text=answer.text,
        lane=lane.name,
        schema_enforced=answer.schema_enforced,
        schema_note=answer.schema_note,
    )


def unavailable_message(
    unavailable: LaneUnavailable,
    named: str,
    others: Sequence[str] = (),
) -> str:
    """Name the runtime that was tried, what it lacked, the fix, and the silence.

    The last paragraph is not decoration. Someone whose default lane just failed
    is one step from assuming a tool "should have" used the other login sitting
    on the machine; the message says plainly that it did not, and will not
    unless they ask for it by name.
    """
    scope = (
        f"{RUNTIME_VARIABLE}={named} names the runtime to use, and it cannot run."
        if named
        else f"The default LLM runtime is {unavailable.lane}, and it cannot run."
    )
    invitation = (
        f" To use one instead, name it yourself: "
        f"{RUNTIME_VARIABLE}={'|'.join(others)}."
        if others
        else ""
    )
    return "\n".join(
        [
            scope,
            f"  {unavailable.lane}: {unavailable.reason}",
            f"    fix: {unavailable.remedy}",
            "Nothing else was tried. Venator never moves a run onto another "
            "sign-in or another account on its own." + invitation,
        ]
    )
