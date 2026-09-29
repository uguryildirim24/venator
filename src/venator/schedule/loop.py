"""Run the scheduled discovery-to-view pipeline and commit its data artifacts.

Usage:
    python -m venator.schedule.loop [--profile NAME] [--dry-run]
    python -m venator.schedule.loop --only discover,filters,view [--profile NAME]

``--only`` names a subset of ``STAGE_ORDER`` and runs it in ``STAGE_ORDER``
order — a caller chooses among the stages, never reorders them. It is how the
dashboard's run surface starts a run (``ui/server/runs/``), which is also why
the three stage lines this module prints are a contract now: ``<stage>:
starting`` and ``<stage>: ok`` on stdout, ``<stage>: ERROR — <message>`` on
stderr, each flushed as it is written. A reader that meets a line it does not
recognise ignores it; the exit status and the heartbeats stay the authority.

The commit stage runs only when the data directory is actually inside a Git
work tree. That is the developer case ADR-0001 describes: stages run in the
cloud, the Owner's Mac pulls, and the repository is the transport between them.
An Install that was shipped rather than cloned has no repository, and committing
one person's Postings and Filter Decisions into whatever repository the process
happens to be sitting in would be wrong there. So the stage detects the work
tree instead of assuming it, and says why it is skipping when there is none.

The loop's ``--as-of`` reaches every stage that reads it, so filtering and the view
resolve the same month.

The gate asks where the data directory *is*, so the answer must not be
overridable by the environment the process inherited: `$GIT_DIR` and friends
point Git at a repository regardless of where the command runs, and every `git`
here is handed an environment with them removed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from venator.paths import DATA_SUBDIR, StoreRootError, resolve_store_paths
from venator.schedule.lock import pipeline_lock
from venator.secrets import scrub, scrub_record


COMMIT_MESSAGE = "chore(data): record scheduled pipeline run"
STAGE_ORDER = ("discover", "filters", "score", "recheck", "view", "notify", "commit")
STAGE_DESCRIPTIONS = {
    "discover": "python -m venator.discover.run",
    "filters": "python -m venator.match.run",
    "score": "python -m venator.score.run --execute",
    "recheck": "refresh new picks and saved Postings",
    "view": "python -m venator.view.build",
    "notify": "notify about new picks from the rebuilt View",
    "commit": (
        f"git add data/ && git commit -m {COMMIT_MESSAGE!r} "
        "(if changed, and only when data/ is inside a Git work tree)"
    ),
}

#: Environment variables that tell Git which repository to operate on, wherever
#: the command is run from. Stripped before every `git` this module spawns: with
#: `$GIT_DIR` set, a shipped Install with no repository anywhere would sail past
#: the work-tree gate and commit one person's Postings and Filter Decisions into
#: whatever repository the environment happened to name.
GIT_LOCATION_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")

StageCallable = Callable[[], Mapping[str, Any] | None]


def git_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment to run `git` with: this one, minus the location overrides.

    Everything else is kept — `$PATH`, `$HOME`, the credential and locale
    settings a real run needs, and `$GIT_CEILING_DIRECTORIES`, which bounds
    discovery rather than redirecting it.
    """
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items() if key not in GIT_LOCATION_ENV}


def append_heartbeat(
    heartbeat_path: Path,
    stage: str,
    status: str,
    metrics: Mapping[str, Any] | None = None,
    *,
    error: str | None = None,
    pause_reason: str | None = None,
    waiting: int = 0,
    at: str | None = None,
) -> dict:
    """Append one dashboard-compatible heartbeat for an executed stage.

    Only bounded Score ledger metrics are copied onto the row.
    """
    row = {
        "at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "stage": stage,
    }
    if metrics:
        for key in ("usd", "batches", "waiting"):
            if key in metrics:
                row[key] = metrics[key]
    if error:
        row["error"] = error[:300]
    if pause_reason:
        row["pause_reason"] = pause_reason
        row["waiting"] = waiting
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    with heartbeat_path.open("a", encoding="utf-8", newline="\n") as destination:
        # The one boundary every stored record crosses — venator/secrets.py.
        # Scrubbed before serialising and again after: escaping a newline into
        # `\n` puts a word character where the text had a line break, and that
        # is enough to hide a key from every pattern there.
        destination.write(
            scrub(json.dumps(scrub_record(row), ensure_ascii=False)) + "\n"
        )
    return row


def _resolved(repository: Path | None, data_dir: Path | None) -> tuple[Path, Path]:
    """The directory the stages run in, and the data directory inside it.

    A caller that names `repository` and leaves `data_dir` off keeps the meaning
    those two arguments always had — `data/` *relative to that repository* —
    because that pairing is how a second store is pointed at
    (`commit_data(repository, Path("run/marketing"))`). Only when a default is
    actually in play does the Install's store root decide, and then both come
    from it together, so the stages, the heartbeat and the commit all land in
    one place (`venator.paths`).
    """
    if repository is not None and data_dir is None:
        return Path(repository), Path(DATA_SUBDIR)
    stores = resolve_store_paths(repository=repository, data_dir=data_dir)
    return stores["repository"], stores["data_dir"]


#: The stage modules that take ``--as-of``.
AS_OF_MODULES = frozenset({"venator.view.build", "venator.score.run"})


def run_module(
    module: str,
    repository: Path | None = None,
    profile: str | None = None,
    as_of: str | None = None,
    extra: Sequence[str] = (),
) -> dict[str, Any]:
    repository, _ = _resolved(repository, None)
    arguments = ["--profile", profile] if profile else []
    if as_of is not None and module in AS_OF_MODULES:
        arguments.extend(["--as-of", as_of])
    arguments.extend(extra)
    result = subprocess.run(
        [sys.executable, "-m", module, *arguments],
        cwd=repository,
        check=True,
        env=dict(os.environ),
        capture_output=module == "venator.score.run", text=module == "venator.score.run",
    )
    return json.loads(result.stdout) if module == "venator.score.run" else {}


def git_work_tree_for(path: Path) -> Path | None:
    """The Git work tree ``path`` sits in, or None when it sits in none.

    None is the shipped Install: no clone, no remote, nothing to commit to. It
    is also what a machine with no ``git`` on PATH gets, which is the same
    answer for the same reason.
    """
    probe = Path(path)
    while not probe.is_dir() and probe != probe.parent:
        probe = probe.parent
    try:
        found = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            env=git_environment(),
        )
    except OSError:
        return None
    if found.returncode != 0 or not found.stdout.strip():
        return None
    work_tree = Path(found.stdout.strip()).resolve()
    resolved = Path(path).resolve()
    if resolved != work_tree and work_tree not in resolved.parents:
        return None
    return work_tree


def data_is_versioned(repository: Path | None = None, data_dir: Path | None = None) -> bool:
    """Whether this Install's data directory is inside a Git work tree."""
    repository, data_dir = _resolved(repository, data_dir)
    return git_work_tree_for(Path(repository) / data_dir) is not None


def _pathspec(data_dir: Path, repository: Path | None = None) -> str:
    """`data_dir` as the pathspec every `git` in the commit stage is limited to.

    The commit stage stages, diffs and commits exactly one directory, and which
    one is the caller's `data_dir` — a literal `data/` here would quietly commit
    the wrong tree for any Install whose data lives elsewhere.

    The resolved data directory is absolute now that it no longer hangs off the
    working directory, and `git` is run with `cwd=repository`, so an absolute
    path inside that repository is written back down to a repository-relative
    one. A path that is *not* inside it is left absolute and `git` refuses it,
    which is the right answer: staging a directory outside the work tree is not
    something to paper over.
    """
    path = Path(data_dir)
    if path.is_absolute() and repository is not None:
        try:
            path = path.resolve().relative_to(Path(repository).resolve())
        except ValueError:
            pass
    return path.as_posix().rstrip("/") + "/"


def commit_data(
    repository: Path | None = None, data_dir: Path | None = None
) -> dict[str, Any]:
    """Commit data/ only when it differs from HEAD; never push.

    Skipped, loudly and successfully, when data/ is in no Git work tree.
    """
    repository, data_dir = _resolved(repository, data_dir)
    if not data_is_versioned(repository, data_dir):
        print(
            f"{Path(repository) / data_dir} is in no Git work tree; skipping the commit stage "
            "— this Install keeps its data in place and nothing is committed"
        )
        return {"_commit_created": False}
    pathspec = _pathspec(data_dir, repository)
    environment = git_environment()
    subprocess.run(["git", "add", "--", pathspec], cwd=repository, check=True, env=environment)
    changed = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", pathspec],
        cwd=repository,
        check=False,
        env=environment,
    )
    if changed.returncode == 0:
        print(f"{pathspec} is clean; no commit created")
        return {"_commit_created": False}
    if changed.returncode != 1:
        raise RuntimeError(f"git diff --cached exited {changed.returncode}")
    subprocess.run(
        ["git", "commit", "-m", COMMIT_MESSAGE, "--", pathspec],
        cwd=repository,
        check=True,
        env=environment,
    )
    return {"_commit_created": True}


def include_commit_heartbeat(
    repository: Path | None = None, data_dir: Path | None = None
) -> None:
    """Amend the successful commit-stage heartbeat into its data commit.

    Only ever reached when `commit_data` created a commit, which it does only
    inside a Git work tree.
    """
    repository, data_dir = _resolved(repository, data_dir)
    pathspec = _pathspec(data_dir, repository)
    environment = git_environment()
    subprocess.run(["git", "add", "--", pathspec], cwd=repository, check=True, env=environment)
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit", "--", pathspec],
        cwd=repository,
        check=True,
        env=environment,
    )


def recheck(repository: Path, profile_name: str | None) -> dict[str, Any]:
    from venator.paths import install_stores
    from venator.discover.refresh import refresh_posting
    from venator.discover.store import append_observations, load_postings, posting_revision
    from venator.profile import resolve_profile
    from venator.score.selection import select_inputs
    from venator.track.store import fold_states, load_events, verify_track_dir

    stores = install_stores()
    profile = resolve_profile(profile_name)
    _, inputs = select_inputs(profile, stores, date.today().isoformat())
    fetches = []
    if stores.runs_file.is_file():
        fetches = [entry['at'] for line in stores.runs_file.read_text(encoding='utf-8').splitlines()
                   if (entry := json.loads(line)).get('stage') == 'discover' and entry.get('status') == 'ok']
    previous_fetch = fetches[-2] if len(fetches) >= 2 else None
    postings = load_postings(stores.postings_dir)
    arrival = {posting['key']: str(posting.get('discovered_at', '')) for posting in postings}
    keys = {row.posting_key for row in inputs if row.score is not None
            and row.score.probability >= 0.5 and fetches
            and (previous_fetch is None or arrival.get(row.posting_key, '') > previous_fetch)}
    # Application actions append TrackEvents without rebuilding the View. Read the
    # live store so a Posting saved since yesterday's View is still rechecked.
    if stores.track_dir.exists():
        verify_track_dir(stores.track_dir, profile.identifier)
        keys.update(key for key, row in fold_states(load_events(stores.track_dir), {}).items()
                    if row['state'] in ('approved', 'prepared', 'rejected'))
    if not keys:
        return {}
    changed = 0
    for posting in postings:
        if posting['key'] not in keys:
            continue
        fresh = refresh_posting(posting)
        if not isinstance(fresh, Mapping) or any(fresh.get(k) != posting.get(k) for k in ('key', 'board', 'source')):
            continue
        append_observations(stores.postings_dir, [fresh])
        if posting_revision(fresh) != posting_revision(posting):
            changed += 1
    if changed:
        run_module('venator.match.run', repository, profile_name)
    return {'changed': changed}


def notify(repository: Path) -> dict[str, Any]:
    from venator.paths import install_stores
    view = install_stores().path / 'build/venator.db'
    if not view.is_file():
        return {}
    with sqlite3.connect(view) as database:
        rows = database.execute('''
            WITH fetches AS (
              SELECT julianday(at) AS finished FROM runs
              WHERE stage = 'discover' AND status = 'ok'
              ORDER BY finished DESC LIMIT 2
            ), window AS (
              SELECT max(finished) AS latest,
                     CASE WHEN count(*) = 2 THEN min(finished) END AS previous FROM fetches
            )
            SELECT p.title FROM postings p CROSS JOIN window w
            JOIN keep_scores k ON k.posting_key = p.key
            JOIN assessments a ON a.posting_key = p.key
            JOIN hard_filter_latest h ON h.posting_key = p.key
            JOIN decisions d ON d.id = h.decision_id
            LEFT JOIN application_states s ON s.posting_key = p.key
            WHERE k.probability >= 0.5 AND d.verdict = 'pass'
              AND a.listing_status = 'open' AND s.posting_key IS NULL
              AND w.latest IS NOT NULL AND julianday(p.discovered_at) <= w.latest
              AND (w.previous IS NULL OR julianday(p.discovered_at) > w.previous)
            ORDER BY p.discovered_at DESC
        ''').fetchall()
    receipt = view.parent / 'picks-notification.json'
    if rows:
        receipt.write_text(json.dumps({'count': len(rows), 'title': rows[0][0]}) + '\n',
                           encoding='utf-8', newline='\n')
        subprocess.run(['open', '-g', 'venator://picks'], check=True)
    else:
        receipt.unlink(missing_ok=True)
    return {'new_picks': len(rows)}


def default_stage_callables(
    repository: Path | None = None,
    profile: str | None = None,
    as_of: str | None = None,
    *,
    interactive_discover: bool = False,
) -> dict[str, StageCallable]:
    return {
        "discover": lambda: run_module(
            "venator.discover.run",
            repository,
            profile,
            extra=("--interactive",) if interactive_discover else (),
        ),
        "filters": lambda: run_module("venator.match.run", repository, profile, as_of),
        "score": lambda: run_module("venator.score.run", repository, profile, as_of, ("--execute",)),
        "recheck": lambda: recheck(repository, profile),
        "view": lambda: run_module("venator.view.build", repository, profile, as_of),
        "notify": lambda: notify(repository),
        "commit": lambda: commit_data(repository),
    }


class StageSelectionError(ValueError):
    """``--only`` named something that is not a stage."""


def parse_only(value: str | None) -> tuple[str, ...] | None:
    """``--only``'s comma-separated value as stage names, or None when unset.

    Splitting only. Which names are stages is `planned_stages`' question and is
    asked in one place, so `run_loop(only=[...])` called directly is held to the
    same rule as the flag. The order names arrive in is not preserved — see
    `planned_stages`.
    """
    if value is None:
        return None
    named = [part.strip() for part in value.split(",") if part.strip()]
    if not named:
        raise StageSelectionError(
            f"--only needs at least one stage name; the stages are {', '.join(STAGE_ORDER)}"
        )
    return tuple(dict.fromkeys(named))


def planned_stages(*, only: Sequence[str] | None = None) -> list[str]:
    """The stages this run will execute, always in ``STAGE_ORDER`` order.

    ``only`` selects a subset; it cannot reorder one. The order is the pipeline's
    — filters decide Postings discovery found, and the view is built from what
    both wrote — so a caller who could reorder it could only get it wrong.
    """
    if only is not None:
        chosen = set(only)
        unknown = [name for name in only if name not in STAGE_ORDER]
        if unknown:
            raise StageSelectionError(
                f"--only names {', '.join(repr(name) for name in unknown)}, which is not a "
                f"stage of this loop; the stages are {', '.join(STAGE_ORDER)}"
            )
        return [stage for stage in STAGE_ORDER if stage in chosen]
    # Discovery and eligibility produce an evidence-based view without
    # committing personal runtime data to Git.
    return ["discover", "filters", "score", "recheck", "view", "notify"]


def run_loop(
    *,
    only: Sequence[str] | None = None,
    dry_run: bool = False,
    stage_callables: Mapping[str, StageCallable] | None = None,
    heartbeat_path: Path | None = None,
    repository: Path | None = None,
    profile: str | None = None,
    as_of: str | None = None,
    interactive_discover: bool = False,
) -> int:
    # One resolution for the whole loop, announced once: every stage below runs
    # with `repository` as its working directory and therefore resolves the very
    # same store for itself.
    stores = resolve_store_paths(heartbeat_path=heartbeat_path, repository=repository)
    heartbeat_path = stores["heartbeat_path"]
    repository = stores["repository"]
    if as_of is not None:
        print(f"as_of={as_of} Profile={profile or 'default'} root={repository}", file=sys.stderr)
    stages = planned_stages(only=only)
    if dry_run:
        print("DRY RUN — no stages will execute")
        for index, stage in enumerate(stages, start=1):
            print(f"{index}. {stage}: {STAGE_DESCRIPTIONS[stage]}")
        if only is not None:
            omitted = [stage for stage in STAGE_ORDER if stage not in stages]
            if omitted:
                print(f"not selected by --only: {', '.join(omitted)}")
        # Only worth saying when the commit stage is actually planned; with
        # `--only discover`, a note about a stage nobody asked for is noise.
        if "commit" in stages and not data_is_versioned(repository):
            print("commit: would be skipped — data/ is in no Git work tree")
        return 0

    actions = dict(
        default_stage_callables(
            repository, profile, as_of, interactive_discover=interactive_discover,
        )
        if stage_callables is None
        else stage_callables
    )
    for stage in stages:
        action = actions.get(stage)
        if action is None:
            error = f"no callable configured for stage {stage!r}"
            append_heartbeat(heartbeat_path, stage, "error", error=error)
            print(f"{stage}: ERROR — {error}", file=sys.stderr)
            return 1
        print(f"{stage}: starting", flush=True)
        try:
            metrics = action() or {}
        except Exception as error:
            try:
                append_heartbeat(heartbeat_path, stage, "error", error=str(error))
            except Exception as heartbeat_error:
                print(
                    f"{stage}: ERROR writing failure heartbeat — {heartbeat_error}",
                    file=sys.stderr,
                    flush=True,
                )
            print(f"{stage}: ERROR — {error}", file=sys.stderr, flush=True)
            return 1
        try:
            paused = metrics.get("paused")
            if stage != "score":
                append_heartbeat(
                    heartbeat_path, stage, "paused" if paused else "ok", metrics,
                    pause_reason=paused, waiting=metrics.get("waiting", 0),
                )
            if stage == "commit" and metrics.get("_commit_created") is True:
                include_commit_heartbeat(repository)
        except Exception as error:
            print(f"{stage}: ERROR writing heartbeat — {error}", file=sys.stderr, flush=True)
            return 1
        print(f"{stage}: {'paused' if paused else 'ok'}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        default=None,
        metavar="STAGE[,STAGE...]",
        help=(
            "run only these stages, in the loop's own order; "
            f"one or more of {', '.join(STAGE_ORDER)}"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--as-of")
    parser.add_argument(
        "--interactive-discover",
        action="store_true",
        help="keep Discover bounded for a dashboard-triggered refresh",
    )
    # The loop forwards a Profile *name* to every stage; --profile-dir is a
    # single-stage escape hatch and is deliberately not threaded through here.
    parser.add_argument(
        "--profile",
        default=None,
        help="name of the Profile under profiles/ that every stage runs for",
    )
    args = parser.parse_args()
    today = args.as_of or date.today().isoformat()
    try:
        date.fromisoformat(today)
    except ValueError as error:
        parser.error(str(error))
    try:
        only = parse_only(args.only)
    except StageSelectionError as error:
        parser.error(str(error))
    try:
        if args.dry_run:
            return run_loop(only=only, dry_run=True, profile=args.profile, as_of=today)
        if only is not None:
            # Dashboard runs own this lock for the entire child tree.
            return run_loop(only=only, profile=args.profile, as_of=today,
                            interactive_discover=args.interactive_discover)
        from venator.paths import install_stores
        with pipeline_lock(install_stores().path):
            return run_loop(
                only=only, profile=args.profile, as_of=today,
                interactive_discover=args.interactive_discover,
            )
    except StageSelectionError as error:
        parser.error(str(error))
    except StoreRootError as error:
        print(f"ERROR — {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
