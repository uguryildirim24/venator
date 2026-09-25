"""The probe's `state` enumeration is one list, written down in three places.

`LaneStatus.state` is a published contract: `venator.llm.probe` prints it and
the dashboard's "connect a runtime" step branches on it. Three surfaces spell
the same list out — the docstring beside the field here, the schema paragraph in
`coordination/CONTRACTS.md`, and the `RuntimeState` union in
`ui/shared/onboarding.ts` — and nothing but this test makes them agree.

They have already drifted once, in the direction that hurts. `not_spawnable`
was added to the two TypeScript-facing surfaces and not to the Python one, so
the value the Python side actually emits was absent from its own authority. The
opposite drift is worse: a state Python can produce that the dashboard has never
heard of renders as an empty label next to a runtime someone is trying to
connect, and neither `tsc` nor `pnpm smoke` can see it coming, because the value
is born on this side of the boundary.
"""

from __future__ import annotations

import re
from pathlib import Path

from venator.llm.runtime import LaneStatus, LaneUnavailable

ROOT = Path(__file__).resolve().parents[2]


def _states_from_the_docstring() -> list[str]:
    """The indented `name  description` block in LaneStatus's docstring."""
    # Indentation is matched loosely on purpose: CPython 3.13 dedents a
    # docstring's common leading whitespace, so the column this reads at is a
    # property of the interpreter rather than of the file.
    docstring = LaneStatus.__doc__ or ""
    states = [
        match.group(1)
        for match in re.finditer(r"^ +([a-z_]+) {2,}\S", docstring, re.MULTILINE)
    ]
    assert states, "LaneStatus's docstring no longer enumerates the states"
    return states


def _states_from_the_contracts() -> list[str]:
    text = (ROOT / "coordination" / "CONTRACTS.md").read_text(encoding="utf-8")
    listing = re.search(r"`state` is one of\s*((?:`[a-z_]+`,?\s*)+)\.", text)
    assert listing is not None, "CONTRACTS.md no longer spells the state list out"
    return re.findall(r"`([a-z_]+)`", listing.group(1))


def _states_from_the_dashboard() -> list[str]:
    text = (ROOT / "ui" / "shared" / "onboarding.ts").read_text(encoding="utf-8")
    listing = re.search(r"export const RUNTIME_STATES[^=]*=\s*\[(.*?)\];", text, re.DOTALL)
    assert listing is not None, "ui/shared/onboarding.ts no longer exports RUNTIME_STATES"
    return re.findall(r'"([a-z_]+)"', listing.group(1))


def test_all_three_surfaces_name_the_same_states_in_the_same_order() -> None:
    # Order too, not just membership: the three lists are read side by side by
    # whoever adds the fourth state, and a reordering makes that comparison a
    # puzzle rather than a glance.
    docstring = _states_from_the_docstring()

    assert docstring == _states_from_the_contracts()
    assert docstring == _states_from_the_dashboard()


def test_every_state_the_python_side_can_produce_is_in_the_list() -> None:
    # LaneUnavailable.kind is where a state is minted: _probe_command reports
    # `state=unavailable.kind` verbatim. A new kind constant with no entry in
    # the list is the drift this test exists to stop, and it is the direction
    # the TypeScript compiler cannot see.
    minted = {
        value
        for name, value in vars(LaneUnavailable).items()
        if name.isupper() and isinstance(value, str)
    }

    assert minted <= set(_states_from_the_docstring()), (
        "LaneUnavailable mints a kind that no surface enumerates, so the "
        f"dashboard would render it as nothing: {sorted(minted)}"
    )
