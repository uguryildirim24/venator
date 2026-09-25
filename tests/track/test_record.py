from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.track.record import record_event
from venator.track.store import EVENT_ACTORS, load_events


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def setup_data(tmp_path: Path) -> tuple[Path, Path, Path]:
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    track_dir = tmp_path / "track"
    write_jsonl(postings_dir / "2026-08-19.jsonl", [{"key": "p:1"}, {"key": "p:2"}])
    return postings_dir, decisions_dir, track_dir


def stored_event(posting_key: str, event: str, detail: str | None = None) -> dict:
    return {
        "posting_key": posting_key,
        "event": event,
        "actor": EVENT_ACTORS[event],
        "detail": detail,
        "at": "2026-08-19T12:00:00+00:00",
    }


def test_manual_submission_does_not_require_prior_approval(tmp_path: Path) -> None:
    postings_dir, decisions_dir, track_dir = setup_data(tmp_path)
    write_jsonl(track_dir / "2026-08-19.jsonl", [stored_event("p:1", "fill", "form")])

    record_event(
            "submit",
            "p:1",
            "confirmation",
            postings_dir=postings_dir,
            decisions_dir=decisions_dir,
            track_dir=track_dir,
    )

    assert [event["event"] for event in load_events(track_dir)] == ["fill", "submit"]


def test_record_event_appends_fixed_actor_and_complete_happy_path(tmp_path: Path) -> None:
    postings_dir, decisions_dir, track_dir = setup_data(tmp_path)
    calls = [
        ("approve", None),
        ("fill", "prepared application"),
        ("submit", "confirmation 123"),
        ("outcome", "interview"),
        ("outcome", "offer"),
    ]

    for event, detail in calls:
        recorded = record_event(
            event,
            "p:1",
            detail,
            postings_dir=postings_dir,
            decisions_dir=decisions_dir,
            track_dir=track_dir,
        )
        assert recorded["actor"] == EVENT_ACTORS[event]

    events = load_events(track_dir)
    assert [event["event"] for event in events] == [item[0] for item in calls]
    assert events[-1]["detail"] == "offer"


@pytest.mark.parametrize(
    ("event", "detail", "message"),
    [
        ("fill", None, "required"),
        ("submit", "  ", "required"),
        ("approve", "extra", "not allowed"),
        ("outcome", "pending", "one of"),
    ],
)
def test_record_event_validates_detail(
    tmp_path: Path,
    event: str,
    detail: str | None,
    message: str,
) -> None:
    postings_dir, decisions_dir, track_dir = setup_data(tmp_path)

    with pytest.raises(ValueError, match=message):
        record_event(
            event,
            "p:1",
            detail,
            postings_dir=postings_dir,
            decisions_dir=decisions_dir,
            track_dir=track_dir,
        )


def test_record_event_rejects_unknown_posting(tmp_path: Path) -> None:
    postings_dir, decisions_dir, track_dir = setup_data(tmp_path)

    with pytest.raises(ValueError, match="unknown posting_key"):
        record_event(
            "approve",
            "missing",
            postings_dir=postings_dir,
            decisions_dir=decisions_dir,
            track_dir=track_dir,
        )
