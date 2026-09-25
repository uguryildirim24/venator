"""Two Profiles never share one Track store.

The decisions store has refused a second Profile since ADR-0002 made one
directory belong to one Profile, and the read paths joined it later. The Track
store had neither: ``data/track/`` carried no claim and no stamp, and
``fold_states`` keys a lifecycle state by ``posting_key`` **alone**. So two
Profiles whose decisions stores were correctly separated *and* correctly
claimed still interleaved the moment they shared a ``--track-dir``, and the
last Owner to act decided the other Owner's Posting's state — reproduced end to
end in ``test_the_reported_reproduction_is_refused_at_the_second_profile``.

Every test here goes through the mechanism ``match.store`` already uses, now
shared as ``venator.profile.claim``: the Profile identifier recorded in a
store's ``.profile``. The properties are the ones that review pinned for the
decisions store, checked one at a time because none of them follows from
another.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.profile import Profile
from venator.track.record import record_event
from venator.track.status import status_rows
from venator.track.store import (
    TRACK_OWNER_FILE,
    claim_track_dir,
    load_events,
    verify_track_dir,
)
from venator.view.build import build_database

ADA = "6f1c9d2b4a8e47f0b3c5d7e9a1b2c3d4"
GRACE = "9f8e7d6c5b4a392817065f4e3d2c1b0a"


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def decisions_for(directory: Path, identifier: str) -> Path:
    """One Posting, passed and queued, in a store claimed by ``identifier``."""
    write_jsonl(
        directory / "2026-08-25.jsonl",
        [
            {
                "posting_key": "p:1",
                "stage": "hard_filter",
                "verdict": "pass",
                "rule": None,
                "profile_id": identifier,
                "filters_version": "abc123abc123",
                "decided_at": "2026-08-25T11:00:00+00:00",
            },
            {
                "posting_key": "p:1",
                "stage": "llm_score",
                "verdict": "queue",
                "rule": None,
                "score": 80,
                "profile_id": identifier,
                "filters_version": "abc123abc123",
                "decided_at": "2026-08-25T11:01:00+00:00",
            },
        ],
    )
    (directory / TRACK_OWNER_FILE).write_text(identifier + "\n", encoding="utf-8")
    return directory


def two_profiles(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Postings, one shared Track store, and a correct decisions store each."""
    postings_dir = tmp_path / "postings"
    write_jsonl(postings_dir / "2026-08-25.jsonl", [{"key": "p:1", "title": "One"}])
    return (
        postings_dir,
        decisions_for(tmp_path / "dec-ada", ADA),
        decisions_for(tmp_path / "dec-grace", GRACE),
        tmp_path / "track",
    )


def track_events(track_dir: Path) -> list[dict]:
    return [json.loads(line) for path in sorted(track_dir.glob("*.jsonl")) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_reported_reproduction_is_refused_at_the_second_profile(tmp_path: Path) -> None:
    """The defect, end to end: Ada's Posting read ``rejected`` because Grace
    rejected hers. Both decisions stores are separate and claimed; only the
    Track store is shared, and that was enough."""
    postings_dir, ada_decisions, grace_decisions, track_dir = two_profiles(tmp_path)

    record_event(
        "approve",
        "p:1",
        postings_dir=postings_dir,
        decisions_dir=ada_decisions,
        track_dir=track_dir,
        identifier=ADA,
    )

    with pytest.raises(ValueError) as error:
        record_event(
            "reject",
            "p:1",
            postings_dir=postings_dir,
            decisions_dir=grace_decisions,
            track_dir=track_dir,
            identifier=GRACE,
        )

    assert ADA in str(error.value) and GRACE in str(error.value)
    assert "--track-dir" in str(error.value)
    assert [event["event"] for event in track_events(track_dir)] == ["approve"]
    rows = status_rows(postings_dir, ada_decisions, track_dir, identifier=ADA)
    assert [row["state"] for row in rows] == ["approved"]


def test_record_refuses_before_it_appends_anything(tmp_path: Path) -> None:
    """Counted, not inferred from the exit code: the store is byte-identical."""
    postings_dir, ada_decisions, grace_decisions, track_dir = two_profiles(tmp_path)
    record_event(
        "approve",
        "p:1",
        postings_dir=postings_dir,
        decisions_dir=ada_decisions,
        track_dir=track_dir,
        identifier=ADA,
    )
    # The day file the append actually created, read off the store rather than
    # spelled out. `append_events` dates it `date.today()`, so a literal here is
    # only true on the day it was written — this line said "2026-08-25.jsonl"
    # and went red at 00:04 UTC the next morning, on main as well as on any
    # branch. Discovering it also removes the midnight race a `date.today()`
    # here would still have had.
    before = next(iter(sorted(track_dir.glob("*.jsonl"))))
    before_rows = len(track_events(track_dir))
    before_bytes = sorted(
        (path.name, path.read_bytes()) for path in track_dir.iterdir() if path.is_file()
    )

    with pytest.raises(ValueError):
        record_event(
            "reject",
            "p:1",
            postings_dir=postings_dir,
            decisions_dir=grace_decisions,
            track_dir=track_dir,
            identifier=GRACE,
        )

    assert len(track_events(track_dir)) == before_rows == 1
    assert (
        sorted((path.name, path.read_bytes()) for path in track_dir.iterdir() if path.is_file())
        == before_bytes
    )
    assert before.exists()


def test_a_refused_record_does_not_create_or_stamp_an_absent_store(tmp_path: Path) -> None:
    """A refusal has no side effect at all, the claim included."""
    postings_dir, ada_decisions, _, track_dir = two_profiles(tmp_path)

    with pytest.raises(ValueError, match="unknown posting_key"):
        record_event(
            "approve",
            "p:404",
            postings_dir=postings_dir,
            decisions_dir=ada_decisions,
            track_dir=track_dir,
            identifier=ADA,
        )

    assert not track_dir.exists()


def test_status_refuses_another_profiles_lifecycle(tmp_path: Path) -> None:
    postings_dir, ada_decisions, grace_decisions, track_dir = two_profiles(tmp_path)
    record_event(
        "approve",
        "p:1",
        postings_dir=postings_dir,
        decisions_dir=ada_decisions,
        track_dir=track_dir,
        identifier=ADA,
    )

    with pytest.raises(ValueError) as error:
        status_rows(postings_dir, grace_decisions, track_dir, identifier=GRACE)

    assert ADA in str(error.value) and GRACE in str(error.value)


def test_view_build_refuses_to_materialize_another_profiles_lifecycle(tmp_path: Path) -> None:
    """The dashboard names no Profile, so this is the least visible read of all."""
    postings_dir, ada_decisions, grace_decisions, track_dir = two_profiles(tmp_path)
    record_event(
        "approve",
        "p:1",
        postings_dir=postings_dir,
        decisions_dir=ada_decisions,
        track_dir=track_dir,
        identifier=ADA,
    )
    database = tmp_path / "build" / "venator.db"

    with pytest.raises(ValueError) as error:
        build_database(
            postings_dir,
            grace_decisions,
            database,
            track_dir,
            tmp_path / "runs.jsonl",
            profile=Profile(name="grace", directory=tmp_path / "grace", id=GRACE),
        )

    assert ADA in str(error.value) and GRACE in str(error.value)
    assert not database.exists()


def test_a_read_never_claims_an_unstamped_store(tmp_path: Path) -> None:
    """Reading is not owning: a read-only command must not decide the owner."""
    postings_dir, ada_decisions, _, track_dir = two_profiles(tmp_path)
    write_jsonl(track_dir / "2026-08-25.jsonl", [])
    assert not (track_dir / TRACK_OWNER_FILE).exists()

    status_rows(postings_dir, ada_decisions, track_dir, identifier=ADA)
    verify_track_dir(track_dir, ADA)
    build_database(
        postings_dir,
        ada_decisions,
        tmp_path / "build" / "venator.db",
        track_dir,
        tmp_path / "runs.jsonl",
        profile=Profile(name="ada", directory=tmp_path / "ada", id=ADA),
    )

    assert not (track_dir / TRACK_OWNER_FILE).exists()


@pytest.mark.parametrize("stamp", ["", "   ", "\n", "\t \n"])
def test_an_empty_or_blank_stamp_is_unclaimed_and_never_a_mismatch(
    tmp_path: Path, stamp: str
) -> None:
    """A truncated write must not lock a store: blank means unclaimed."""
    postings_dir, ada_decisions, _, track_dir = two_profiles(tmp_path)
    track_dir.mkdir(parents=True, exist_ok=True)
    (track_dir / TRACK_OWNER_FILE).write_text(stamp, encoding="utf-8")

    verify_track_dir(track_dir, GRACE)
    assert status_rows(postings_dir, ada_decisions, track_dir, identifier=ADA) is not None


def test_an_absent_track_store_proceeds(tmp_path: Path) -> None:
    """Absent means "not configured", never "invalid" — a Profile with no Track
    history yet must not be refused a status."""
    postings_dir, ada_decisions, _, track_dir = two_profiles(tmp_path)
    assert not track_dir.exists()

    verify_track_dir(track_dir, GRACE)
    rows = status_rows(postings_dir, ada_decisions, track_dir, identifier=ADA)

    assert [row["state"] for row in rows] == ["queued"]
    assert not track_dir.exists()


def test_a_stamp_naming_a_profile_that_no_longer_exists_refuses_and_names_both(
    tmp_path: Path,
) -> None:
    """The identifier is opaque, so a deleted Profile is not a special case —
    but the message has to say whose store it is and who is asking, or nobody
    can act on it."""
    postings_dir, ada_decisions, _, track_dir = two_profiles(tmp_path)
    track_dir.mkdir(parents=True, exist_ok=True)
    (track_dir / TRACK_OWNER_FILE).write_text("a-profile-since-deleted\n", encoding="utf-8")

    with pytest.raises(ValueError) as error:
        status_rows(postings_dir, ada_decisions, track_dir, identifier=ADA)

    message = str(error.value)
    assert "a-profile-since-deleted" in message and ADA in message
    assert str(track_dir / TRACK_OWNER_FILE) in message


def test_a_stray_event_is_reported_and_never_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``profile_id`` on an event is evidence, not a second lock: the fold still
    reads it, and ``data/`` is append-only (ADR-0001)."""
    postings_dir, ada_decisions, _, track_dir = two_profiles(tmp_path)
    write_jsonl(
        track_dir / "2026-08-25.jsonl",
        [
            {
                "posting_key": "p:1",
                "event": "approve",
                "actor": "owner",
                "detail": None,
                "at": "2026-08-25T12:00:00+00:00",
                "profile_id": GRACE,
            }
        ],
    )
    (track_dir / TRACK_OWNER_FILE).write_text(ADA + "\n", encoding="utf-8")

    rows = status_rows(postings_dir, ada_decisions, track_dir, identifier=ADA)

    warning = capsys.readouterr().err
    assert "WARNING" in warning
    assert f"1 by Profile {GRACE!r}" in warning
    assert "No row has been dropped or changed" in warning
    assert "--track-dir" in warning
    assert [row["state"] for row in rows] == ["approved"]
    assert len(load_events(track_dir)) == 1


def test_an_unclaimed_store_holding_a_stray_event_is_not_described_as_claimed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The warning states what the stamp says, not what it would be convenient
    to assume. An unclaimed store is the case worth hearing about — nothing on
    disk says whose it is — and calling it claimed is the opposite of the
    truth."""
    postings_dir, ada_decisions, _, track_dir = two_profiles(tmp_path)
    write_jsonl(
        track_dir / "2026-08-25.jsonl",
        [
            {
                "posting_key": "p:1",
                "event": "approve",
                "actor": "owner",
                "detail": None,
                "at": "2026-08-25T12:00:00+00:00",
                "profile_id": GRACE,
            }
        ],
    )
    assert not (track_dir / TRACK_OWNER_FILE).exists()

    status_rows(postings_dir, ada_decisions, track_dir, identifier=ADA)

    warning = capsys.readouterr().err
    assert "is claimed by no Profile" in warning
    assert f"is claimed by Profile {ADA!r}" not in warning


def test_an_event_carries_the_profile_that_recorded_it_and_none_without_one(
    tmp_path: Path,
) -> None:
    """Evidence is written, and absence stays absence — never a placeholder."""
    postings_dir, ada_decisions, _, track_dir = two_profiles(tmp_path)

    with_profile = record_event(
        "approve",
        "p:1",
        postings_dir=postings_dir,
        decisions_dir=ada_decisions,
        track_dir=track_dir,
        identifier=ADA,
    )
    assert with_profile["profile_id"] == ADA

    without = record_event(
        "reject",
        "p:1",
        postings_dir=postings_dir,
        decisions_dir=ada_decisions,
        track_dir=tmp_path / "flags-only",
    )
    assert "profile_id" not in without
    assert load_events(tmp_path / "flags-only") == [without]


def test_a_stamp_that_is_a_directory_reports_rather_than_tracebacks(tmp_path: Path) -> None:
    """``OSError`` is caught alongside ``ValueError`` at every CLI seam that
    reads a stamp; here the store layer is simply allowed to raise it, so the
    seam has something to catch."""
    _, _, _, track_dir = two_profiles(tmp_path)
    (track_dir / TRACK_OWNER_FILE).mkdir(parents=True)

    with pytest.raises(OSError):
        verify_track_dir(track_dir, ADA)


def test_the_first_successful_record_claims_an_unclaimed_store(tmp_path: Path) -> None:
    """The write path stamps — and only the write path, and only on success."""
    postings_dir, ada_decisions, _, track_dir = two_profiles(tmp_path)
    assert not track_dir.exists()

    record_event(
        "approve",
        "p:1",
        postings_dir=postings_dir,
        decisions_dir=ada_decisions,
        track_dir=track_dir,
        identifier=ADA,
    )

    assert (track_dir / TRACK_OWNER_FILE).read_text(encoding="utf-8").strip() == ADA


def test_claiming_a_foreign_store_refuses_on_its_own(tmp_path: Path) -> None:
    """``claim_track_dir`` is safe standalone: it does not assume a caller
    verified first, and it never overwrites another Profile's stamp."""
    _, _, _, track_dir = two_profiles(tmp_path)
    track_dir.mkdir(parents=True, exist_ok=True)
    (track_dir / TRACK_OWNER_FILE).write_text(ADA + "\n", encoding="utf-8")

    with pytest.raises(ValueError) as error:
        claim_track_dir(track_dir, GRACE)

    assert ADA in str(error.value) and GRACE in str(error.value)
    assert (track_dir / TRACK_OWNER_FILE).read_text(encoding="utf-8").strip() == ADA
