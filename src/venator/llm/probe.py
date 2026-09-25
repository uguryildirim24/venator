"""Ask this machine which LLM runtimes it actually has.

    uv run python -m venator.llm.probe --json              # the default lane
    uv run python -m venator.llm.probe --lane codex --json # one named lane
    uv run python -m venator.llm.probe --all --json        # everything

Written for the dashboard's "connect a runtime" step, which has to tell a person
something true and specific before they can get started, and which shells out to
this module because the repo has no console scripts.

**Availability is measured, never inferred.** `available` is reported only for a
lane that was invoked and answered. There is no PATH check standing in for a
working install, no config file read standing in for a login, and no cache: a
runtime that was available a minute ago is not evidence about now, and this
package refuses on principle to learn whether someone is signed in by reading
their credentials rather than by asking their runtime.

**What it costs.** Probing a command lane spends one very small completion on
that person's own subscription — see PROBE_PROMPT in venator/llm/lanes.py for
the arithmetic, which comes to roughly twenty input tokens and a handful of
output tokens per lane, plus one process launch. Nothing measurable against a
subscription, but not nothing, which is why `--all` is opt-in rather than the
default: probing one runtime someone just picked is the common case, and
launching all three is a deliberate "what do I have?".

**What it never costs.** The key lane is not contacted. Its request would bill a
card at a price only the endpoint knows, so it is reported as `configured` or
`not_configured` and never as `available`.

Exit codes: 0 whenever the probe ran, including when every lane is unavailable —
an unavailable runtime is the answer, not an error, and a caller should not have
to read stderr to tell those apart. 2 only when the probe itself cannot run: an
unknown --lane, or both --lane and --all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence

from venator.llm.lanes import DEFAULT_RUNTIME, LANES, RUNTIME_VARIABLE
from venator.llm.runtime import LaneStatus, ProbeableLane, Runner, run_process


def probe_lane(
    name: str,
    *,
    lanes: Sequence[ProbeableLane] | None = None,
    runner: Runner | None = None,
    environ: Mapping[str, str] | None = None,
) -> LaneStatus:
    """Probe one lane by name. Raises ValueError if no such lane exists."""
    registry = {lane.name: lane for lane in (LANES if lanes is None else lanes)}
    lane = registry.get(name)
    if lane is None:
        raise ValueError(
            f"{name!r} names no runtime; available: {', '.join(registry)}"
        )
    status = lane.probe(
        run_process if runner is None else runner,
        os.environ if environ is None else environ,
    )
    return _mark_default(status)


def probe_all(
    *,
    lanes: Sequence[ProbeableLane] | None = None,
    runner: Runner | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[LaneStatus, ...]:
    """Probe every lane. One process launch each — a deliberate call."""
    registry = LANES if lanes is None else lanes
    return tuple(
        _mark_default(
            lane.probe(
                run_process if runner is None else runner,
                os.environ if environ is None else environ,
            )
        )
        for lane in registry
    )


def _mark_default(status: LaneStatus) -> LaneStatus:
    if status.lane != DEFAULT_RUNTIME:
        return status
    return LaneStatus(
        lane=status.lane,
        state=status.state,
        ready=status.ready,
        detail=status.detail,
        remedy=status.remedy,
        caveat=status.caveat,
        default=True,
    )


def render(statuses: Sequence[LaneStatus]) -> str:
    """The human form, for someone running this in a terminal."""
    lines: list[str] = []
    for status in statuses:
        mark = "ok " if status.ready else "-- "
        default = "  (default)" if status.default else ""
        lines.append(f"{mark}{status.lane}: {status.state}{default}")
        lines.append(f"      {status.detail}")
        if status.remedy:
            lines.append(f"      fix: {status.remedy}")
        if status.caveat:
            lines.append(f"      note: {status.caveat}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m venator.llm.probe",
        description="Report which LLM runtimes this machine can actually use.",
    )
    parser.add_argument("--lane", help=f"probe one runtime (default: {DEFAULT_RUNTIME})")
    parser.add_argument(
        "--all",
        action="store_true",
        help="probe every runtime; one process launch and one tiny completion each",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON on stdout")
    args = parser.parse_args(argv)

    if args.lane and args.all:
        print("--lane and --all ask for different things; pick one", file=sys.stderr)
        return 2

    try:
        if args.all:
            statuses = probe_all()
        else:
            chosen = args.lane or os.environ.get(RUNTIME_VARIABLE, "").strip()
            statuses = (probe_lane(chosen or DEFAULT_RUNTIME),)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([status.as_dict() for status in statuses], indent=2))
    else:
        print(render(statuses))
    # An unavailable runtime is data. The probe ran, so the probe succeeded.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
