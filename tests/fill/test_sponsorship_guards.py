"""The sponsorship invariant, pinned one layer at a time.

The planner is structurally unable to answer a sponsorship question "No", and
three separate pieces of code make that true. Only one of them was covered, and
covering the outermost hid the other two: every existing test asks a `radio` or
`select` field whose options are ordinary Yes/No spellings, and on that shape
the option backstop catches whatever the fact layer lets through. Ablate either
inner layer and the whole suite still passes.

The layers, innermost first:

1. ``_sponsorship_fact`` answers only from ``requires_sponsorship: true``.
   Relaxing it so ``false`` is answerable leaves the suite green while a `text`,
   `textarea` or `checkbox` sponsorship field starts carrying a "No" — those
   three kinds never reach the option backstop, because they have no options.
2. Canonical option resolution refuses any resolved option for the sponsorship
   fact that does not read as an affirmative. Deleting it leaves the suite green
   too: it only bites once an Owner-written ``option_aliases`` entry spells the
   affirmative as something like "Not required", which is exactly the spelling
   that turns the guard into the only thing standing between the Profile and a
   selected "No".
3. A label mentioning sponsorship never routes to the authorization fact —
   covered in ``tests/fill/test_work_authorization.py``.

None of this is about whether the Owner requires sponsorship. It is about the
planner never *stating* that they do not, on a question an employer reads as an
attestation, from a Profile that never said it.
"""

from __future__ import annotations

import pytest

from venator.fill.mapping import map_field
from venator.fill.plan import create_fill_plan

SPONSORSHIP_LABEL = "Will you now or in the future require sponsorship for employment visa status?"

RESUME = {
    "name": "Ada Lovelace",
    "contact": {"email": "ada@example.org", "location": "Boston, MA"},
}

#: Every field kind a FormSpec can carry a sponsorship question on, with the
#: options an ATS would offer for it. `text`, `textarea` and `checkbox` are the
#: three with no option list and therefore no backstop behind the fact layer.
KINDS = [
    ("text", None),
    ("textarea", None),
    ("checkbox", None),
    ("select", ["Yes", "No"]),
    ("radio", ["Yes", "No"]),
]

#: Everything that is not the literal ``True``. ``0`` and ``1`` are here because
#: they compare equal to the booleans and only an identity check separates them,
#: and ``"no"`` because a YAML value quoted by hand arrives as a string.
NOT_A_STATED_YES = [None, False, 0, 1, "", "no", "false", "Yes", []]


def field(kind: str, options: list[str] | None) -> dict:
    return {
        "label": SPONSORSHIP_LABEL,
        "kind": kind,
        "required": True,
        "selector": f"#{kind}",
        "options": options,
    }


def constraints(**work_authorization: object) -> dict:
    return {"work_authorization": {"status": "F-1 international student", **work_authorization}}


@pytest.mark.parametrize("kind, options", KINDS)
@pytest.mark.parametrize("stated", NOT_A_STATED_YES)
def test_only_an_explicit_true_answers_a_sponsorship_question(
    kind: str, options: list[str] | None, stated: object
) -> None:
    """Anything other than ``requires_sponsorship: true`` answers nothing at all.

    Pinned for **every** field kind, which is the part that was missing. On a
    `select` or `radio` the option backstop would refuse a leaked "No" anyway;
    on `text`, `textarea` and `checkbox` there is nothing behind this check, and
    a fact layer relaxed to ``value is None`` puts the literal string ``'No'``
    (or an unchecked box) on the form.
    """
    mapping = map_field(field(kind, options), RESUME, constraints(requires_sponsorship=stated))

    assert not mapping.mapped
    assert mapping.value is None
    assert mapping.value is not False


@pytest.mark.parametrize("kind, options", KINDS)
def test_an_absent_sponsorship_fact_answers_nothing_either(
    kind: str, options: list[str] | None
) -> None:
    """A Profile that says nothing is not a Profile that said no."""
    mapping = map_field(field(kind, options), RESUME, constraints())

    assert not mapping.mapped
    assert mapping.value is None


@pytest.mark.parametrize("kind, options, expected", [(k, o, True) for k, o in KINDS[:3]])
def test_a_stated_true_is_answered_so_the_guard_is_not_vacuous(
    kind: str, options: list[str] | None, expected: bool
) -> None:
    """The affirmative direction still works, on the kinds with no options.

    Without this the tests above would pass just as well against a mapper that
    answered nothing ever, and would stop being evidence of anything.
    """
    mapping = map_field(field(kind, options), RESUME, constraints(requires_sponsorship=True))

    assert mapping.mapped
    assert mapping.value == (True if kind == "checkbox" else "Yes")
    assert mapping.source == "constraints.yaml:work_authorization.requires_sponsorship"


def test_a_text_sponsorship_field_reaches_the_fillplan_unanswered() -> None:
    """End to end, on the kind with no backstop: nothing lands in ``fields``.

    A mapper that is right and a plan that answers anyway is the failure worth
    catching, so this reads the FillPlan the driver would execute rather than
    stopping at the mapper.
    """
    plan = create_fill_plan(
        "source:board:1",
        "https://example.org/apply",
        {"fields": [field("text", None)]},
        RESUME,
        constraints(requires_sponsorship=False),
    )

    assert [item["label"] for item in plan["fields"]] == []
    assert [item["label"] for item in plan["unmapped"]] == [SPONSORSHIP_LABEL]
    assert plan["never_submit"] is True


def test_an_owner_alias_cannot_spell_the_affirmative_as_a_refusal() -> None:
    """The option backstop, on the only shape that reaches it.

    ``option_aliases`` is the Owner's escape hatch for an ATS whose canonical
    spellings are its own, and it is the one route by which a *selected* option
    for the sponsorship fact can be something other than a Yes/No word. An entry
    mapping the affirmative onto "Not required" is a plausible thing to write —
    it reads like the Owner describing their own situation — and it would put
    the planner's mark against the answer that says no sponsorship is needed.
    Refused: an option that does not read as an affirmative is not selectable
    for this fact, whatever the alias table says.
    """
    constraints_value = constraints(requires_sponsorship=True)
    constraints_value["option_aliases"] = {"sponsorship": {True: ["Not required"]}}

    mapping = map_field(
        field("select", ["Not required", "Required"]),
        RESUME,
        constraints_value,
    )

    assert not mapping.mapped
    assert mapping.value is None
    assert "not an exact or canonical option" in mapping.reason


def test_the_same_alias_route_does_work_for_an_affirmative_spelling() -> None:
    """So the refusal above is the guard, not a dead alias lookup.

    Same mechanism, same Profile shape, an affirmative spelling: it resolves.
    That is what makes the previous test evidence that the backstop fired rather
    than evidence that aliases never match anything.
    """
    constraints_value = constraints(requires_sponsorship=True)
    constraints_value["option_aliases"] = {
        "sponsorship": {True: ["Yes — I will need sponsorship"]}
    }

    mapping = map_field(
        field("select", ["Yes — I will need sponsorship", "No"]),
        RESUME,
        constraints_value,
    )

    assert mapping.mapped
    assert mapping.value == "Yes — I will need sponsorship"
