from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def run_module(
    module: str, *args: object, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, *(str(arg) for arg in args)],
        check=False,
        capture_output=True,
        text=True,
        cwd=None if cwd is None else str(cwd),
    )


def setup_data(tmp_path: Path) -> tuple[Path, Path, Path]:
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    track_dir = tmp_path / "track"
    write_jsonl(postings_dir / "2026-08-19.jsonl", [{"key": "p:queued"}, {"key": "p:other"}])
    write_jsonl(
        decisions_dir / "2026-08-19.jsonl",
        [
            {
                "posting_key": "p:queued",
                "stage": "llm_score",
                "verdict": "queue",
                "decided_at": "2026-08-19T11:00:00+00:00",
            },
            {
                "posting_key": "p:other",
                "stage": "llm_score",
                "verdict": "kill",
                "decided_at": "2026-08-19T11:01:00+00:00",
            },
        ],
    )
    return postings_dir, decisions_dir, track_dir


def test_record_and_status_clis_use_explicit_tmp_directories(tmp_path: Path) -> None:
    postings_dir, decisions_dir, track_dir = setup_data(tmp_path)

    recorded = run_module(
        "venator.track.record", "--profile", "example",
        "approve",
        "p:queued",
        "--postings-dir",
        postings_dir,
        "--decisions-dir",
        decisions_dir,
        "--track-dir",
        track_dir,
    )
    assert recorded.returncode == 0, recorded.stderr
    assert recorded.stdout.strip() == "recorded approve for p:queued"

    status = run_module(
        "venator.track.status", "--profile", "example",
        "--postings-dir",
        postings_dir,
        "--decisions-dir",
        decisions_dir,
        "--track-dir",
        track_dir,
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout) == [
        {
            "posting_key": "p:queued",
            "state": "approved",
            "detail": None,
            "since": json.loads(next(track_dir.glob("*.jsonl")).read_text())["at"],
        }
    ]


def test_record_cli_accepts_assistance_without_prior_approval(tmp_path: Path) -> None:
    postings_dir, decisions_dir, track_dir = setup_data(tmp_path)

    result = run_module(
        "venator.track.record", "--profile", "example",
        "fill",
        "p:queued",
        "--detail",
        "application",
        "--postings-dir",
        postings_dir,
        "--decisions-dir",
        decisions_dir,
        "--track-dir",
        track_dir,
    )

    assert result.returncode == 0
    assert "recorded fill" in result.stdout
    assert track_dir.exists()


def profile_at(root: Path, name: str, identifier: str) -> str:
    """A minimal Profile under ``<root>/profiles/``, selectable by ``--profile``.

    The commands run with ``root`` as their working directory, because
    ``--profile`` names a directory under ``profiles/`` and ``--profile-dir``
    refuses a Profile outside the checkout (ADR-0002, as the code still has it).
    """
    directory = root / "profiles" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "targeting.yaml").write_text(
        f"profile:\n  name: {name}\n  id: {identifier}\n", encoding="utf-8"
    )
    return name


def test_both_track_clis_take_a_profile_like_every_other_stage(tmp_path: Path) -> None:
    """CLAUDE.md and docs/RUNNING.md say *every* stage takes ``--profile``.

    These two did not, which left a person having to know which stages are
    special. The flag is not decoration: the Profile it resolves is what the
    decisions store is checked against below.
    """
    postings_dir, decisions_dir, track_dir = setup_data(tmp_path)
    profile = profile_at(tmp_path, "ada", "ada-identifier")

    recorded = run_module(
        "venator.track.record",
        "approve",
        "p:queued",
        "--postings-dir",
        postings_dir,
        "--decisions-dir",
        decisions_dir,
        "--track-dir",
        track_dir,
        "--profile",
        profile,
        cwd=tmp_path,
    )
    assert recorded.returncode == 0, recorded.stderr

    status = run_module(
        "venator.track.status",
        "--postings-dir",
        postings_dir,
        "--decisions-dir",
        decisions_dir,
        "--track-dir",
        track_dir,
        "--profile",
        profile,
        cwd=tmp_path,
    )
    assert status.returncode == 0, status.stderr
    assert [row["state"] for row in json.loads(status.stdout)] == ["approved"]


def test_the_track_clis_refuse_another_profiles_decisions(tmp_path: Path) -> None:
    """The resolved Profile locates the store rather than being accepted and ignored."""
    postings_dir, decisions_dir, track_dir = setup_data(tmp_path)
    (decisions_dir / ".profile").write_text("ada-identifier\n", encoding="utf-8")
    grace = profile_at(tmp_path, "grace", "grace-identifier")

    recorded = run_module(
        "venator.track.record",
        "approve",
        "p:queued",
        "--postings-dir",
        postings_dir,
        "--decisions-dir",
        decisions_dir,
        "--track-dir",
        track_dir,
        "--profile",
        grace,
        cwd=tmp_path,
    )
    status = run_module(
        "venator.track.status",
        "--postings-dir",
        postings_dir,
        "--decisions-dir",
        decisions_dir,
        "--track-dir",
        track_dir,
        "--profile",
        grace,
        cwd=tmp_path,
    )

    for result in (recorded, status):
        assert result.returncode == 2
        assert "ada-identifier" in result.stderr
        assert "grace-identifier" in result.stderr
    assert not track_dir.exists()


def test_the_track_clis_refuse_a_second_profiles_track_store(tmp_path: Path) -> None:
    """The reported fail-open, at the CLI: two Profiles, one ``--track-dir``.

    Their decisions stores are separate and each correctly claimed, which is
    what makes this the interesting case — the decisions guard is doing its job
    and the interleave happens anyway, because ``fold_states`` keys a lifecycle
    state by Posting alone.

    The refusal is checked by *counting rows* before and after rather than by
    trusting the exit code: a guard that refuses after appending is no guard.
    """
    postings_dir, ada_decisions, track_dir = setup_data(tmp_path)
    grace_decisions = tmp_path / "decisions-grace"
    grace_decisions.mkdir()
    for line in (ada_decisions / "2026-08-19.jsonl").read_text(encoding="utf-8").splitlines():
        (grace_decisions / "2026-08-19.jsonl").open("a", encoding="utf-8").write(line + "\n")
    ada = profile_at(tmp_path, "ada", "ada-identifier")
    grace = profile_at(tmp_path, "grace", "grace-identifier")
    (ada_decisions / ".profile").write_text("ada-identifier\n", encoding="utf-8")
    (grace_decisions / ".profile").write_text("grace-identifier\n", encoding="utf-8")

    approved = run_module(
        "venator.track.record", "approve", "p:queued",
        "--postings-dir", postings_dir, "--decisions-dir", ada_decisions,
        "--track-dir", track_dir, "--profile", ada, cwd=tmp_path,
    )
    assert approved.returncode == 0, approved.stderr
    stored_before = sorted(path.read_bytes() for path in track_dir.glob("*.jsonl"))
    assert sum(len(value.splitlines()) for value in stored_before) == 1

    rejected = run_module(
        "venator.track.record", "reject", "p:queued",
        "--postings-dir", postings_dir, "--decisions-dir", grace_decisions,
        "--track-dir", track_dir, "--profile", grace, cwd=tmp_path,
    )
    status = run_module(
        "venator.track.status",
        "--postings-dir", postings_dir, "--decisions-dir", grace_decisions,
        "--track-dir", track_dir, "--profile", grace, cwd=tmp_path,
    )

    for result in (rejected, status):
        assert result.returncode == 2
        assert "ada-identifier" in result.stderr and "grace-identifier" in result.stderr
        assert "--track-dir" in result.stderr
    assert sorted(path.read_bytes() for path in track_dir.glob("*.jsonl")) == stored_before

    ada_status = run_module(
        "venator.track.status",
        "--postings-dir", postings_dir, "--decisions-dir", ada_decisions,
        "--track-dir", track_dir, "--profile", ada, cwd=tmp_path,
    )
    assert ada_status.returncode == 0, ada_status.stderr
    assert [row["state"] for row in json.loads(ada_status.stdout)] == ["approved"]


def test_explicit_store_directories_still_work(tmp_path: Path) -> None:
    """The legacy shape — explicit ``--decisions-dir``/``--track-dir``, no
    ``--profile`` — still records and still reports.

    The implied Profile of the checkout is what those runs are, so the store
    they write is claimed for it; what must not happen is a refusal. An absent
    flag configures nothing, and never turns a working invocation into an
    error.
    """
    postings_dir, decisions_dir, track_dir = setup_data(tmp_path)

    recorded = run_module(
        "venator.track.record", "--profile", "example", "approve", "p:queued",
        "--postings-dir", postings_dir, "--decisions-dir", decisions_dir,
        "--track-dir", track_dir,
    )
    status = run_module(
        "venator.track.status", "--profile", "example",
        "--postings-dir", postings_dir, "--decisions-dir", decisions_dir,
        "--track-dir", track_dir,
    )

    assert recorded.returncode == 0, recorded.stderr
    assert status.returncode == 0, status.stderr
    assert [row["state"] for row in json.loads(status.stdout)] == ["approved"]


def test_a_stamp_that_is_a_directory_is_reported_not_traced_back(tmp_path: Path) -> None:
    """A ``.profile`` that is a directory raises ``OSError``, which every CLI
    seam that reads a stamp now catches alongside ``ValueError``."""
    postings_dir, decisions_dir, track_dir = setup_data(tmp_path)
    profile = profile_at(tmp_path, "ada", "ada-identifier")
    track_dir.mkdir(parents=True, exist_ok=True)
    (track_dir / ".profile").mkdir()

    for module, extra in (
        ("venator.track.record", ("approve", "p:queued")),
        ("venator.track.status", ()),
    ):
        result = run_module(
            module, *extra,
            "--postings-dir", postings_dir, "--decisions-dir", decisions_dir,
            "--track-dir", track_dir, "--profile", profile, cwd=tmp_path,
        )
        assert result.returncode == 2, result.stdout
        assert "Traceback" not in result.stderr
