"""The Owner's own statement that they are authorized to work — and only that.

Before this, ``binary_work_authorization`` returned nothing unconditionally, so
"Are you legally authorized to work in the United States?" always landed in
``unmapped``: an Owner who simply is authorized had no way to say so. The fix is
one optional Profile fact, ``work_authorization.authorized_to_work``, that the
Owner writes themselves.

Two things it must not become, and both are load-bearing:

* **A blank must never render as a "No".** Not answered and answered-no stay
  distinguishable the whole way to the form, so the tests below follow the value
  through ``create_fill_plan`` rather than stopping at the mapper — a ``None``
  that is correct in the Profile and arrives at the FillPlan as ``""`` or
  ``False`` is the interesting failure.
* **A route to answering a sponsorship question "No".** "Authorized to work" and
  "requires sponsorship" are different questions with different fields, and the
  planner stays structurally unable to answer the second in the negative. That
  guard existing matters more than this feature does.
"""

from __future__ import annotations

from typing import Mapping

import pytest

from venator.fill.mapping import LABEL_FACTS, fact_name_for_label, map_field, normalize_label
from venator.fill.plan import STANDARD_FIELDS, create_fill_plan

AUTHORIZATION_LABEL = "Are you legally authorized to work in the United States?"
SPONSORSHIP_LABEL = "Will you now or in the future require sponsorship for employment visa status?"

RESUME = {
    "name": "Ada Lovelace",
    "contact": {"email": "ada@example.org", "location": "Boston, MA"},
}


def constraints(**work_authorization: object) -> dict:
    return {"work_authorization": {"status": "F-1 international student", **work_authorization}}


def field(label: str, kind: str, options: list[str] | None = None) -> dict:
    return {
        "label": label,
        "kind": kind,
        "required": True,
        "selector": f"#{kind}",
        "options": options,
    }


def plan_for(constraints_value: Mapping[str, object]) -> dict:
    """A whole FillPlan over the standard field set, as the Fill stage builds one."""
    return create_fill_plan(
        "source:board:1",
        "https://example.org/apply",
        {"fields": [dict(item) for item in STANDARD_FIELDS]},
        RESUME,
        constraints_value,
    )


def answered(plan: Mapping[str, object], label: str) -> object:
    values = [item for item in plan["fields"] if item["label"] == label]
    return values[0]["value"] if values else None


def unmapped_reason(plan: Mapping[str, object], label: str) -> str | None:
    reasons = [item["reason"] for item in plan["unmapped"] if item["label"] == label]
    return reasons[0] if reasons else None


@pytest.mark.parametrize(
    "stated, expected",
    [(True, "Yes"), (False, "No")],
)
def test_the_owners_stated_authorization_is_answered_with_its_source(
    stated: bool, expected: str
) -> None:
    """Both directions come from the Owner's statement, never from an inference."""
    mapping = map_field(
        field(AUTHORIZATION_LABEL, "radio", ["Yes", "No"]),
        RESUME,
        constraints(authorized_to_work=stated),
    )

    assert mapping.mapped
    assert mapping.value == expected
    assert mapping.source == "constraints.yaml:work_authorization.authorized_to_work"


@pytest.mark.parametrize(
    "silent",
    [
        {},
        {"authorized_to_work": None},
        # The "or anything that is not a boolean" half of the rule, which the
        # two cases above do not reach. ``0`` compares equal to ``False`` and
        # only the identity check separates them; ``""`` and ``[]`` are the
        # falsy values a hand-edited YAML file produces; ``"no"`` is a quoted
        # answer that looks like one and is not.
        {"authorized_to_work": ""},
        {"authorized_to_work": 0},
        {"authorized_to_work": "no"},
        {"authorized_to_work": []},
    ],
)
def test_a_silent_profile_leaves_the_question_for_the_owner(silent: dict) -> None:
    """Absent means "not configured" — it is not an answer, and it is not "No"."""
    mapping = map_field(
        field(AUTHORIZATION_LABEL, "radio", ["Yes", "No"]),
        RESUME,
        constraints(**silent),
    )

    assert not mapping.mapped
    assert mapping.value is None
    assert "work_authorization.authorized_to_work" in mapping.reason
    assert "never answered 'No'" in mapping.reason


@pytest.mark.parametrize("silent", [{}, {"authorized_to_work": None}])
def test_an_unanswered_question_reaches_the_fillplan_as_unmapped_not_as_a_value(
    silent: dict,
) -> None:
    """End to end: the ``None`` in the Profile must not become ``""`` or ``False``.

    A mapper that is right and a plan that quietly answers anyway is the failure
    worth testing for, so this reads the FillPlan the driver would execute: the
    label appears in ``unmapped`` with a reason, appears nowhere in ``fields``,
    and no field in the plan carries a falsy answer that a form would render.
    """
    plan = plan_for(constraints(**silent))

    assert answered(plan, AUTHORIZATION_LABEL) is None
    assert AUTHORIZATION_LABEL not in [item["label"] for item in plan["fields"]]
    assert unmapped_reason(plan, AUTHORIZATION_LABEL) is not None
    assert all(item["value"] not in ("", False, None) for item in plan["fields"])
    assert plan["never_submit"] is True


def test_a_stated_no_is_the_owners_answer_and_stays_distinct_from_silence() -> None:
    """``false`` and unset must not collapse into one falsy value anywhere."""
    stated = plan_for(constraints(authorized_to_work=False))
    silent = plan_for(constraints())

    assert answered(stated, AUTHORIZATION_LABEL) == "No"
    assert unmapped_reason(stated, AUTHORIZATION_LABEL) is None
    assert answered(silent, AUTHORIZATION_LABEL) is None
    assert unmapped_reason(silent, AUTHORIZATION_LABEL) is not None


def test_stating_authorization_does_not_answer_a_sponsorship_question() -> None:
    """The invariant this feature must not become a route around.

    ``requires_sponsorship`` is false here *and* the Owner has said they are
    authorized — the most tempting shape there is for inferring "No sponsorship
    needed". The planner still says nothing.
    """
    plan = plan_for(constraints(authorized_to_work=True, requires_sponsorship=False))

    assert answered(plan, AUTHORIZATION_LABEL) == "Yes"
    assert answered(plan, SPONSORSHIP_LABEL) is None
    assert unmapped_reason(plan, SPONSORSHIP_LABEL) is not None


@pytest.mark.parametrize(
    "options",
    [["Yes", "No"], ["No"], ["No, I do not require sponsorship", "Yes"]],
)
def test_a_sponsorship_question_is_still_unanswerable_in_the_no_direction(
    options: list[str],
) -> None:
    """Whatever the Profile says about authorization, and whatever the ATS offers."""
    for work_authorization in (
        {},
        {"authorized_to_work": True},
        {"authorized_to_work": False},
        {"authorized_to_work": True, "requires_sponsorship": False},
    ):
        mapping = map_field(
            field(SPONSORSHIP_LABEL, "radio", options),
            RESUME,
            constraints(**work_authorization),
        )

        assert not mapping.mapped
        assert mapping.value is None


def test_a_combined_question_is_neither_question() -> None:
    """One label asking both is answered by neither fact.

    An ATS that asks "authorized to work without sponsorship" is asking about
    sponsorship too, and the authorization fact says nothing about sponsorship.
    """
    mapping = map_field(
        field(
            "Are you legally authorized to work in the United States without sponsorship?",
            "radio",
            ["Yes", "No"],
        ),
        RESUME,
        constraints(authorized_to_work=True),
    )

    assert not mapping.mapped


def test_no_authorization_alias_can_ever_route_a_sponsorship_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard against the *next* person widening the alias list.

    Today no ``binary_work_authorization`` alias mentions sponsorship, so the
    combined phrasing above is unmapped simply because nothing claims it. That
    is an accident of the current list, and the invariant must not rest on it:
    the day someone adds "Are you authorized to work here without sponsorship?"
    as an authorization alias — a reasonable-looking edit — the Profile's
    authorization fact would start answering a sponsorship question. It does
    not, because a label that mentions sponsorship is refused at the routing
    step whatever the alias table says.
    """
    combined = normalize_label("Are you authorized to work here without sponsorship?")
    monkeypatch.setitem(LABEL_FACTS, combined, "binary_work_authorization")

    assert fact_name_for_label("Are you authorized to work here without sponsorship?") is None
    assert map_field(
        field("Are you authorized to work here without sponsorship?", "radio", ["Yes", "No"]),
        RESUME,
        constraints(authorized_to_work=True),
    ).mapped is False


def test_an_option_that_smuggles_sponsorship_in_is_refused_even_by_alias() -> None:
    """The option list is the other way the two questions could be merged.

    An Owner-written alias is the only route by which a sponsorship-bearing
    option could be selected for the authorization field, so that route is shut
    as well: the Profile stated authorization, not sponsorship.
    """
    constraints_value = constraints(authorized_to_work=True)
    constraints_value["option_aliases"] = {
        "binary_work_authorization": {
            "Yes": ["Yes, and I do not require sponsorship"],
        }
    }

    mapping = map_field(
        field(
            AUTHORIZATION_LABEL,
            "radio",
            ["Yes, and I do not require sponsorship", "No"],
        ),
        RESUME,
        constraints_value,
    )

    assert not mapping.mapped
    assert "not an exact or canonical option" in mapping.reason


def test_authorization_is_never_inferred_from_an_immigration_status() -> None:
    """``status`` describes; it does not attest. Only the explicit fact answers."""
    mapping = map_field(
        field(AUTHORIZATION_LABEL, "radio", ["Yes", "No"]),
        RESUME,
        {"work_authorization": {"status": "US citizen"}},
    )

    assert not mapping.mapped
