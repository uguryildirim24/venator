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
from pathlib import Path
from venator.discover.run import DETAIL_REFRESH_AFTER
from venator.discover.store import posting_revision
from venator.match.assessment import assess_posting
from venator.qualify.compile import compile_profile
from venator.qualify.store import (
    JEV_KIND,
    as_of_month as parse_as_of_month,
    promotion_events,
    qualification_rows,
    verify_store,
)
from venator.qualify.jev_effective import current_filter_version, jev_promotion_state
from venator.qualify.jev_release import JEV_RELEASE
from venator.qualify.jev_policy import _description_reason
from venator.qualify.posting import canonical_posting
from venator.view.jev import (
    ASSESSMENTS_DDL,
    JEV_TRIAGE_DDL,
    jev_view_bindings,
    materialize_triage,
    selected_jev_shadow,
)
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

def _board_names(profile: Profile | None = None) -> dict[str, str]:
    """ATS board token -> employer display name, for Postings stored before the
    company field existed (and for adapters that never learn it)."""
    return dict(profile.sources.names) if profile is not None else {}


SCHEMA = """
CREATE TABLE postings (
  key TEXT PRIMARY KEY,
  source TEXT,
  board TEXT,
  company TEXT,
  title TEXT,
  location TEXT,
  url TEXT,
  posted_at TEXT,
  discovered_at TEXT,
  description_html TEXT
);
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
""" + JEV_TRIAGE_DDL + """
CREATE TABLE jev_selection (mode TEXT NOT NULL CHECK (mode IN ('shadow', 'promoted')));
CREATE TABLE jev_skip (posting_key TEXT PRIMARY KEY, reason TEXT NOT NULL);
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
CREATE INDEX jev_triage_mode ON jev_triage (mode, decision, state);
CREATE INDEX jev_triage_current ON jev_triage (state, mode);
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
    nothing when all boards are named.

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
    qualifications_dir: Path | None = None,
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
    if qualifications_dir is not None or as_of_month is not None:
        requested["qualifications_dir"] = qualifications_dir
    stores = resolve_store_paths(**requested)
    postings_dir = stores["postings_dir"]
    decisions_dir = stores["decisions_dir"]
    database_path = stores["database_path"]
    track_dir = stores["track_dir"]
    runs_file = stores["runs_file"]
    qualifications_dir = stores.get("qualifications_dir")
    if profile is not None:
        verify_decisions_dir(decisions_dir, profile.identifier)
        verify_track_dir(track_dir, profile.identifier)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = database_path.with_name(f".{database_path.name}.tmp")
    temporary_path.unlink(missing_ok=True)
    postings = load_postings(postings_dir)
    qualification_versions: dict[str, str] = {}
    promotion_revision = ""
    promotion_state = None
    shadow_qualifier: str | None = None
    jev_mode = profile is not None and profile.filters.qualification_mode == "jev"
    jev_triage_rows: list[tuple[object, ...]] = []
    if jev_mode:
        if as_of_month is None:
            raise ValueError("jev qualification view requires --as-of")
        assert profile is not None
        assert qualifications_dir is not None
        verify_store(qualifications_dir, profile.identifier)
        compiled = compile_profile(profile, as_of_month=as_of_month)
        qualification_history = qualification_rows(qualifications_dir)
        promotion_state = jev_promotion_state(
            promotion_events(qualifications_dir), profile.identifier
        )
        accepted, qualification_versions = jev_view_bindings(
            profile, compiled, postings, as_of_month
        )
        active = (
            promotion_state.current(accepted, qualifier_kind=JEV_KIND)
            if accepted is not None
            else None
        )
        shadow_qualifier = selected_jev_shadow(qualification_history, profile.identifier)
        posting_revisions = {
            str(posting["key"]): posting_revision(posting) for posting in postings
        }
        jev_triage_rows = materialize_triage(
            postings,
            qualification_history,
            profile_id=profile.identifier,
            as_of_month=as_of_month,
            posting_revisions=posting_revisions,
            promoted_qualifier=active.qualifier_version if active else None,
            shadow_qualifier=shadow_qualifier,
            input_versions=qualification_versions,
        )
        promotion_revision = promotion_state.revision
    decisions = list(iter_jsonl(decisions_dir))
    latest_decisions = {
        (decision["posting_key"], decision["stage"]): decision
        for decision in decisions
        if isinstance(decision.get("posting_key"), str)
        and isinstance(decision.get("stage"), str)
    }
    track_events = load_events(track_dir)
    runs = list(_iter_jsonl_file(runs_file))
    folded_states = fold_states(track_events, latest_decisions)
    application_states = [folded_states[key] for key in sorted(folded_states)]
    current_filters_version = None
    if profile is not None:
        if jev_mode:
            current_filters_version = current_filter_version(
                profile,
                promotion_state,
                JEV_RELEASE,
            )
        else:
            current_filters_version = filters_version(
                profile.constraints_path,
                profile.targeting_path,
            )
    latest_hard = {
        posting_key: decision
        for (posting_key, stage), decision in latest_decisions.items()
        if stage == "hard_filter"
    }
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
    # Index materialized triage once; scanning it for every Posting is quadratic
    # on the real corpus (two rows per Posting).
    promoted_rows = {str(row[0]): row for row in jev_triage_rows if row[1] == "promoted"}
    selected_rows = {str(row[0]): row for row in jev_triage_rows
                     if row[1] == ("promoted" if jev_mode and active is not None else "shadow")}
    assessment_rows = []
    # No private cohort index ships in the public distribution.
    group_index: dict[str, object] = {}
    groups = group_index.get("posting_to_group", {})
    protected_hashes = set(JEV_RELEASE.protected_group_hashes) if jev_mode else set()
    jev_skip_rows = []
    for posting in postings:
        decision = latest_hard.get(posting["key"])
        stale_decision = False
        if decision is not None and (
            decision.get("filters_version") != current_filters_version
            or decision.get("posting_version") != posting_revision(posting)
        ):
            stale_decision = True
            delegated_before = (isinstance(decision.get("facts"), dict) and
                                any(value == "delegated" for value in decision["facts"].values()))
            if (
                decision.get("posting_version") != posting_revision(posting)
                or not (jev_mode and delegated_before)
            ):
                decision = None
        jev_row = promoted_rows.get(posting["key"])
        selected_row = selected_rows.get(posting["key"])
        if (jev_mode and posting.get("listing_status") != "closed"
                and latest_hard.get(posting["key"], {}).get("verdict") == "pass"
                and not (selected_row is not None and selected_row[2] == "current")):
            key = str(posting["key"])
            group = groups.get(key, key)
            if (hashlib.sha256(key.encode("utf-8")).hexdigest() in protected_hashes
                    or hashlib.sha256(group.encode("utf-8")).hexdigest() in protected_hashes):
                jev_skip_rows.append((key, "protected"))
            else:
                if posting.get("description_kind") != "full":
                    reason = "snippet" if posting.get("description_kind") == "snippet" else "missing"
                else:
                    try:
                        reason = _description_reason(posting, canonical_posting(posting, group_index))
                    except (TypeError, ValueError):
                        reason = "missing"
                if reason is not None:
                    jev_skip_rows.append((key, reason))
        if jev_mode and decision is not None and not stale_decision:
            # SPEC-jev §10: a new Jev answer is never combined with a hard
            # decision that was made against another one. The reserved pair
            # names what the decision was bound to; the filters version stays
            # static as responses arrive, so it cannot say this by itself.
            current_jev = jev_row is not None and jev_row[2] == "current"
            expected = (
                (jev_row[11], jev_row[12]) if current_jev and jev_row is not None
                else ("fallback", qualification_versions.get(posting["key"], "unavailable"))
            )
            facts = decision.get("facts") if isinstance(decision.get("facts"), dict) else {}
            if (facts.get("jev"), facts.get("jev_input")) != expected or expected[1] == "unavailable":
                stale_decision = True
                if not current_jev:
                    decision = None
        input_version = hashlib.sha256(json.dumps(
            ["evidence-v14", current_filters_version, resume_version, posting_revision(posting), decision,
             bool(posting.get("last_verified_at")), posting.get("verification_status"),
             bool(posting.get("detail_verified_at")), posting.get("detail_verification_status"),
             as_of_month, promotion_revision, shadow_qualifier, jev_row],
            sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        cached = cached_assessments.get(posting["key"])
        if cached is not None and cached["input_version"] == input_version:
            assessment_fields = tuple(cached[name] for name in ("status", "summary", "evidence", "conflicts", "unknowns"))
            assessed_by, assessed_as_of = (
                cached[name] for name in ("assessed_by", "assessed_as_of")
            )
        else:
            # Résumé evidence is independent of Jev's list decision.
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
            database.executemany("INSERT INTO jev_skip VALUES (?, ?)", jev_skip_rows)
            database.execute("INSERT INTO jev_selection (mode) VALUES (?)", (
                "promoted" if jev_mode and active is not None else "shadow",
            ))
            database.executemany("INSERT INTO assessments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", assessment_rows)
            if jev_triage_rows:
                database.executemany(
                    "INSERT INTO jev_triage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    jev_triage_rows,
                )
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
                  key, source, board, company, title, location, url, posted_at, discovered_at, description_html
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                posting_rows,
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
                [
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
                    for decision in decisions
                ],
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
                jev_mode=jev_mode,
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
    return len(postings), len(decisions)


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
    parser.add_argument("--qualifications-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--as-of")
    add_profile_argument(parser)
    args = parser.parse_args()
    today = args.as_of or date.today().isoformat()
    try:
        month = parse_as_of_month(today)
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
            qualifications_dir=args.qualifications_dir,
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
            qualifications_dir=stores["qualifications_dir"], as_of_month=month,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(
        f"built {stores['database_path']}: {postings} Postings, {decisions} Filter Decisions"
    )


if __name__ == "__main__":
    main()
