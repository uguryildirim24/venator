"""Reading one Profile's Filter Decisions as another Profile's is refused.

The write path has claimed a decisions store since ADR-0002 made one directory
belong to one Profile. The read paths did not, and that asymmetry made the wrong
answer the silent one: a read pointed at another Profile's store answered
happily — measured on a real Install, 34 Postings where the owning Profile's
own answer was 29 — because nothing on the way in compared the two.

Every test here goes through the same mechanism the write path uses: the
identifier recorded in ``data/decisions/.profile``. Two properties that follow
from reusing it, and that these tests pin because the alternative fails open or
fails a whole existing corpus:

* An **unclaimed** store is unknown, not a mismatch — every store written before
  the claim existed stays readable (ADR-0001; ``data/`` is append-only).
* A **read never claims**. Reading is not owning, and a read-only command that
  stamped a directory would decide whose store it is as a side effect.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from venator.match.store import DECISIONS_OWNER_FILE, verify_decisions_dir
from venator.profile import Profile
from venator.track.record import record_event
from venator.track.status import status_rows
from venator.view.build import build_database

ADA = "6f1c9d2b4a8e47f0b3c5d7e9a1b2c3d4"
GRACE = "9f8e7d6c5b4a392817065f4e3d2c1b0a"


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def claimed_store(tmp_path: Path, identifier: str) -> tuple[Path, Path]:
    """One Profile's Postings and its claimed decisions store, one Posting queued."""
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    write_jsonl(postings_dir / "2026-08-19.jsonl", [{"key": "p:1", "title": "One"}])
    write_jsonl(
        decisions_dir / "2026-08-19.jsonl",
        [
            {
                "posting_key": "p:1",
                "stage": "hard_filter",
                "verdict": "pass",
                "rule": None,
                "profile_id": identifier,
                "filters_version": "abc123abc123",
                "decided_at": "2026-08-19T11:00:00+00:00",
            },
            {
                "posting_key": "p:1",
                "stage": "llm_score",
                "verdict": "queue",
                "rule": None,
                "score": 80,
                "profile_id": identifier,
                "filters_version": "abc123abc123",
                "decided_at": "2026-08-19T11:01:00+00:00",
            },
        ],
    )
    (decisions_dir / DECISIONS_OWNER_FILE).write_text(identifier + "\n", encoding="utf-8")
    return postings_dir, decisions_dir


def test_verify_names_both_profiles_and_refuses(tmp_path: Path) -> None:
    _, decisions_dir = claimed_store(tmp_path, ADA)

    with pytest.raises(ValueError) as error:
        verify_decisions_dir(decisions_dir, GRACE)

    message = str(error.value)
    assert ADA in message and GRACE in message
    assert "--decisions-dir" in message


def test_track_status_refuses_another_profiles_review_queue(tmp_path: Path) -> None:
    """A lifecycle state folds TrackEvents over a *queue verdict* from decisions."""
    postings_dir, decisions_dir = claimed_store(tmp_path, ADA)
    track_dir = tmp_path / "track"

    rows = status_rows(postings_dir, decisions_dir, track_dir, identifier=ADA)
    assert [row["state"] for row in rows] == ["queued"]

    with pytest.raises(ValueError) as error:
        status_rows(postings_dir, decisions_dir, track_dir, identifier=GRACE)

    assert ADA in str(error.value) and GRACE in str(error.value)


def test_track_record_refuses_before_it_appends_anything(tmp_path: Path) -> None:
    """A command pointed at another Profile's decisions store is refused before
    it writes anything into this Profile's Track store."""
    postings_dir, decisions_dir = claimed_store(tmp_path, ADA)
    track_dir = tmp_path / "track"

    with pytest.raises(ValueError) as error:
        record_event(
            "approve",
            "p:1",
            postings_dir=postings_dir,
            decisions_dir=decisions_dir,
            track_dir=track_dir,
            identifier=GRACE,
        )

    assert ADA in str(error.value) and GRACE in str(error.value)
    assert not track_dir.exists()

    event = record_event(
        "approve",
        "p:1",
        postings_dir=postings_dir,
        decisions_dir=decisions_dir,
        track_dir=track_dir,
        identifier=ADA,
    )
    assert event["event"] == "approve"


def test_view_build_refuses_to_materialize_another_profiles_decisions(tmp_path: Path) -> None:
    """The dashboard shows no Profile at all, so this is the least visible read."""
    postings_dir, decisions_dir = claimed_store(tmp_path, ADA)
    database = tmp_path / "build" / "venator.db"

    with pytest.raises(ValueError) as error:
        build_database(
            postings_dir,
            decisions_dir,
            database,
            tmp_path / "track",
            tmp_path / "runs.jsonl",
            profile=Profile(name="grace", directory=tmp_path / "grace", id=GRACE),
        )

    assert ADA in str(error.value) and GRACE in str(error.value)
    assert not database.exists()


def test_an_unclaimed_store_is_unknown_and_every_read_proceeds(tmp_path: Path) -> None:
    """The whole existing corpus predates the claim; absence is never a mismatch."""
    postings_dir, decisions_dir = claimed_store(tmp_path, ADA)
    (decisions_dir / DECISIONS_OWNER_FILE).unlink()

    assert status_rows(postings_dir, decisions_dir, tmp_path / "track", identifier=GRACE)


def test_a_read_never_claims_an_unstamped_store(tmp_path: Path) -> None:
    """Reading is not owning: a read-only command must not decide the owner."""
    postings_dir, decisions_dir = claimed_store(tmp_path, ADA)
    (decisions_dir / DECISIONS_OWNER_FILE).unlink()

    status_rows(postings_dir, decisions_dir, tmp_path / "track", identifier=GRACE)
    verify_decisions_dir(decisions_dir, GRACE)

    assert not (decisions_dir / DECISIONS_OWNER_FILE).exists()


def test_a_stray_row_is_reported_on_a_read_and_never_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``profile_id`` stays evidence on the read path too, never a second lock.

    ``coordination/CONTRACTS.md`` is explicit that the claim is the enforcement
    and the row field is not: two locks that can disagree are worse than one
    that cannot, and a store restored from a backup must stay readable rather
    than becoming unreadable.
    """
    postings_dir, decisions_dir = claimed_store(tmp_path, ADA)
    write_jsonl(
        decisions_dir / "2026-08-20.jsonl",
        [
            {
                "posting_key": "p:1",
                "stage": "hard_filter",
                "verdict": "pass",
                "rule": None,
                "profile_id": GRACE,
                "filters_version": "abc123abc123",
                "decided_at": "2026-08-20T11:00:00+00:00",
            }
        ],
    )

    status_rows(postings_dir, decisions_dir, tmp_path / "track", identifier=ADA)

    warning = capsys.readouterr().err
    assert "WARNING" in warning
    assert f"1 by Profile '{GRACE}'" in warning
    assert "No row has been dropped or changed" in warning


def test_an_unclaimed_store_holding_a_stray_row_is_not_described_as_claimed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The warning must state what the stamp says, not what would be tidy.

    The unclaimed store is the case most worth hearing about — nothing on disk
    says whose it is — and it is exactly the case the first version of this
    warning described as "claimed by Profile 'X'", which is the opposite of the
    truth told to the one person in a position to check it.
    """
    postings_dir, decisions_dir = claimed_store(tmp_path, ADA)
    (decisions_dir / DECISIONS_OWNER_FILE).unlink()
    write_jsonl(
        decisions_dir / "2026-08-20.jsonl",
        [
            {
                "posting_key": "p:1",
                "stage": "hard_filter",
                "verdict": "pass",
                "rule": None,
                "profile_id": GRACE,
                "filters_version": "abc123abc123",
                "decided_at": "2026-08-20T11:00:00+00:00",
            }
        ],
    )

    status_rows(postings_dir, decisions_dir, tmp_path / "track", identifier=ADA)

    warning = capsys.readouterr().err
    assert "is claimed by no Profile" in warning
    assert f"is claimed by Profile {ADA!r}" not in warning
    assert f"1 by Profile {GRACE!r}" in warning
    assert "--decisions-dir" in warning


def test_a_stamp_that_is_a_directory_is_reported_not_traced_back(tmp_path: Path) -> None:
    """``.profile`` is a file, but nothing stops it being a directory, and a
    read of one raises ``OSError`` rather than ``ValueError``. Every CLI seam
    that reads a stamp catches both, so the answer is an error message."""
    import subprocess
    import sys

    postings_dir, decisions_dir = claimed_store(tmp_path, ADA)
    (decisions_dir / DECISIONS_OWNER_FILE).unlink()
    (decisions_dir / DECISIONS_OWNER_FILE).mkdir()
    profile_dir = tmp_path / "profiles" / "ada"
    profile_dir.mkdir(parents=True)
    (profile_dir / "targeting.yaml").write_text(
        f"profile:\n  name: ada\n  id: {ADA}\n", encoding="utf-8"
    )

    for module in ("venator.track.status", "venator.match.run"):
        result = subprocess.run(
            [
                sys.executable, "-m", module,
                "--postings-dir", str(postings_dir),
                "--decisions-dir", str(decisions_dir),
                "--profile", "ada",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert result.returncode == 2, result.stdout
        assert "Traceback" not in result.stderr


def test_a_claimed_store_still_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the sentence, so the unclaimed wording did not eat it.

    ``report_stray_rows`` picks one of two sentences off what the stamp says.
    A test for the unclaimed one alone would pass just as well against a
    version that had lost the claimed one entirely, and the claimed store is
    the case an Owner is most likely to meet.
    """
    postings_dir, decisions_dir = claimed_store(tmp_path, ADA)
    write_jsonl(
        decisions_dir / "2026-08-20.jsonl",
        [
            {
                "posting_key": "p:1",
                "stage": "hard_filter",
                "verdict": "pass",
                "rule": None,
                "profile_id": GRACE,
                "filters_version": "abc123abc123",
                "decided_at": "2026-08-20T11:00:00+00:00",
            }
        ],
    )

    verify_decisions_dir(decisions_dir, ADA)

    warning = capsys.readouterr().err
    assert f"is claimed by Profile {ADA!r}" in warning
    assert "is claimed by no Profile" not in warning
