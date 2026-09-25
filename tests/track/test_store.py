from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from venator.track.store import (
    EVENT_ACTORS,
    append_events,
    fold_states,
    load_events,
    validate_event,
)


def make_event(
    posting_key: str,
    event: str,
    *,
    detail: str | None = None,
    at: str = "2026-08-19T12:00:00+00:00",
) -> dict:
    return {
        "posting_key": posting_key,
        "event": event,
        "actor": EVENT_ACTORS[event],
        "detail": detail,
        "at": at,
    }


@pytest.mark.parametrize(
    "event",
    [
        {"posting_key": "p:1", "event": "approve", "actor": "pipeline", "detail": None, "at": "2026-08-19T12:00:00+00:00"},
        {"posting_key": "p:1", "event": "fill", "actor": "pipeline", "detail": None, "at": "2026-08-19T12:00:00+00:00"},
        {"posting_key": "p:1", "event": "outcome", "actor": "owner", "detail": "maybe", "at": "2026-08-19T12:00:00+00:00"},
        {"posting_key": "p:1", "event": "withdraw", "actor": "owner", "detail": "reason", "at": "2026-08-19T12:00:00+00:00"},
        {"posting_key": "p:1", "event": "approve", "actor": "owner", "detail": None, "at": "2026-08-19T12:00:00-07:00"},
    ],
)
def test_validate_event_rejects_contract_violations(event: dict) -> None:
    with pytest.raises(ValueError):
        validate_event(event)


def test_append_events_validates_whole_batch_before_writing(tmp_path: Path) -> None:
    track_dir = tmp_path / "track"
    valid = make_event("p:1", "approve")
    invalid = {**make_event("p:2", "approve"), "actor": "pipeline"}

    with pytest.raises(ValueError, match="actor"):
        append_events(track_dir, [valid, invalid], day=date(2026, 8, 19))

    assert not track_dir.exists()


def test_append_and_load_events_are_append_only(tmp_path: Path) -> None:
    track_dir = tmp_path / "track"
    first = make_event("p:1", "approve")
    second = make_event("p:2", "reject", at="2026-08-19T12:01:00+00:00")

    assert append_events(track_dir, [first], day=date(2026, 8, 19)) == 1
    assert append_events(track_dir, [second], day=date(2026, 8, 19)) == 1

    assert load_events(track_dir) == [first, second]
    stored = (track_dir / "2026-08-19.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in stored] == [first, second]


def test_fold_states_is_deterministic_and_preserves_tracked_history() -> None:
    decisions_forward = {
        ("p:2", "llm_score"): {
            "posting_key": "p:2",
            "stage": "llm_score",
            "verdict": "queue",
            "decided_at": "2026-08-19T11:00:00+00:00",
        },
        ("p:1", "llm_score"): {
            "posting_key": "p:1",
            "stage": "llm_score",
            "verdict": "kill",
            "decided_at": "2026-08-19T10:00:00+00:00",
        },
    }
    decisions_reverse = dict(reversed(decisions_forward.items()))
    events = [make_event("p:1", "approve")]

    forward = fold_states(events, decisions_forward)
    reverse = fold_states(events, decisions_reverse)

    assert forward == reverse
    assert list(forward) == ["p:2", "p:1"]
    assert forward["p:1"]["state"] == "approved"
    assert forward["p:2"] == {
        "posting_key": "p:2",
        "state": "queued",
        "detail": None,
        "since": "2026-08-19T11:00:00+00:00",
    }


def test_fold_states_uses_latest_outcome() -> None:
    events = [
        make_event("p:1", "outcome", detail="interview"),
        make_event(
            "p:1",
            "outcome",
            detail="offer",
            at="2026-08-19T13:00:00+00:00",
        ),
    ]

    assert fold_states(events, {})["p:1"] == {
        "posting_key": "p:1",
        "state": "concluded",
        "detail": "offer",
        "since": "2026-08-19T13:00:00+00:00",
    }


def test_a_posting_killed_as_a_duplicate_leaves_the_review_queue() -> None:
    """CONTEXT.md's whole reason for Dedup: the Owner never applies to it twice.

    A queue entry is derived from a historical ``llm_score``, but the corpus
    grows underneath it. A Posting queued before is killed at ``hard_filter``
    later, when the employer's own record of the same job arrives and Dedup
    recognizes the pair. Folding the score alone would leave it queued for good,
    because nothing writes an ``llm_score`` row any more.

    The latest hard-filter decision therefore has a veto, which is the ladder
    the dashboard's status column has always used. A Posting the Owner has
    already acted on keeps the state that action put it in: a TrackEvent is a
    record of something that happened and Dedup does not unmake it.
    """
    duplicate = "adzuna:us:duplicate"
    scored = "adzuna:us:scored"
    approved = "adzuna:us:approved"
    decisions = {
        (key, "llm_score"): {
            "posting_key": key,
            "stage": "llm_score",
            "verdict": "queue",
            "decided_at": "2026-08-19T10:00:00+00:00",
        }
        for key in (duplicate, scored, approved)
    }
    for key in (duplicate, approved):
        decisions[(key, "hard_filter")] = {
            "posting_key": key,
            "stage": "hard_filter",
            "verdict": "kill",
            "rule": "duplicate",
            "decided_at": "2026-08-26T10:00:00+00:00",
        }
    decisions[(scored, "hard_filter")] = {
        "posting_key": scored,
        "stage": "hard_filter",
        "verdict": "pass",
        "rule": None,
        "decided_at": "2026-08-19T09:00:00+00:00",
    }

    states = fold_states([make_event(approved, "approve")], decisions)

    assert duplicate not in states
    assert states[scored]["state"] == "queued"
    assert states[approved]["state"] == "approved"
