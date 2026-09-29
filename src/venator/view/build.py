"""Rebuild the disposable SQLite view from pipeline JSONL stores.

Usage:
    python -m venator.view.build [--profile NAME]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import date, datetime, timezone
from itertools import islice
from pathlib import Path
from venator.discover.run import DETAIL_REFRESH_AFTER
from venator.discover.store import posting_revision
from venator.match.assessment import assess_posting
from venator.score.model import load_model
from venator.score.selection import inputs_for_passes
from venator.view.verify import verify_dashboard

from venator.match.store import (
    filters_version,
    iter_jsonl,
    load_postings,
    verify_decisions_dir,
)
from venator.track.store import fold_states, load_events, verify_track_dir
from venator.profile import (
    PROFILE_ENV,
    PROFILES_DIR,
    Profile,
    ProfileError,
    add_profile_argument,
    announce_profile,
    available_profiles,
    load_profile,
    profile_from_arguments,
)
from venator.paths import STORE_HELP, StoreRootError, resolve_store_paths

def _location_places(posting: Mapping[str, object]) -> str:
    """Keep source places beside the display label, including Workday additionalLocations.

    Old replay rows can lack the structured list; use only explicit source location
    fields, never descriptions or offices (which need not be work locations).
    """
    from venator.discover.common import normalize_locations

    locations = posting.get("locations")
    names = [item.get("name") for item in locations if isinstance(item, Mapping)] if isinstance(locations, list) else []
    if not names or re.search(r"\b\d+\s+locations?\b", str(posting.get("location", "")), re.I):
        facts = posting.get("source_facts")
        if isinstance(facts, Mapping):
            for fields in (facts, *(value for value in facts.values() if isinstance(value, Mapping))):
                recovered = normalize_locations(
                    fields.get("locationsText") or fields.get("location") or fields.get("locations"),
                    fields.get("additionalLocations") or fields.get("secondaryLocations"),
                )
                names.extend(item["name"] for item in recovered if item["name"])
    return json.dumps(list(dict.fromkeys(name for name in names if isinstance(name, str) and name.strip())))


def _board_names(profile: Profile | None = None) -> dict[str, str]:
    """ATS board token -> employer display name, for Postings stored before the
    company field existed (and for adapters that never learn it)."""
    return dict(profile.sources.names) if profile is not None else {}


ASSESSMENTS_DDL = """\
CREATE TABLE assessments (
  posting_key TEXT PRIMARY KEY,
  status TEXT,
  summary TEXT,
  evidence TEXT,
  conflicts TEXT,
  unknowns TEXT,
  listing_status TEXT,
  last_verified_at TEXT,
  description_kind TEXT,
  apply_url TEXT,
  opportunity_type TEXT,
  input_version TEXT,
  assessed_by TEXT NOT NULL DEFAULT 'deterministic'
    CHECK (assessed_by IN ('deterministic')),
  assessed_as_of TEXT
);"""


SCHEMA = """
CREATE TABLE postings (
  key TEXT PRIMARY KEY,
  source TEXT,
  board TEXT,
  company TEXT,
  title TEXT,
  location TEXT,
  location_places TEXT,
  url TEXT,
  posted_at TEXT,
  discovered_at TEXT,
  description_html TEXT
);
CREATE TABLE posting_revisions (key TEXT PRIMARY KEY, revision TEXT NOT NULL);
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY,
  posting_key TEXT,
  stage TEXT,
  verdict TEXT,
  rule TEXT,
  score INTEGER,
  reason TEXT,
  filters_version TEXT,
  decided_at TEXT
);
CREATE TABLE track_events (
  id INTEGER PRIMARY KEY,
  posting_key TEXT,
  event TEXT,
  actor TEXT,
  detail TEXT,
  at TEXT
);
CREATE TABLE application_states (
  posting_key TEXT PRIMARY KEY,
  state TEXT,
  detail TEXT,
  since TEXT
);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  at TEXT,
  status TEXT,
  stage TEXT,
  pause_reason TEXT,
  waiting INTEGER
);
""" + ASSESSMENTS_DDL + """
CREATE TABLE keep_scores (
  posting_key TEXT PRIMARY KEY, input_hash TEXT NOT NULL, model_id TEXT,
  probability REAL CHECK (probability >= 0 AND probability <= 1), scored_at TEXT
);
CREATE TABLE source_health (
  source_key TEXT PRIMARY KEY, status TEXT, last_attempt_at TEXT,
  last_success_at TEXT, count INTEGER, message TEXT,
  known_jobs INTEGER, full_verified_details INTEGER, needs_detail_check INTEGER
);
CREATE INDEX decisions_latest ON decisions (posting_key, stage, decided_at DESC, id DESC);
CREATE TABLE hard_filter_latest (posting_key TEXT PRIMARY KEY, decision_id INTEGER NOT NULL);
CREATE INDEX decisions_filters_version ON decisions (filters_version);
CREATE INDEX decisions_decided_at ON decisions (decided_at DESC);
CREATE INDEX track_events_posting ON track_events (posting_key, at, id);
CREATE INDEX postings_employer ON postings (source, board, company);
CREATE INDEX runs_fetch ON runs (stage, status, at DESC);
"""


_DETAIL_CLOCKS = ("detail_verified_at", "last_detail_verified_at", "detail_last_verified_at")


def _valid_past_timestamp(value: object, *, now: datetime) -> datetime | None:
    """Parse an observation clock only when it is timezone-aware and not future-dated."""

    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate[-1:].casefold() == "z":
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    return None if parsed > now else parsed


def _posting_clock(posting: Mapping[str, object], names: tuple[str, ...], *, now: datetime) -> datetime | None:
    """Use the first present clock; a malformed current clock cannot be rescued by an old alias."""

    for name in names:
        value = posting.get(name)
        if value not in (None, ""):
            return _valid_past_timestamp(value, now=now)
    return None


def _usable_full_description(posting: Mapping[str, object]) -> bool:
    """Whether a Posting carries actual readable full text, rather than an empty HTML shell."""

    kind = posting.get("description_kind")
    description = posting.get("description_html")
    if not isinstance(kind, str) or kind.casefold() != "full" or not isinstance(description, str):
        return False
    # Strip non-content elements before unescaping so ``&lt;p&gt;`` remains visible text,
    # while ``<p>&nbsp;</p>`` and comment-only payloads do not count as a description.
    content = re.sub(
        r"<!--.*?-->|<script\b[^>]*>.*?</script\s*>|<style\b[^>]*>.*?</style\s*>",
        " ",
        description,
        flags=re.IGNORECASE | re.DOTALL,
    )
    content = html.unescape(re.sub(r"<[^>]*>", " ", content)).replace("\u200b", "")
    return bool(" ".join(content.split()))


def _source_key(posting: Mapping[str, object]) -> str:
    source = posting.get("source")
    board = posting.get("board")
    # Legacy/imported rows still belong in the coverage denominator. Missing
    # attribution must remain visible rather than making publication fail.
    if not isinstance(source, str) or not source:
        return "unknown"
    if not isinstance(board, str) or not board:
        return f"{source}:unknown"
    return f"{source}:{board}"


def _status(posting: Mapping[str, object], field: str) -> str | None:
    value = posting.get(field)
    return value.casefold().strip() if isinstance(value, str) else None


def source_coverage(
    postings: Iterable[Mapping[str, object]], *, now: datetime | None = None
) -> dict[str, dict[str, int]]:
    """Count known rows and verified detail evidence for each stored source board.

    ``known_jobs`` is the effective append-only Posting set, so a partial source's
    health ``count`` never becomes a false denominator. Direct ATS sources verify
    full descriptions through their listing response; Workday requires its separate
    detail verification clock and status within the shared refresh window. Closed
    rows remain known but do not need a detail check because they cannot be acted on
    as current listings.
    """

    current = now or datetime.now(timezone.utc)
    coverage: dict[str, dict[str, int]] = {}
    for posting in postings:
        key = _source_key(posting)
        counts = coverage.setdefault(
            key,
            {"known_jobs": 0, "full_verified_details": 0, "needs_detail_check": 0},
        )
        counts["known_jobs"] += 1
        listing_status = _status(posting, "listing_status")
        if listing_status == "closed":
            continue
        listing_clock = _posting_clock(posting, ("last_verified_at",), now=current)
        listing_verified = (
            listing_status == "open"
            and _status(posting, "verification_status") == "verified"
            and listing_clock is not None
            and current - listing_clock < DETAIL_REFRESH_AFTER
        )
        if _status(posting, "source") == "workday":
            detail_clock = _posting_clock(posting, _DETAIL_CLOCKS, now=current)
            detail_verified = (
                _status(posting, "detail_verification_status") == "verified"
                and detail_clock is not None
                and current - detail_clock < DETAIL_REFRESH_AFTER
            )
            full_verified = listing_verified and detail_verified and _usable_full_description(posting)
        else:
            full_verified = listing_verified and _usable_full_description(posting)
        if full_verified:
            counts["full_verified_details"] += 1
        else:
            counts["needs_detail_check"] += 1
    return coverage


def _iter_jsonl_file(path: Path) -> Iterable[dict]:
    """Read one optional JSONL file with the same guarantees as iter_jsonl."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON in {path}:{line_number}: {error.msg}") from error
            if not isinstance(value, dict):
                raise ValueError(f"expected a JSON object in {path}:{line_number}")
            yield value


def _warn_about_unnamed_employers(
    companies: list[str | None], *, profile: Profile | None
) -> None:
    """Say out loud how many Postings the view could not give an employer name.

    An Install that holds no Profile is a legitimate state — absent means "not
    configured", not "invalid" — so this stays a warning and the view still
    builds. But the loss is otherwise silent and large: with no Profile there is
    no `sources.names`, so every ATS Posting lands with `company` NULL and the
    whole dashboard reads "via Greenhouse" with no employer anywhere. Naming the
    count and the cause is what makes a degraded view legible as degraded.

    The same line covers the narrower case where a Profile is selected but some
    board it collected from is not named in its registry. Healthy builds print
    nothing: the count can be 0.

    It is addressed to a person, so it goes to stderr. This command's stdout is
    a machine-readable channel — the `built ...` line and nothing else — and a
    warning interleaved into it is a line a consumer has to parse around
    (coordination/CONTRACTS.md, "Command output streams").
    """
    unnamed = sum(1 for company in companies if company is None)
    if not unnamed:
        return
    cause = (
        "no Profile is configured, so no board registry names them"
        if profile is None
        else "their board is not named in the Profile's targeting.yaml: sources.names"
    )
    print(
        f"warning: {unnamed} of {len(companies)} Postings have no employer name "
        f"({cause}); the dashboard falls back to the source for those",
        file=sys.stderr,
    )


def build_database(
    postings_dir: Path | None = None,
    decisions_dir: Path | None = None,
    database_path: Path | None = None,
    track_dir: Path | None = None,
    runs_file: Path | None = None,
    *,
    profile: Profile | None = None,
    as_of_month: str | None = None,
) -> tuple[int, int]:
    """Build into a temporary database and atomically replace the old view.

    A Profile resolves here for employer-name enrichment, and once one has, it
    also decides whose Filter Decisions this database is allowed to hold. The
    view is a read path like any other: building one Profile's decisions into a
    database the dashboard then presents under another Profile's name is the
    same silent wrong answer an unguarded read gives, one screen further on and
    harder to notice, because a dashboard shows no Profile at all. Without a
    Profile the build stays unguarded and simply loses the enrichment — an
    unclaimed store is unknown, not a mismatch.

    The Track store is checked on the same terms. ``track_events`` and
    ``application_states`` are materialized straight out of it, and a lifecycle
    state is keyed by Posting alone, so another Profile's approvals and
    rejections would reach the dashboard as this Profile's — the Focus view's
    status for a Posting, decided by whichever Owner acted last. Neither check
    claims: building a view is a read.
    """
    requested = dict(postings_dir=postings_dir, decisions_dir=decisions_dir,
                     database_path=database_path, track_dir=track_dir, runs_file=runs_file)
    stores = resolve_store_paths(**requested)
    postings_dir = stores["postings_dir"]
    decisions_dir = stores["decisions_dir"]
    database_path = stores["database_path"]
    track_dir = stores["track_dir"]
    runs_file = stores["runs_file"]
    if profile is not None:
        verify_decisions_dir(decisions_dir, profile.identifier)
        verify_track_dir(track_dir, profile.identifier)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = database_path.with_name(f".{database_path.name}.tmp")
    temporary_path.unlink(missing_ok=True)
    postings = load_postings(postings_dir)
    # Only the latest row per Posting/stage is needed in memory for assessment.
    # Replay the append-only store again when inserting into SQLite instead of
    # retaining hundreds of thousands of historical Filter Decisions as dicts.
    latest_decisions = {}
    decision_count = 0
    for decision in iter_jsonl(decisions_dir):
        decision_count += 1
        if isinstance(decision.get("posting_key"), str) and isinstance(decision.get("stage"), str):
            latest_decisions[(decision["posting_key"], decision["stage"])] = decision
    track_events = load_events(track_dir)
    runs = list(_iter_jsonl_file(runs_file))
    folded_states = fold_states(track_events, latest_decisions)
    application_states = [folded_states[key] for key in sorted(folded_states)]
    current_filters_version = (
        filters_version(profile.constraints_path, profile.targeting_path)
        if profile is not None else None
    )
    latest_hard = {
        posting_key: decision
        for (posting_key, stage), decision in latest_decisions.items()
        if stage == "hard_filter"
    }
    model = load_model(postings_dir.parent.parent)
    score_inputs = []
    if profile is not None and current_filters_version is not None:
        score_inputs = inputs_for_passes(
            postings, latest_hard, profile, as_of_month or '', current_filters_version,
            model, postings_dir.parent / 'keep-scores',
        )
    cached_assessments = {}
    resume_version = hashlib.sha256(json.dumps(
        dict(profile.resume) if profile is not None else None,
        sort_keys=True, ensure_ascii=False, default=str,
    ).encode("utf-8")).hexdigest()
    if database_path.is_file():
        try:
            with closing(sqlite3.connect(database_path)) as previous_view:
                previous_view.row_factory = sqlite3.Row
                cached_assessments = {
                    row["posting_key"]: dict(row) for row in previous_view.execute(
                        "SELECT posting_key, input_version, status, summary, evidence, conflicts, unknowns, "
                        "assessed_by, assessed_as_of FROM assessments"
                    )
                }
        except sqlite3.DatabaseError:
            # Older or damaged derived views can always be rebuilt from source.
            cached_assessments = {}
    assessment_rows = []
    for posting in postings:
        decision = latest_hard.get(posting["key"])
        stale_decision = False
        if decision is not None and (
            decision.get("filters_version") != current_filters_version
            or decision.get("posting_version") != posting_revision(posting)
        ):
            stale_decision = True
            decision = None
        input_version = hashlib.sha256(json.dumps(
            ["evidence-v15", current_filters_version, resume_version, posting_revision(posting), decision,
             bool(posting.get("last_verified_at")), posting.get("verification_status"),
             bool(posting.get("detail_verified_at")), posting.get("detail_verification_status"),
             as_of_month],
            sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        cached = cached_assessments.get(posting["key"])
        if cached is not None and cached["input_version"] == input_version:
            assessment_fields = tuple(cached[name] for name in ("status", "summary", "evidence", "conflicts", "unknowns"))
            assessed_by, assessed_as_of = (
                cached[name] for name in ("assessed_by", "assessed_as_of")
            )
        else:
            assessment = assess_posting(posting, profile, decision)
            assessed_by = "deterministic"
            assessed_as_of = None
            assessment_fields = (
                assessment["status"], assessment["summary"],
                json.dumps(assessment["evidence"], ensure_ascii=False),
                json.dumps(assessment["conflicts"], ensure_ascii=False),
                json.dumps(assessment["unknowns"], ensure_ascii=False),
            )
        assessment_rows.append((
            posting["key"], *assessment_fields,
            posting.get("listing_status", "unknown"),
            posting.get("detail_verified_at") if posting.get("source") == "workday" else posting.get("last_verified_at"),
            posting.get("description_kind", "snippet" if posting.get("source") == "adzuna" else "missing"),
            posting.get("apply_url") or posting.get("url"), posting.get("opportunity_type", "unknown"), input_version,
            assessed_by, assessed_as_of,
        ))

    board_names = _board_names(profile)
    companies: list[str | None] = [
        posting.get("company") or board_names.get(posting.get("board", ""), None)
        for posting in postings
    ]
    posting_rows = [
        (
            posting["key"],
            posting.get("source"),
            posting.get("board"),
            company,
            posting.get("title"),
            posting.get("location"),
            _location_places(posting),
            posting.get("url"),
            posting.get("posted_at"),
            posting.get("discovered_at"),
            posting.get("description_html"),
        )
        for posting, company in zip(postings, companies, strict=True)
    ]
    coverage = source_coverage(postings)

    try:
        # `closing` is not decoration. sqlite3's own context manager commits the
        # transaction and leaves the connection OPEN, so without this the
        # os.replace below renames a file this very process still holds a handle
        # on. POSIX allows that; Windows does not — SQLite opens with
        # FILE_SHARE_READ | FILE_SHARE_WRITE and no FILE_SHARE_DELETE, so
        # MoveFileExW fails with WinError 32 with no dashboard running and no
        # second process involved. The cleanup in the `except` below fails the
        # same way, replacing a real build error with a PermissionError raised
        # from the handler and demoting the original to __context__.
        with closing(sqlite3.connect(temporary_path)) as database, database:
            database.executescript(SCHEMA)
            database.executemany("INSERT INTO keep_scores VALUES (?, ?, ?, ?, ?)", (
                (row.posting_key, row.input_hash, model.model_id if model else None,
                 row.score.probability if row.score else None, row.score.scored_at if row.score else None)
                for row in score_inputs
            ))
            database.executemany("INSERT INTO assessments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", assessment_rows)
            health_path = postings_dir / "source-health.json"
            health = {}
            if health_path.exists():
                health = json.loads(health_path.read_text(encoding="utf-8"))
                if not isinstance(health, dict):
                    raise ValueError(f"expected source health to be an object in {health_path}")
            empty_coverage = {
                "known_jobs": 0,
                "full_verified_details": 0,
                "needs_detail_check": 0,
            }
            health_rows = []
            for key in sorted(set(health) | set(coverage)):
                value = health.get(key, {})
                if not isinstance(value, Mapping):
                    raise ValueError(f"expected source health {key!r} to be an object")
                source_counts = coverage.get(key, empty_coverage)
                health_rows.append(
                    (
                        key,
                        value.get("status", "unknown"),
                        value.get("last_attempt_at"),
                        value.get("last_success_at"),
                        value.get("count", 0),
                        value.get("message"),
                        source_counts["known_jobs"],
                        source_counts["full_verified_details"],
                        source_counts["needs_detail_check"],
                    )
                )
            database.executemany(
                "INSERT INTO source_health VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                health_rows,
            )
            database.executemany(
                """
                INSERT INTO postings (
                  key, source, board, company, title, location, location_places, url, posted_at, discovered_at, description_html
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                posting_rows,
            )
            database.executemany(
                "INSERT INTO posting_revisions VALUES (?, ?)",
                ((posting["key"], posting_revision(posting)) for posting in postings),
            )
            # A decision row also carries `profile_id` — which Profile produced it
            # — and the view deliberately does not materialize it. A decisions
            # store belongs to one Profile by construction (the `.profile`
            # claim), so every row this database holds names the same Profile
            # and a column of one repeated value tells the dashboard nothing it
            # could act on. The identifier earns its keep on the JSONL row,
            # where it makes a store self-describing away from the checkout that
            # wrote it; the diagnostic that reads it lives in `match.store`, not
            # in the UI. Materializing it would also change the view DDL that
            # `coordination/CONTRACTS.md` fixes and that `ui/server/db.ts`
            # asserts on every open — cost on both sides of a boundary, for a
            # column with no reader. Add it when something in the UI needs it.
            database.executemany(
                """
                INSERT INTO decisions (
                  posting_key, stage, verdict, rule, score, reason, filters_version, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        decision.get("posting_key"),
                        decision.get("stage"),
                        decision.get("verdict"),
                        decision.get("rule"),
                        decision.get("score"),
                        decision.get("reason"),
                        decision.get("filters_version"),
                        decision.get("decided_at"),
                    )
                    for decision in islice(iter_jsonl(decisions_dir), decision_count)
                ),
            )
            database.execute("""INSERT INTO hard_filter_latest
                SELECT posting_key, id FROM (
                  SELECT posting_key, id, ROW_NUMBER() OVER (
                    PARTITION BY posting_key ORDER BY decided_at DESC, id DESC
                  ) AS recency FROM decisions WHERE stage = 'hard_filter'
                ) WHERE recency = 1""")
            database.executemany(
                """
                INSERT INTO track_events (
                  posting_key, event, actor, detail, at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        event.get("posting_key"),
                        event.get("event"),
                        event.get("actor"),
                        event.get("detail"),
                        event.get("at"),
                    )
                    for event in track_events
                ],
            )
            database.executemany(
                """
                INSERT INTO application_states (
                  posting_key, state, detail, since
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        application_state["posting_key"],
                        application_state["state"],
                        application_state["detail"],
                        application_state["since"],
                    )
                    for application_state in application_states
                ],
            )
            # `stage` names which step of `venator.schedule.loop` wrote the
            # heartbeat — discover, filters, view or commit. A row without one
            # is NULL, never guessed at here.
            database.executemany(
                """
                INSERT INTO runs (
                  at, status, stage, pause_reason, waiting
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        run.get("at"),
                        run.get("status"),
                        run.get("stage"),
                        run.get("pause_reason"),
                        run.get("waiting"),
                    )
                    for run in runs
                ],
            )
            verify_dashboard(
                database,
                postings,
                latest_hard,
                current_filters_version,
            )
        os.replace(temporary_path, database_path)
    except Exception:
        # Whatever went wrong, that is the failure worth reporting. Removing the
        # half-built temporary is housekeeping, and housekeeping that raises here
        # would replace the real diagnosis with its own and leave the original
        # reachable only as __context__. `closing` above means the handle is
        # already gone, so the ordinary case cannot fail; something else holding
        # the file (an antivirus scanner, a crashed earlier run) still can.
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _warn_about_unnamed_employers(companies, profile=profile)
    return len(postings), decision_count


def _install_holds_no_profile(
    args: argparse.Namespace, profiles_dir: Path = PROFILES_DIR
) -> bool:
    """True only when nothing named a Profile and the install holds none.

    This is the one absence the view tolerates. It is deliberately narrower
    than "resolving a Profile raised": a named Profile that does not exist, a
    ``--profile-dir`` that is not one, a Profile whose files will not load, and
    two Profiles with nothing to choose between them are all failures to name a
    Profile, not the absence of one, and each is reported instead of ignored.
    """
    if args.profile is not None or args.profile_dir is not None:
        return False
    if PROFILE_ENV in os.environ:
        return False
    try:
        return not any(
            not load_profile(directory).scaffold
            for directory in available_profiles(profiles_dir)
        )
    except ProfileError:
        # A directory that will not load leaves the question open rather than
        # answering it "no Profile" — the caller reports the original error.
        return False


def _profile_for_view(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> Profile | None:
    """The Profile whose ``sources`` name the employers, or None if there is none.

    The view is disposable, so an install that holds no Profile at all — a
    fresh checkout, a fixture build — still gets a database, without employer
    display names. Anything else stops the build: `company` is read from the
    Profile for every ATS board at once, so a swallowed ProfileError does not
    degrade the view a little, it empties one column for the whole corpus while
    printing the same line a healthy build prints.
    """
    try:
        profile = profile_from_arguments(args)
    except ProfileError as error:
        if not _install_holds_no_profile(args):
            parser.error(str(error))
        # Diagnostic, not output: stderr for the same reason the unnamed-employer
        # warning is there.
        print(
            "no Profile is configured, so no employer display names are available; "
            "building the view without them",
            file=sys.stderr,
        )
        return None
    announce_profile(profile)
    return profile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postings-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--decisions-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--database", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--track-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--runs-file", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--as-of")
    add_profile_argument(parser)
    args = parser.parse_args()
    today = args.as_of or date.today().isoformat()
    try:
        month = date.fromisoformat(today).strftime("%Y-%m")
    except ValueError as error:
        parser.error(str(error))
    profile = _profile_for_view(args, parser)
    try:
        stores = resolve_store_paths(
            postings_dir=args.postings_dir,
            decisions_dir=args.decisions_dir,
            database_path=args.database,
            track_dir=args.track_dir,
            runs_file=args.runs_file,
        )
    except StoreRootError as error:
        parser.error(str(error))
    print(f"as_of={today} Profile={profile.identifier if profile else 'none'} root={stores['database_path'].parent.parent}", file=sys.stderr)
    try:
        postings, decisions = build_database(
            stores["postings_dir"],
            stores["decisions_dir"],
            stores["database_path"],
            stores["track_dir"],
            stores["runs_file"],
            profile=profile,
            as_of_month=month,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(
        f"built {stores['database_path']}: {postings} Postings, {decisions} Filter Decisions"
    )


if __name__ == "__main__":
    main()
