from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from venator.schedule.loop import (
    COMMIT_MESSAGE,
    GIT_LOCATION_ENV,
    STAGE_ORDER,
    StageSelectionError,
    commit_data,
    data_is_versioned,
    git_environment,
    git_work_tree_for,
    parse_only,
    planned_stages,
    run_loop,
)
from venator.view.build import build_database


def read_heartbeats(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def outside_any_work_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory that provably sits in no Git work tree.

    Assuming this of `tmp_path` is not good enough. `$TMPDIR` can itself be
    inside a checkout, and a test that merely assumed otherwise would not just
    fail there — `commit_data` would run, and two
    `chore(data): record scheduled pipeline run` commits would land in whoever's
    repository was hosting it. So the condition is *constructed*: Git's own
    `$GIT_CEILING_DIRECTORIES` stops repository discovery at `tmp_path`, and the
    precondition is then checked rather than trusted.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.resolve()))
    install = tmp_path / "Venator"
    (install / "data").mkdir(parents=True)
    if git_work_tree_for(install / "data") is not None:
        pytest.skip("$TMPDIR is inside a Git work tree that the ceiling did not stop")
    return install


def test_stage_order_and_heartbeat_shape(tmp_path: Path) -> None:
    calls = []
    heartbeat = tmp_path / "data" / "runs.jsonl"

    def stage(name: str):
        def run() -> dict[str, int]:
            calls.append(name)
            return {"pending": 2, "recorded": 1, "queued": 1}

        return run

    actions = {
        name: stage(name) for name in STAGE_ORDER
    }

    assert run_loop(stage_callables=actions, heartbeat_path=heartbeat) == 0
    assert calls == ["discover", "filters", "view"]
    rows = read_heartbeats(heartbeat)
    assert [row["stage"] for row in rows] == calls
    assert all(
        set(row) == {"at", "status", "stage"}
        for row in rows
    )
    assert all(row["status"] == "ok" for row in rows)
    assert all("pending" not in row and "recorded" not in row and "queued" not in row for row in rows)


def test_key_only_reaches_jev_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from venator.schedule import loop

    seen: list[tuple[str, str | None]] = []

    def fake_run(argv, **kwargs):
        seen.append((argv[2], kwargs["env"].get("TYPESAFE_API_KEY")))
        if argv[2] == "venator.qualify.jev":
            return subprocess.CompletedProcess(argv, 0, '{"paused": "no-key", "waiting": 1}', "")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-sentinel")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    loop.run_module("venator.discover.run", tmp_path)
    loop.run_module("venator.match.run", tmp_path)
    loop.run_jev(tmp_path, None, "2026-09-18")
    loop.run_module("venator.view.build", tmp_path)
    assert seen == [
        ("venator.discover.run", None), ("venator.match.run", None),
        ("venator.qualify.jev", "secret-sentinel"), ("venator.view.build", None),
    ]


def test_default_loop_never_spawns_jev_or_passes_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from venator.schedule import loop

    seen: list[tuple[str, str | None]] = []

    def fake_run(argv, **kwargs):
        seen.append((argv[2], kwargs["env"].get("TYPESAFE_API_KEY")))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-sentinel")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    assert run_loop(repository=tmp_path, heartbeat_path=tmp_path / "runs.jsonl") == 0
    assert seen == [
        ("venator.discover.run", None),
        ("venator.match.run", None),
        ("venator.view.build", None),
    ]


def test_paused_jev_still_builds_view(tmp_path: Path) -> None:
    heartbeat = tmp_path / "runs.jsonl"
    called: list[str] = []
    actions = {
        name: (lambda name=name: called.append(name) or
               ({"paused": "out-of-credit", "waiting": 3} if name == "jev" else {}))
        for name in STAGE_ORDER
    }
    assert run_loop(only=["jev", "view"], stage_callables=actions, heartbeat_path=heartbeat) == 0
    assert called == ["jev", "view"]
    rows = read_heartbeats(heartbeat)
    assert rows[0]["status"] == "paused"
    assert rows[0]["pause_reason"] == "out-of-credit"
    assert rows[0]["waiting"] == 3
    assert rows[1]["status"] == "ok"


def test_failure_heartbeats_and_aborts_remaining_stages(tmp_path: Path) -> None:
    calls = []
    heartbeat = tmp_path / "runs.jsonl"

    def ok(name: str):
        def run() -> None:
            calls.append(name)

        return run

    def fail() -> None:
        calls.append("filters")
        raise RuntimeError("fixture failure")

    actions = {
        "discover": ok("discover"),
        "filters": fail,
        "view": ok("view"),
        "commit": ok("commit"),
    }

    assert run_loop(stage_callables=actions, heartbeat_path=heartbeat) == 1
    assert calls == ["discover", "filters"]
    rows = read_heartbeats(heartbeat)
    assert [(row["stage"], row["status"]) for row in rows] == [
        ("discover", "ok"),
        ("filters", "error"),
    ]
    assert rows[-1]["error"] == "fixture failure"


def test_dry_run_executes_nothing(tmp_path: Path, capsys) -> None:
    calls = []
    heartbeat = tmp_path / "runs.jsonl"
    actions = {
        name: lambda name=name: calls.append(name)
        for name in STAGE_ORDER
    }

    assert run_loop(
        dry_run=True,
        stage_callables=actions,
        heartbeat_path=heartbeat,
    ) == 0

    assert calls == []
    assert not heartbeat.exists()
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "venator.match.run" in output
    assert "venator.view.build" in output


def test_explicit_empty_stage_map_cannot_fall_back_to_real_commands(tmp_path: Path) -> None:
    heartbeat = tmp_path / "runs.jsonl"

    assert run_loop(stage_callables={}, heartbeat_path=heartbeat) == 1

    rows = read_heartbeats(heartbeat)
    assert [(row["stage"], row["status"]) for row in rows] == [("discover", "error")]
    assert "no callable configured" in rows[0]["error"]


def test_default_run_omits_commit(tmp_path: Path) -> None:
    calls = []
    heartbeat = tmp_path / "runs.jsonl"
    actions = {
        name: lambda name=name: calls.append(name)
        for name in STAGE_ORDER
    }

    assert run_loop(
        stage_callables=actions,
        heartbeat_path=heartbeat,
    ) == 0

    assert calls == ["discover", "filters", "view"]
    assert [row["stage"] for row in read_heartbeats(heartbeat)] == calls


def test_view_builder_tolerates_stage_field_in_run_heartbeat(tmp_path: Path) -> None:
    postings = tmp_path / "postings"
    decisions = tmp_path / "decisions"
    track = tmp_path / "track"
    runs = tmp_path / "runs.jsonl"
    database_path = tmp_path / "build" / "venator.db"
    postings.mkdir()
    decisions.mkdir()
    runs.write_text(
        json.dumps(
            {
                "at": "2026-08-18T23:59:00+00:00",
                "status": "ok",
                "stage": "discover",
            }
        )
        + "\n"
    )

    assert build_database(postings, decisions, database_path, track, runs) == (0, 0)

    with sqlite3.connect(database_path) as database:
        row = database.execute(
            "SELECT at, status, stage FROM runs"
        ).fetchone()
    assert row == ("2026-08-18T23:59:00+00:00", "ok", "discover")


def test_commit_data_commits_only_data_and_skips_clean_tree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Schedule Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "schedule-test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    data = tmp_path / "data"
    data.mkdir()
    heartbeat = data / "runs.jsonl"
    heartbeat.write_text('{"status":"initial"}\n')
    subprocess.run(["git", "add", "data/"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

    heartbeat.write_text('{"status":"initial"}\n{"status":"ok"}\n')
    (tmp_path / "outside.txt").write_text("must remain uncommitted\n")

    assert commit_data(tmp_path)["_commit_created"] is True
    message = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert message == COMMIT_MESSAGE
    assert (tmp_path / "outside.txt").exists()
    assert "?? outside.txt" in subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert commit_data(tmp_path)["_commit_created"] is False


def test_commit_stage_heartbeat_is_amended_and_leaves_clean_data(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Schedule Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "schedule-test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    data = tmp_path / "data"
    data.mkdir()
    heartbeat = data / "runs.jsonl"
    heartbeat.write_text('{"status":"initial"}\n')
    subprocess.run(["git", "add", "data/"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

    calls = []

    def stage(name: str):
        def run() -> None:
            calls.append(name)

        return run

    actions = {
        "discover": stage("discover"),
        "filters": stage("filters"),
        "view": stage("view"),
        "commit": lambda: commit_data(tmp_path),
    }

    assert run_loop(
        only=["discover", "filters", "view", "commit"],
        stage_callables=actions,
        heartbeat_path=heartbeat,
        repository=tmp_path,
    ) == 0

    assert calls == ["discover", "filters", "view"]
    assert [row["stage"] for row in read_heartbeats(heartbeat)[-4:]] == [
        "discover",
        "filters",
        "view",
        "commit",
    ]
    assert subprocess.run(
        ["git", "status", "--porcelain", "--", "data/"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


def make_repository(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Schedule Test"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "schedule-test@example.invalid"], cwd=root, check=True
    )
    return root


def test_a_data_directory_inside_a_work_tree_is_versioned(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    (repository / "data").mkdir()

    assert git_work_tree_for(repository / "data") == repository.resolve()
    assert data_is_versioned(repository) is True


def test_a_data_directory_in_no_work_tree_is_not_versioned(
    outside_any_work_tree: Path,
) -> None:
    """The shipped Install: an application data directory, no clone anywhere."""
    assert git_work_tree_for(outside_any_work_tree / "data") is None
    assert data_is_versioned(outside_any_work_tree) is False


def test_commit_data_skips_and_says_why_outside_a_work_tree(
    outside_any_work_tree: Path, capsys
) -> None:
    install = outside_any_work_tree
    (install / "data" / "postings.jsonl").write_text('{"key":"x"}\n', encoding="utf-8")

    result = commit_data(install)

    assert result["_commit_created"] is False
    printed = capsys.readouterr().out
    assert "is in no Git work tree" in printed
    assert "nothing is committed" in printed


def test_the_loop_completes_outside_a_work_tree_with_the_commit_stage_skipped(
    outside_any_work_tree: Path,
) -> None:
    install = outside_any_work_tree
    heartbeat = install / "data" / "runs.jsonl"
    calls: list[str] = []

    def stage(name: str):
        def run() -> None:
            calls.append(name)

        return run

    actions = {
        "discover": stage("discover"),
        "filters": stage("filters"),
        "view": stage("view"),
        "commit": lambda: commit_data(install),
    }

    assert (
        run_loop(
            only=["discover", "filters", "view", "commit"],
            stage_callables=actions,
            heartbeat_path=heartbeat,
            repository=install,
        )
        == 0
    )
    rows = read_heartbeats(heartbeat)
    assert [row["stage"] for row in rows] == ["discover", "filters", "view", "commit"]
    assert rows[-1]["status"] == "ok"
    assert "recorded" not in rows[-1]


def test_a_dry_run_outside_a_work_tree_says_the_commit_stage_would_be_skipped(
    tmp_path: Path, capsys
) -> None:
    install = tmp_path / "Venator"
    (install / "data").mkdir(parents=True)

    assert run_loop(dry_run=True, repository=install, only=["commit"]) == 0

    assert "commit: would be skipped" in capsys.readouterr().out


def test_a_dry_run_inside_a_work_tree_does_not_claim_the_commit_stage_is_skipped(
    tmp_path: Path, capsys
) -> None:
    repository = make_repository(tmp_path)
    (repository / "data").mkdir()

    assert run_loop(dry_run=True, repository=repository) == 0

    assert "would be skipped" not in capsys.readouterr().out


def test_a_data_directory_that_only_looks_like_it_is_in_a_work_tree_is_not(
    tmp_path: Path,
) -> None:
    """The containment check: what makes this stricter than "am I in a repo".

    `git_work_tree_for` probes the nearest *existing* ancestor of the path,
    because a data directory that has not been created yet still has to get an
    answer. That walk can land inside a repository the path does not actually
    live in: nothing along `repo/install/../../ship/data` exists, so the walk
    climbs to `repo/` and Git dutifully answers `repo`. The path resolves to
    `ship/data`, outside that work tree entirely. Without the containment check
    the first scheduled run of a shipped Install sitting beside a checkout would
    commit that person's Postings and Filter Decisions into the checkout.
    """
    repository = make_repository(tmp_path / "repo")

    outside = repository / "install" / ".." / ".." / "ship" / "data"

    assert outside.resolve() == (tmp_path / "ship" / "data").resolve()
    assert git_work_tree_for(outside) is None
    # The same probe *inside* the work tree still answers, so this is the
    # containment check refusing and not the walk failing to find anything.
    assert git_work_tree_for(repository / "install" / "data") == repository.resolve()


def test_a_failed_git_probe_is_no_work_tree_whatever_it_printed(
    outside_any_work_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero `git` is not a work tree even when it wrote to stdout.

    `rev-parse` prints its usage and diagnostics; taking stdout without reading
    the exit status would turn an error message into a path to commit into.
    """
    import subprocess as subprocess_module

    from venator.schedule import loop as loop_module

    def failing(*arguments: object, **keywords: object) -> subprocess_module.CompletedProcess:
        return subprocess_module.CompletedProcess(
            args=["git"], returncode=128, stdout=str(outside_any_work_tree), stderr="fatal"
        )

    monkeypatch.setattr(loop_module.subprocess, "run", failing)

    assert git_work_tree_for(outside_any_work_tree / "data") is None


def test_a_machine_with_no_git_has_no_work_tree_rather_than_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `git` on PATH is the shipped Install's answer too, not a crash."""
    from venator.schedule import loop as loop_module

    def missing(*arguments: object, **keywords: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(loop_module.subprocess, "run", missing)

    assert git_work_tree_for(tmp_path) is None


def test_git_location_variables_are_stripped_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in GIT_LOCATION_ENV:
        monkeypatch.setenv(name, "/somewhere/else")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/keep/me")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = git_environment()

    assert not [name for name in GIT_LOCATION_ENV if name in environment]
    assert environment["GIT_CEILING_DIRECTORIES"] == "/keep/me"
    assert environment["PATH"] == "/usr/bin"


def test_git_dir_in_the_environment_cannot_smuggle_a_commit_out_of_an_install(
    outside_any_work_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`$GIT_DIR` must not talk the commit stage past the work-tree gate.

    A shipped Install has no repository. With `$GIT_DIR` and `$GIT_WORK_TREE`
    exported, Git would happily operate on the repository they name from
    anywhere at all — so the Install's Postings and Filter Decisions would be
    committed into a repository that has nothing to do with this person.
    """
    repository = make_repository(tmp_path / "elsewhere")
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repository, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()

    install = outside_any_work_tree
    (install / "data" / "postings.jsonl").write_text('{"key":"x"}\n', encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(repository / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(install))

    result = commit_data(install)

    assert result["_commit_created"] is False
    assert "is in no Git work tree" in capsys.readouterr().out
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip() == head


def test_the_commit_stage_stages_the_data_directory_it_was_given(tmp_path: Path) -> None:
    """`data_dir` decides the pathspec, not a literal `data/` in three places."""
    repository = make_repository(tmp_path)
    corpus = repository / "run" / "marketing"
    corpus.mkdir(parents=True)
    (corpus / "postings.jsonl").write_text('{"key":"x"}\n', encoding="utf-8")
    (repository / "data").mkdir()
    (repository / "data" / "postings.jsonl").write_text('{"key":"other"}\n', encoding="utf-8")

    assert commit_data(repository, Path("run/marketing"))["_commit_created"] is True

    committed = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert committed == ["run/marketing/postings.jsonl"]


# --- `--only`: the subset the dashboard's run surface starts -------------------
#
# The run surface never sequences stages itself (ui/server/runs/). It names a
# subset and this loop runs it, so what has to hold is that the subset is a
# *choice among* STAGE_ORDER and never a reordering of it, that a name that is
# not a stage is a usage error rather than a quietly shorter run, and that
# `commit` stays reachable from the CLI while the dashboard never names it.


def test_only_runs_the_named_subset_in_the_loops_own_order(tmp_path: Path) -> None:
    calls = []
    heartbeat = tmp_path / "runs.jsonl"
    actions = {name: (lambda name=name: calls.append(name)) for name in STAGE_ORDER}

    # Named backwards on purpose: a caller chooses among the stages and cannot
    # reorder them, because the order is the pipeline's own.
    assert run_loop(
        only=["view", "filters", "discover"],
        stage_callables=actions,
        heartbeat_path=heartbeat,
    ) == 0

    assert calls == ["discover", "filters", "view"]
    assert [row["stage"] for row in read_heartbeats(heartbeat)] == calls


def test_only_omits_every_stage_it_does_not_name(tmp_path: Path) -> None:
    calls = []
    heartbeat = tmp_path / "runs.jsonl"
    actions = {name: (lambda name=name: calls.append(name)) for name in STAGE_ORDER}

    assert run_loop(only=["discover"], stage_callables=actions, heartbeat_path=heartbeat) == 0

    assert calls == ["discover"]
    assert "commit" not in calls


def test_only_still_reaches_commit_from_the_command_line() -> None:
    """The dashboard never names `commit`; the CLI and the launchd job still may."""
    assert planned_stages(only=["discover", "commit"]) == ["discover", "commit"]


def test_default_refresh_never_commits_personal_data() -> None:
    assert planned_stages() == ["discover", "filters", "view"]


def test_a_name_that_is_not_a_stage_is_a_usage_error_naming_the_valid_set() -> None:
    with pytest.raises(StageSelectionError) as raised:
        planned_stages(only=["discover", "submit"])
    assert "'submit'" in str(raised.value)
    for stage in STAGE_ORDER:
        assert stage in str(raised.value)


def test_parse_only_splits_trims_and_deduplicates() -> None:
    assert parse_only(None) is None
    assert parse_only(" discover , filters ,discover ") == ("discover", "filters")
    with pytest.raises(StageSelectionError):
        parse_only(" , ")


def test_dry_run_names_the_subset_and_says_nothing_about_stages_nobody_asked_for(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    heartbeat = tmp_path / "runs.jsonl"

    assert run_loop(
        only=["discover", "filters", "view"],
        dry_run=True,
        heartbeat_path=heartbeat,
        repository=tmp_path,
    ) == 0

    printed = capsys.readouterr().out
    assert "1. discover:" in printed
    assert "2. filters:" in printed
    assert "3. view:" in printed
    assert "not selected by --only: jev, commit" in printed
    # The commit stage was not asked for, so its work-tree note is noise here.
    assert "commit: would be skipped" not in printed
    assert not heartbeat.exists()


def test_the_only_flag_reaches_run_loop_from_the_command_line(tmp_path: Path) -> None:
    """End to end through `main`, because the flag is only useful from argv."""
    finished = subprocess.run(
        [sys.executable, "-m", "venator.schedule.loop", "--only", "discover,view", "--dry-run"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "VENATOR_HOME": str(tmp_path / "Install")},
    )
    assert finished.returncode == 0, finished.stderr
    assert "1. discover:" in finished.stdout
    assert "2. view:" in finished.stdout
    assert "2. filters:" not in finished.stdout
    assert "not selected by --only: filters, jev, commit" in finished.stdout


def test_an_unknown_stage_on_the_command_line_exits_with_a_usage_error(tmp_path: Path) -> None:
    finished = subprocess.run(
        [sys.executable, "-m", "venator.schedule.loop", "--only", "submit", "--dry-run"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "VENATOR_HOME": str(tmp_path / "Install")},
    )
    assert finished.returncode == 2
    assert "'submit'" in finished.stderr


def test_the_filter_stage_receives_as_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from venator.schedule import loop

    calls: list[list[str]] = []
    monkeypatch.setattr(loop, "_resolved", lambda repository, data_dir: (tmp_path, Path("data")))
    monkeypatch.setattr(loop.subprocess, "run", lambda argv, **_kwargs: calls.append(list(argv)))
    loop.default_stage_callables(tmp_path, "example", "2026-09-18")["filters"]()
    assert calls[0][-4:] == ["--profile", "example", "--as-of", "2026-09-18"]
