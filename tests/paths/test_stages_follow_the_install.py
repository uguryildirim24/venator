"""Two working directories, one store — run as the stages actually run.

`tests/paths/test_store_paths.py` pins the rule; this file pins the thing the
rule was written for. A shipped Install has no checkout, so before this every
stage wrote `data/postings` beside whatever directory the launcher inherited:
running once from the Owner's home and once from their Desktop built two
half-populated stores, each looking complete, with nothing on screen to say so.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

POSTINGS = [
    {
        "key": "greenhouse:acme:1",
        "source": "greenhouse",
        "board": "acme",
        "title": "Research Associate",
        "location": "Boston, MA",
        "url": "https://example.test/apply/1",
        "description_html": "<p>Bench work in a wet lab.</p>",
    },
    {
        "key": "greenhouse:acme:2",
        "source": "greenhouse",
        "board": "acme",
        "title": "Data Intern",
        "location": "Reykjavik, Iceland",
        "url": "https://example.test/apply/2",
        "description_html": "<p>Must be a U.S. citizen.</p>",
    },
]


def profile(root: Path, name: str) -> Path:
    directory = root / "profiles" / name
    directory.mkdir(parents=True)
    (directory / "targeting.yaml").write_text("search:\n  terms: []\n", encoding="utf-8")
    (directory / "constraints.yaml").write_text("", encoding="utf-8")
    (directory / "resume.yaml").write_text("", encoding="utf-8")
    return directory


@pytest.fixture
def install(tmp_path: Path) -> Path:
    """An application data directory with one Profile and some Postings in it."""
    root = tmp_path / "install"
    profile(root, "ada")
    postings = root / "data" / "postings"
    postings.mkdir(parents=True)
    (postings / "2026-08-18.jsonl").write_text(
        "".join(json.dumps(posting) + "\n" for posting in POSTINGS), encoding="utf-8"
    )
    return root


def stage(
    module: str, *arguments: str, cwd: Path, install: Path, home: Path | None = None,
    override: bool = True,
) -> subprocess.CompletedProcess[str]:
    """One stage, run the way a shipped Install runs it: from anywhere."""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home if home is not None else install.parent / "home"),
    }
    if override:
        environment["VENATOR_HOME"] = str(install)
    return subprocess.run(
        [sys.executable, "-m", module, *arguments],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )


def elsewhere(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory


def test_two_working_directories_reach_the_same_store(tmp_path: Path, install: Path) -> None:
    first = elsewhere(tmp_path, "from-here")
    second = elsewhere(tmp_path, "from-over-there")

    one = stage("venator.match.run", cwd=first, install=install)
    two = stage("venator.match.run", cwd=second, install=install)

    assert one.returncode == 0, one.stderr
    assert two.returncode == 0, two.stderr
    decisions = install / "data" / "decisions"
    assert sorted(path.name for path in decisions.glob("*.jsonl"))
    # The old behaviour, stated as the thing that must not happen: a `data/`
    # growing beside whichever directory the command was started in.
    assert not (first / "data").exists()
    assert not (second / "data").exists()
    # The second run finds the first one's Filter Decisions already there.
    assert "2 already decided" in two.stdout


def test_each_run_says_where_its_stores_resolved_to(tmp_path: Path, install: Path) -> None:
    result = stage("venator.match.run", cwd=elsewhere(tmp_path, "anywhere"), install=install)

    assert f"Stores under {install}/" in result.stderr
    assert "application data directory" in result.stderr


def test_the_store_line_goes_to_stderr_so_piped_json_still_parses(
    tmp_path: Path, install: Path
) -> None:
    """`venator.track.status | jq` must keep getting JSON, as it did before."""
    result = stage("venator.track.status", cwd=elsewhere(tmp_path, "anywhere"), install=install)

    assert result.returncode == 0, result.stderr
    assert "Stores under" in result.stderr
    assert isinstance(json.loads(result.stdout), list)


def test_the_view_and_the_heartbeat_follow_the_same_rule(tmp_path: Path, install: Path) -> None:
    filters = stage("venator.match.run", cwd=elsewhere(tmp_path, "one"), install=install)
    view = stage("venator.view.build", cwd=elsewhere(tmp_path, "two"), install=install)

    assert filters.returncode == 0, filters.stderr
    assert view.returncode == 0, view.stderr
    assert (install / "build" / "venator.db").is_file()
    assert f"built {install / 'build' / 'venator.db'}" in view.stdout


def test_an_explicit_flag_still_means_exactly_what_it_said(tmp_path: Path, install: Path) -> None:
    named = tmp_path / "run" / "marketing"

    result = stage(
        "venator.match.run",
        "--postings-dir",
        str(install / "data" / "postings"),
        "--decisions-dir",
        str(named / "decisions"),
        cwd=elsewhere(tmp_path, "anywhere"),
        install=install,
    )

    assert result.returncode == 0, result.stderr
    assert sorted((named / "decisions").glob("*.jsonl"))
    assert not (install / "data" / "decisions").exists()


def test_the_claim_still_refuses_a_second_profile_on_the_default_store(
    tmp_path: Path, install: Path
) -> None:
    """One `data/decisions` belongs to one Profile, defaults included (PR #22)."""
    profile(install, "grace")

    first = stage("venator.match.run", "--profile", "ada", cwd=tmp_path, install=install)
    second = stage("venator.match.run", "--profile", "grace", cwd=tmp_path, install=install)

    assert first.returncode == 0, first.stderr
    assert (install / "data" / "decisions" / ".profile").read_text().strip() == "ada"
    assert second.returncode != 0
    assert "'ada'" in second.stderr and "grace" in second.stderr


def test_a_read_of_the_default_store_still_never_claims_it(
    tmp_path: Path, install: Path
) -> None:
    result = stage("venator.track.status", cwd=elsewhere(tmp_path, "anywhere"), install=install)

    assert result.returncode == 0, result.stderr
    assert not (install / "data" / "decisions" / ".profile").exists()


def test_a_checkout_does_not_receive_personal_store_data(tmp_path: Path, install: Path) -> None:
    """Even a checkout with old data on disk cannot replace the Install's store."""
    root = (tmp_path / "checkout").resolve()
    (root / "src" / "venator").mkdir(parents=True)
    (root / "src" / "venator" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "venator"\n', encoding="utf-8")
    (root / "ui").mkdir()
    postings = root / "data" / "postings"
    postings.mkdir(parents=True)
    (postings / "2026-08-18.jsonl").write_text(
        "".join(json.dumps(posting) + "\n" for posting in POSTINGS), encoding="utf-8"
    )
    profile(root, "ada")

    result = stage(
        "venator.match.run", "--profile-dir", str(root / "profiles" / "ada"),
        cwd=root / "ui", install=install,
    )

    assert result.returncode == 0, result.stderr
    assert f"Stores under {install}/" in result.stderr
    assert sorted((install / "data" / "decisions").glob("*.jsonl"))
    assert not (root / "data" / "decisions").exists()
    assert not (root / "ui" / "data").exists()


def test_a_work_tree_that_is_not_a_checkout_uses_the_install(
    tmp_path: Path, install: Path
) -> None:
    other = (tmp_path / "other-project").resolve()
    (other / ".git").mkdir(parents=True)
    (other / "data" / "postings").mkdir(parents=True)

    result = stage(
        "venator.match.run", "--profile-dir", str(install / "profiles" / "ada"),
        cwd=other, install=install,
    )

    assert result.returncode == 0, result.stderr
    assert sorted((install / "data" / "decisions").glob("*.jsonl"))
    assert not (other / "data" / "decisions").exists()


# --------------------------------------------------------------------------
# The interaction with the Profile claim on a store directory
# --------------------------------------------------------------------------
#
# `venator.profile.claim` locks a store *directory* to one Profile, and this
# module changes which directory that is. The two land together, so the
# interaction is run here rather than reasoned about: a claim that did not
# follow the store to its resolved location would either lock an Owner out of
# their own corpus or let a second Profile interleave into it, and both are
# silent.


def store_bytes(directory: Path) -> dict[str, bytes]:
    """Every file under `directory`, by relative path and by content.

    Bytes rather than row counts: `data/` is append-only (ADR-0001), and what a
    refusal has to leave behind is the store exactly as it was — a rewritten
    line keeps the count and is the worse failure of the two.
    """
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_the_claim_follows_the_store_to_its_new_location(
    tmp_path: Path, install: Path
) -> None:
    """A claimed store stays claimed when the Install moves, and refuses by name.

    The claim is recorded in the store directory, so moving the Install moves
    the claim with it. What must not happen is the claim being left behind: the
    Owner would find their own corpus refused, or a second Profile would be let
    into it.
    """
    profile(install, "grace")
    first = stage("venator.match.run", "--profile", "ada", cwd=tmp_path, install=install)
    assert first.returncode == 0, first.stderr

    moved = tmp_path / "moved-install"
    install.rename(moved)

    # The Profile that claimed it still reaches its own Filter Decisions.
    again = stage("venator.match.run", "--profile", "ada", cwd=tmp_path, install=moved)
    assert again.returncode == 0, again.stderr
    assert "2 already decided" in again.stdout
    assert (moved / "data" / "decisions" / ".profile").read_text().strip() == "ada"

    # A second Profile is still refused, and the refusal still names both.
    before = store_bytes(moved / "data")
    second = stage("venator.match.run", "--profile", "grace", cwd=tmp_path, install=moved)

    assert second.returncode != 0
    assert "'ada'" in second.stderr and "grace" in second.stderr
    assert str(moved / "data" / "decisions") in second.stderr
    # Refused before anything was appended, byte for byte.
    assert store_bytes(moved / "data") == before


def test_the_stamp_lands_in_the_newly_resolved_store_and_not_the_old_one(
    tmp_path: Path, install: Path
) -> None:
    """Pointed at a second Install, the claim is written there — and only there.

    Nothing is migrated, copied or relocated by a store changing location: the
    first Install is left exactly as it was, down to its bytes.
    """
    first = stage("venator.match.run", "--profile", "ada", cwd=tmp_path, install=install)
    assert first.returncode == 0, first.stderr
    untouched = store_bytes(install / "data")

    second_install = tmp_path / "other-install"
    profile(second_install, "ada")
    postings = second_install / "data" / "postings"
    postings.mkdir(parents=True)
    (postings / "2026-08-18.jsonl").write_text(
        "".join(json.dumps(posting) + "\n" for posting in POSTINGS), encoding="utf-8"
    )

    second = stage("venator.match.run", "--profile", "ada", cwd=tmp_path, install=second_install)

    assert second.returncode == 0, second.stderr
    assert (second_install / "data" / "decisions" / ".profile").read_text().strip() == "ada"
    # The Filter Decisions were made afresh there, not carried over.
    assert "2 decided" in second.stdout
    assert store_bytes(install / "data") == untouched


def queue_one_posting(tmp_path: Path, install: Path) -> None:
    """Get `greenhouse:acme:1` to a queued Match Score, as `ada`, from a historical row."""
    filters = stage("venator.match.run", "--profile", "ada", cwd=tmp_path, install=install)
    assert filters.returncode == 0, filters.stderr
    decisions = install / "data" / "decisions"
    day = next(decisions.glob("*.jsonl"))
    with day.open("a", encoding="utf-8", newline="\n") as destination:
        destination.write(
            json.dumps(
                {
                    "posting_key": "greenhouse:acme:1",
                    "stage": "llm_score",
                    "verdict": "queue",
                    "score": 90,
                    "reason": "The applicant has the bench experience the role asks for.",
                }
            )
            + "\n"
        )


def test_the_track_store_claim_holds_on_the_resolved_default(
    tmp_path: Path, install: Path
) -> None:
    """#29 guards `data/track/` too, and this PR decides which `data/track/` that is.

    The Track store is reached here only by resolution — the run names its own
    Postings and Filter Decisions outright, so the *only* default in play is
    `--track-dir`. That is what makes this the interaction test rather than a
    second copy of the decisions one: the directory the Track claim protects is
    the one `venator.paths` resolved.
    """
    queue_one_posting(tmp_path, install)
    approved = stage(
        "venator.track.record", "approve", "greenhouse:acme:1", "--profile", "ada",
        cwd=tmp_path, install=install,
    )
    assert approved.returncode == 0, approved.stderr
    track = install / "data" / "track"
    assert (track / ".profile").read_text().strip() == "ada"

    # A second Profile with its own Postings and Filter Decisions, but no
    # --track-dir: the Track store is the resolved default, and it is claimed.
    profile(install, "grace")
    grace = install / "data" / "grace"
    (grace / "postings").mkdir(parents=True)
    (grace / "postings" / "2026-08-18.jsonl").write_text(
        "".join(json.dumps(posting) + "\n" for posting in POSTINGS), encoding="utf-8"
    )
    before = store_bytes(install / "data")

    refused = stage(
        "venator.track.record", "approve", "greenhouse:acme:1", "--profile", "grace",
        "--postings-dir", str(grace / "postings"),
        "--decisions-dir", str(grace / "decisions"),
        cwd=tmp_path, install=install,
    )

    assert refused.returncode != 0
    assert str(track) in refused.stderr
    assert "'ada'" in refused.stderr and "grace" in refused.stderr
    assert store_bytes(install / "data") == before


def test_a_read_never_claims_either_resolved_store(tmp_path: Path, install: Path) -> None:
    """`track.status` reads both stores by resolution and stamps neither.

    A read that claimed a directory would decide whose store it is as a side
    effect of answering a question about it — and now that the directory is
    resolved rather than named, it would do so somewhere nobody pointed at.
    """
    result = stage("venator.track.status", "--profile", "ada", cwd=tmp_path, install=install)

    assert result.returncode == 0, result.stderr
    assert not (install / "data" / "decisions" / ".profile").exists()
    assert not (install / "data" / "track" / ".profile").exists()
