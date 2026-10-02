from __future__ import annotations

import pytest

from venator.contacts.matching import contact_link, matches, outreach_detail, reached_out
from venator.track.store import EVENT_ACTORS, fold_states
from venator.score.train import Label, training_labels


def contact(company="Example Therapeutics, Inc.", board="", person="Alex Example", id="a"):
    return {"id": id, "company": company, "board": board, "person": person}


def test_board_wins_without_company_fallback_and_multiple_people():
    posting = {"board": "example", "company": "Example Therapeutics"}
    contacts = [contact(), contact(person="Jamie Example", id="b"),
                contact(company="Other Lab", board="example", id="c"), contact(board="other", id="d")]
    assert [row["id"] for row in matches(posting, contacts, {})] == ["a", "b", "c"]
    assert matches(posting, [contact(company="Example Tx"), contact(company="Example")], {}) == []
    assert matches({"board": "example", "company": None}, [contact()], {"example": "  EXAMPLE   THERAPEUTICS  "})
    assert matches({"board": "unknown", "company": None}, [contact(company="unknown")], {}) == []


def test_shared_board_uses_office_company_not_profile_board_name():
    names = {"examplegroup": "Example Group"}
    contacts = [contact(company="Synthetic Office", id="a"), contact(company="Another Office", id="b"),
                contact(company="Example Group", id="c"), contact(board="examplegroup", id="d")]
    posting = {"board": "examplegroup", "company": "Synthetic Office Inc."}
    assert [row["id"] for row in matches(posting, contacts, names)] == ["a", "d"]


@pytest.mark.parametrize(("route", "href"), [
    ("alex@example.test", "mailto:alex@example.test"),
    ("mailto:alex@example.test", "mailto:alex@example.test"),
    ("https://www.linkedin.com/in/alex-example", "https://www.linkedin.com/in/alex-example"),
    ("javascript:alert(1)", None), ("file:///tmp/contact", None), ("https://", None),
])
def test_contact_routes(route, href):
    assert contact_link(route) == href


@pytest.mark.parametrize("standing", [None, "approve", "reject", "submit", "restore"])
def test_outreach_and_undo_leave_application_standing_and_labels_unchanged(standing):
    def event(kind, detail=None):
        return {"posting_key": "synthetic:lab:1", "event": kind, "actor": EVENT_ACTORS[kind],
                "detail": detail, "at": "2026-10-01T00:00:00Z", "profile_id": "synthetic"}
    events = [] if standing is None else [event(standing, "Applied" if standing == "submit" else None)]
    base = [Label("synthetic:lab:1", 0, "2026-09-01")]
    outreach = event("outreach", outreach_detail(contact()))
    undo = event("outreach_undo", outreach_detail(contact()))
    assert reached_out([outreach], "synthetic:lab:1") == {"a": outreach["at"]}
    assert reached_out([outreach, undo], "synthetic:lab:1") == {"a": None}
    for added in ([outreach], [outreach, undo]):
        assert fold_states([*events, *added], {}) == fold_states(events, {})
        assert training_labels(base, [*events, *added]) == training_labels(base, events)
