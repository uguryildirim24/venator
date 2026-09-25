from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from venator.view.build import build_database


@pytest.fixture
def store_paths(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "postings": tmp_path / "postings",
        "decisions": tmp_path / "decisions",
        "track": tmp_path / "track",
        "runs": tmp_path / "runs.jsonl",
        "database": tmp_path / "build" / "venator.db",
    }
    paths["postings"].mkdir()
    paths["decisions"].mkdir()
    return paths


def write_jsonl(path: Path, *rows: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows))


def build(paths: dict[str, Path]) -> tuple[int, int]:
    return build_database(
        paths["postings"],
        paths["decisions"],
        paths["database"],
        paths["track"],
        paths["runs"],
    )


def score(
    posting_key: str,
    *,
    verdict: str = "queue",
    decided_at: str = "2026-08-18T20:00:00+00:00",
) -> dict:
    return {
        "posting_key": posting_key,
        "stage": "llm_score",
        "verdict": verdict,
        "rule": None,
        "score": 90 if verdict == "queue" else 50,
        "reason": "fixture score",
        "filters_version": "abcdef123456",
        "decided_at": decided_at,
    }


def event(
    posting_key: str,
    event_name: str,
    at: str,
    *,
    actor: str = "owner",
    detail: str | None = None,
) -> dict:
    return {
        "posting_key": posting_key,
        "event": event_name,
        "actor": actor,
        "detail": detail,
        "at": at,
    }


def test_derived_queued_row_uses_latest_score(store_paths: dict[str, Path]) -> None:
    posting_key = "greenhouse:example:1"
    write_jsonl(
        store_paths["decisions"] / "2026-08-18.jsonl",
        score(posting_key, verdict="kill", decided_at="2026-08-18T19:00:00+00:00"),
        score(posting_key, decided_at="2026-08-18T20:00:00+00:00"),
    )

    build(store_paths)

    with sqlite3.connect(store_paths["database"]) as database:
        row = database.execute(
            "SELECT posting_key, state, detail, since FROM application_states"
        ).fetchone()
    assert row == (posting_key, "queued", None, "2026-08-18T20:00:00+00:00")


def test_approve_event_overrides_derived_queue(store_paths: dict[str, Path]) -> None:
    posting_key = "greenhouse:example:2"
    approval = event(posting_key, "approve", "2026-08-19T00:01:00+00:00")
    write_jsonl(store_paths["decisions"] / "2026-08-18.jsonl", score(posting_key))
    write_jsonl(store_paths["track"] / "2026-08-19.jsonl", approval)

    build(store_paths)

    with sqlite3.connect(store_paths["database"]) as database:
        state = database.execute(
            "SELECT posting_key, state, detail, since FROM application_states"
        ).fetchone()
        stored_event = database.execute(
            "SELECT id, posting_key, event, actor, detail, at FROM track_events"
        ).fetchone()
    assert state == (posting_key, "approved", None, approval["at"])
    assert stored_event == (1, posting_key, "approve", "owner", None, approval["at"])


def test_rerecorded_outcome_keeps_latest_effective_state(
    store_paths: dict[str, Path],
) -> None:
    posting_key = "ashby:example:3"
    events = (
        event(posting_key, "approve", "2026-08-19T00:00:00+00:00"),
        event(
            posting_key,
            "fill",
            "2026-08-19T00:01:00+00:00",
            actor="pipeline",
            detail="applications/example-3",
        ),
        event(
            posting_key,
            "submit",
            "2026-08-19T00:02:00+00:00",
            actor="pipeline",
            detail="confirmation-123",
        ),
        event(
            posting_key,
            "outcome",
            "2026-08-20T00:00:00+00:00",
            detail="interview",
        ),
        event(
            posting_key,
            "outcome",
            "2026-08-21T00:00:00+00:00",
            detail="offer",
        ),
    )
    write_jsonl(store_paths["decisions"] / "2026-08-18.jsonl", score(posting_key))
    write_jsonl(store_paths["track"] / "2026-08-19.jsonl", *events[:3])
    write_jsonl(store_paths["track"] / "2026-08-20.jsonl", events[3])
    write_jsonl(store_paths["track"] / "2026-08-21.jsonl", events[4])

    build(store_paths)

    with sqlite3.connect(store_paths["database"]) as database:
        states = database.execute(
            "SELECT posting_key, state, detail, since FROM application_states"
        ).fetchall()
        stored_events = database.execute(
            "SELECT event, detail FROM track_events ORDER BY id"
        ).fetchall()
    assert states == [(posting_key, "concluded", "offer", events[4]["at"])]
    assert stored_events[-2:] == [("outcome", "interview"), ("outcome", "offer")]


def test_runs_rows_land_in_append_order(store_paths: dict[str, Path]) -> None:
    first = {
        "at": "2026-08-18T20:47:36+00:00",
        "status": "ok",
    }
    second = {
        "at": "2026-08-18T23:25:17+00:00",
        "status": "ok",
    }
    write_jsonl(store_paths["runs"], first, second)

    build(store_paths)

    with sqlite3.connect(store_paths["database"]) as database:
        rows = database.execute(
            "SELECT id, at, status FROM runs ORDER BY id"
        ).fetchall()
    assert rows == [
        (1, first["at"], "ok"),
        (2, second["at"], "ok"),
    ]


def test_missing_track_dir_and_runs_file_build_cleanly(
    store_paths: dict[str, Path],
) -> None:
    assert not store_paths["track"].exists()
    assert not store_paths["runs"].exists()

    assert build(store_paths) == (0, 0)

    with sqlite3.connect(store_paths["database"]) as database:
        assert database.execute("SELECT COUNT(*) FROM track_events").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM application_states").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_rebuild_is_idempotent(store_paths: dict[str, Path]) -> None:
    posting_key = "greenhouse:example:4"
    run = {
        "at": "2026-08-18T23:25:17+00:00",
        "status": "ok",
    }
    write_jsonl(store_paths["decisions"] / "2026-08-18.jsonl", score(posting_key))
    write_jsonl(store_paths["runs"], run)

    assert build(store_paths) == (0, 1)
    assert build(store_paths) == (0, 1)

    with sqlite3.connect(store_paths["database"]) as database:
        assert database.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM application_states").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert database.execute("SELECT id FROM runs").fetchone()[0] == 1


def test_existing_table_rows_and_schemas_are_unchanged(
    store_paths: dict[str, Path],
) -> None:
    posting = {
        "key": "greenhouse:fixture-board:5",
        "source": "greenhouse",
        "board": "fixture-board",
        "company": "Fixture Bio",
        "title": "Research Associate",
        "location": "Boston, MA",
        "url": "https://example.test/5",
        "posted_at": "2026-08-17T00:00:00+00:00",
        "discovered_at": "2026-08-18T00:00:00+00:00",
        "description_html": "<p>Fixture</p>",
    }
    decision = score(posting["key"])
    write_jsonl(store_paths["postings"] / "2026-08-18.jsonl", posting)
    write_jsonl(store_paths["decisions"] / "2026-08-18.jsonl", decision)

    build(store_paths)

    with sqlite3.connect(store_paths["database"]) as database:
        posting_columns = [
            row[1] for row in database.execute("PRAGMA table_info(postings)")
        ]
        decision_columns = [
            row[1] for row in database.execute("PRAGMA table_info(decisions)")
        ]
        posting_bytes = json.dumps(
            database.execute("SELECT * FROM postings ORDER BY key").fetchall(),
            separators=(",", ":"),
        ).encode()
        decision_bytes = json.dumps(
            database.execute("SELECT * FROM decisions ORDER BY id").fetchall(),
            separators=(",", ":"),
        ).encode()

    assert posting_columns == [
        "key",
        "source",
        "board",
        "company",
        "title",
        "location",
        "url",
        "posted_at",
        "discovered_at",
        "description_html",
    ]
    assert decision_columns == [
        "id",
        "posting_key",
        "stage",
        "verdict",
        "rule",
        "score",
        "reason",
        "filters_version",
        "decided_at",
    ]
    assert posting_bytes == json.dumps(
        [[
            posting["key"],
            posting["source"],
            posting["board"],
            posting["company"],
            posting["title"],
            posting["location"],
            posting["url"],
            posting["posted_at"],
            posting["discovered_at"],
            posting["description_html"],
        ]],
        separators=(",", ":"),
    ).encode()
    assert decision_bytes == json.dumps(
        [[
            1,
            decision["posting_key"],
            decision["stage"],
            decision["verdict"],
            decision["rule"],
            decision["score"],
            decision["reason"],
            decision["filters_version"],
            decision["decided_at"],
        ]],
        separators=(",", ":"),
    ).encode()


def hard_filter(
    posting_key: str,
    *,
    verdict: str = "kill",
    rule: str | None = "duplicate",
    decided_at: str = "2026-08-26T10:00:00+00:00",
) -> dict:
    return {
        "posting_key": posting_key,
        "stage": "hard_filter",
        "verdict": verdict,
        "rule": rule,
        "reason": "fixture hard filter",
        "filters_version": "abcdef123456",
        "decided_at": decided_at,
    }


def test_a_duplicate_killed_after_it_was_scored_is_no_longer_queued(
    store_paths: dict[str, Path],
) -> None:
    """The view has to fold the stores the same way ``track.store`` does.

    Both derive a queue entry from the latest ``llm_score``, and both have to
    let the latest ``hard_filter`` decision veto it — otherwise the dashboard's
    Review Queue and ``venator.track.status`` disagree about the same Posting,
    and the disagreement is invisible. The dashboard's status column already
    reads this way (``ui/server/queries.ts`` tests ``hf.verdict = 'kill'``
    before it ever looks at the score).
    """
    killed = "adzuna:us:duplicate"
    live = "adzuna:us:survivor"
    write_jsonl(
        store_paths["decisions"] / "2026-08-18.jsonl",
        hard_filter(killed, verdict="pass", rule=None, decided_at="2026-08-18T19:00:00+00:00"),
        hard_filter(live, verdict="pass", rule=None, decided_at="2026-08-18T19:00:00+00:00"),
        score(killed),
        score(live),
        hard_filter(killed),
    )

    build(store_paths)

    with sqlite3.connect(store_paths["database"]) as database:
        rows = database.execute(
            "SELECT posting_key, state FROM application_states ORDER BY posting_key"
        ).fetchall()
    assert rows == [(live, "queued")]


def test_runs_carry_the_stage_that_wrote_each_heartbeat(
    store_paths: dict[str, Path],
) -> None:
    """`venator.schedule.loop` names its stage; the view must not drop the name.

    The loop appends a heartbeat per stage and always finishes on `commit`.
    A consumer that takes the newest row by `at` therefore needs the discriminator;
    the view carries it and does not pick a winner here.
    """
    discover = {"at": "2026-08-19T01:00:00+00:00", "status": "ok", "stage": "discover"}
    view = {"at": "2026-08-19T01:04:00+00:00", "status": "error", "stage": "view"}
    commit = {"at": "2026-08-19T01:05:00+00:00", "status": "ok", "stage": "commit"}
    write_jsonl(store_paths["runs"], discover, view, commit)

    build(store_paths)

    with sqlite3.connect(store_paths["database"]) as database:
        rows = database.execute(
            "SELECT status, stage FROM runs ORDER BY id"
        ).fetchall()
    assert rows == [("ok", "discover"), ("error", "view"), ("ok", "commit")]
