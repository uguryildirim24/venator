"""Append-only Posting observations and source health.

The JSONL corpus remains the transport format. New Postings retain full rows;
refreshes append only changed fields rather than duplicate bodies. This lets
discovery retain the first time a Posting was seen while recording later
content, lifecycle, and verification changes without rewriting historical data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

OBSERVATION_TIMES = frozenset(
    {
        "discovered_at",
        "first_seen_at",
        "last_verified_at",
        "observed_at",
        "observation_at",
        "retrieved_at",
        "fetched_at",
        "last_attempt_at",
        "detail_attempted_at",
        "detail_verified_at",
        "last_detail_verified_at",
        "detail_last_verified_at",
        "detail_observed_at",
        "detail_fetched_at",
        "verification_attempted_at",
        "verification_checked_at",
        "last_verification_at",
        "verification_timestamp",
        "last_checked_at",
        "checked_at",
    }
)
VERIFICATION_BOOKKEEPING = frozenset(
    {
        "verification_status",
        "verification_error",
        "verification_reason",
        "detail_verification_status",
        "detail_verification_error",
        "detail_verification_reason",
        "closure_reason",
    }
)
LIFECYCLE_STATES = frozenset({"open", "closed", "unknown"})
HEALTH_STATES = frozenset({"ok", "partial", "failed", "skipped"})
POSTING_PART_MAX_BYTES = 90_000_000
_DELTA_RESERVED_FIELDS = frozenset({"key", "_observation", "_remove"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonl_paths(postings_dir: Path) -> list[Path]:
    return sorted(postings_dir.glob("*.jsonl")) if postings_dir.exists() else []


def _read_rows(postings_dir: Path) -> Iterable[dict]:
    for path in _jsonl_paths(postings_dir):
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


def latest_postings(postings_dir: Path, *, keys: set[str] | None = None) -> dict[str, dict]:
    """Return current observations, optionally retaining only named Posting keys.

    Parse every row (including unselected rows) so malformed JSON and missing
    keys still fail at the store boundary. A selected delta still sees every
    earlier observation for its key.
    """

    latest: dict[str, dict] = {}
    for posting in _read_rows(postings_dir):
        key = posting.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("Posting is missing a non-empty string key")
        if keys is not None and key not in keys:
            continue
        if "_observation" in posting:
            updates, removed = posting["_observation"], posting.get("_remove", [])
            if (
                key not in latest
                or not isinstance(updates, dict)
                or set(updates) & _DELTA_RESERVED_FIELDS
                or not isinstance(removed, list)
                or any(
                    not isinstance(name, str) or name in _DELTA_RESERVED_FIELDS
                    for name in removed
                )
            ):
                raise ValueError(f"Invalid Posting observation for {key!r}")
            latest[key].update(updates)
            for name in removed:
                latest[key].pop(name, None)
        else:
            latest[key] = posting
    # Historical full-text rows predate description_kind. Normalize them at
    # the store boundary so every downstream reader sees the same contract.
    for posting in latest.values():
        if posting.get("description_html") and not posting.get("description_kind"):
            posting["description_kind"] = "full"
    return latest


def load_postings(postings_dir: Path) -> list[dict]:
    """List current Postings, retaining the last observation for each key."""

    return list(latest_postings(postings_dir).values())


def current_postings(postings_dir: Path) -> dict[str, dict]:
    """Alias used by refresh callers that need a key-indexed current view."""

    return latest_postings(postings_dir)


def _valid_status(value: object) -> str | None:
    if isinstance(value, str) and value.casefold() in LIFECYCLE_STATES:
        return value.casefold()
    return None


def _first_seen(previous: Mapping[str, Any] | None, posting: Mapping[str, Any], observed_at: str) -> str:
    if previous:
        value = previous.get("first_seen_at") or previous.get("discovered_at")
        if isinstance(value, str) and value:
            return value
    value = posting.get("first_seen_at") or posting.get("discovered_at")
    return value if isinstance(value, str) and value else observed_at


def _merge_source_facts(previous: object, incoming: object) -> object:
    if isinstance(previous, Mapping) and isinstance(incoming, Mapping):
        merged = dict(previous)
        merged.update(incoming)
        return merged
    return incoming if incoming not in (None, "", {}, []) else previous


_SOURCE_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "locations": (
        "location",
        "locations",
        "locationsText",
        "offices",
        "allLocations",
        "secondaryLocations",
        "additionalLocations",
    ),
    "location": (
        "location",
        "locations",
        "locationsText",
        "offices",
        "allLocations",
        "secondaryLocations",
        "additionalLocations",
    ),
    "apply_url": (
        "apply_url",
        "applyUrl",
        "absolute_url",
        "absoluteUrl",
        "hostedUrl",
        "jobUrl",
        "externalUrl",
        "url",
    ),
    "url": (
        "apply_url",
        "applyUrl",
        "absolute_url",
        "absoluteUrl",
        "hostedUrl",
        "jobUrl",
        "externalUrl",
        "url",
    ),
    "published_at": (
        "published_at",
        "publishedAt",
        "first_published",
        "firstPublished",
        "firstPublishedAt",
        "publicationDate",
        "publication_date",
    ),
    "source_updated_at": (
        "source_updated_at",
        "sourceUpdatedAt",
        "updated_at",
        "updatedAt",
        "lastUpdated",
        "lastUpdatedAt",
        "lastUpdatedDate",
        "modified_at",
        "modifiedAt",
    ),
    "application_deadline": (
        "application_deadline",
        "applicationDeadline",
        "deadline",
        "deadlineDate",
        "endDate",
        "end_date",
    ),
    "endDate": ("endDate", "end_date"),
    "end_date": ("endDate", "end_date"),
}


def _source_value_empty(value: object) -> bool:
    """Whether a source field explicitly carries no usable value."""

    if value is None or value == "":
        return True
    if isinstance(value, Mapping):
        return not any(not _source_value_empty(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return not any(not _source_value_empty(item) for item in value)
    return False


def _source_fact_mappings(value: object) -> list[Mapping[str, Any]]:
    """Expose a raw fact object and one level of nested detail facts."""

    if not isinstance(value, Mapping):
        return []
    mappings = [value]
    mappings.extend(item for item in value.values() if isinstance(item, Mapping))
    return mappings


def _explicit_source_empty(posting: Mapping[str, Any], field: str) -> bool:
    """Distinguish an explicitly empty source field from an omitted field."""

    aliases = _SOURCE_FIELD_ALIASES.get(field)
    if not aliases:
        return False
    facts = _source_fact_mappings(posting.get("source_facts"))
    for mapping in facts:
        present = [mapping[key] for key in aliases if key in mapping]
        if present and all(_source_value_empty(value) for value in present):
            return True
    return False


def _clear_related_field(merged: dict[str, Any], key: str, value: object) -> None:
    """Clear the legacy/structured sibling when a source removed a fact."""

    merged[key] = value
    if key == "locations":
        merged["location"] = ""
    elif key == "location":
        merged["locations"] = []
    elif key == "apply_url":
        merged["url"] = ""
    elif key == "url":
        merged["apply_url"] = None


def _observation(
    posting: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    observed_at: str,
    status: str | None = None,
    verification_status: str | None = None,
    update_lifecycle: bool = True,
) -> dict:
    """Merge one normalized source observation with its prior current row."""

    if not isinstance(posting.get("key"), str) or not posting["key"]:
        raise ValueError("Posting is missing a non-empty string key")
    if previous is None:
        merged = dict(posting)
    else:
        merged = dict(previous)
        for key, value in posting.items():
            # Listing responses often omit a detail description.  An existing
            # full description is stronger evidence and must survive a failed
            # or bounded detail refresh.
            if key in {"description_html", "description_kind"} and value in (None, "", "missing", "snippet"):
                continue
            if key == "source_facts":
                merged[key] = _merge_source_facts(previous.get(key), value)
                continue
            if value in (None, "", [], {}) and _explicit_source_empty(posting, key):
                _clear_related_field(merged, key, value)
                continue
            if value not in (None, "", [], {}):
                merged[key] = value
            elif key not in merged:
                merged[key] = value
        prior_description = previous.get("description_html")
        prior_kind = previous.get("description_kind")
        incoming_description = posting.get("description_html")
        incoming_kind = posting.get("description_kind")
        if (
            prior_description
            and prior_kind == "full"
            and (not incoming_description or incoming_kind in {"missing", "snippet"})
        ):
            merged["description_html"] = prior_description
            merged["description_kind"] = "full"
        elif not merged.get("description_kind"):
            merged["description_kind"] = "full" if merged.get("description_html") else "missing"

    # First discovery is immutable even though the effective row moves with
    # each appended observation.  ``discovered_at`` is the legacy spelling and
    # is retained for old readers.
    merged["first_seen_at"] = _first_seen(previous, posting, observed_at)
    if previous and isinstance(previous.get("discovered_at"), str):
        merged["discovered_at"] = previous["discovered_at"]
    else:
        merged.setdefault("discovered_at", merged["first_seen_at"])

    if update_lifecycle:
        if status is not None:
            if status not in LIFECYCLE_STATES:
                raise ValueError(f"unknown listing status: {status!r}")
            merged["listing_status"] = status
        else:
            merged["listing_status"] = _valid_status(merged.get("listing_status")) or "unknown"
        if merged["listing_status"] == "unknown":
            # ``last_verified_at`` means the last source observation that gave us a
            # trustworthy open/closed state.  An unavailable exact check is still
            # recorded, but it must not make old content look freshly verified.
            if previous and isinstance(previous.get("last_verified_at"), str):
                merged["last_verified_at"] = previous["last_verified_at"]
            else:
                merged.pop("last_verified_at", None)
            merged["last_attempt_at"] = observed_at
        else:
            merged["last_verified_at"] = observed_at
    elif previous is not None:
        # A failed detail GET says nothing about whether a stored listing is
        # still present.  Preserve its listing lifecycle clocks while recording
        # the detail attempt and any changed description/source facts.  A
        # successful exact detail observation uses the normal lifecycle path
        # and can establish the listing as open.
        merged["listing_status"] = _valid_status(previous.get("listing_status")) or "unknown"
        for field in ("last_verified_at", "last_attempt_at"):
            if field in previous:
                merged[field] = previous[field]
            else:
                merged.pop(field, None)
    elif status is not None:
        if status not in LIFECYCLE_STATES:
            raise ValueError(f"unknown listing status: {status!r}")
        merged["listing_status"] = status
    if verification_status is not None:
        merged["verification_status"] = verification_status
    if merged.get("verification_status") == "verified":
        merged.pop("verification_error", None)
    if merged.get("detail_verification_status") == "verified":
        merged.pop("detail_verification_error", None)
    return merged


def _daily_output_parts(postings_dir: Path, day: str) -> list[Path]:
    """Return today's base file and parts in the store's lexical read order."""

    base = postings_dir / f"{day}.jsonl"
    parts = sorted(postings_dir.glob(f"{day}_????.jsonl"))
    return ([base] if base.exists() else []) + parts


def _next_output_part(postings_dir: Path, day: str, existing: Sequence[Path]) -> Path:
    if not existing:
        return postings_dir / f"{day}.jsonl"
    last = existing[-1]
    match = re.fullmatch(rf"{re.escape(day)}_(\d{{4}})\.jsonl", last.name)
    number = int(match.group(1)) + 1 if match else 2
    return postings_dir / f"{day}_{number:04d}.jsonl"


def _append_rows(
    postings_dir: Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    previous: dict[str, dict] | None = None,
) -> int:
    if not rows:
        return 0
    postings_dir.mkdir(parents=True, exist_ok=True)
    previous = latest_postings(postings_dir) if previous is None else previous
    day = date.today().isoformat()
    parts = _daily_output_parts(postings_dir, day)
    output = parts[-1] if parts else _next_output_part(postings_dir, day, parts)
    size = output.stat().st_size if output.exists() else 0
    destination = None
    try:
        for row in rows:
            stored = dict(row)
            prior = previous.get(row["key"])
            if prior is not None:
                changes = {
                    key: value
                    for key, value in row.items()
                    if key != "key" and (key not in prior or value != prior[key])
                }
                removed = sorted(set(prior) - set(row) - {"key"})
                stored = {"key": row["key"], "_observation": changes}
                if removed:
                    stored["_remove"] = removed
            encoded = (json.dumps(stored, ensure_ascii=False) + "\n").encode("utf-8")
            if len(encoded) > POSTING_PART_MAX_BYTES:
                raise ValueError(
                    f"Posting {row['key']!r} exceeds the per-file Posting store limit"
                )
            if size and size + len(encoded) > POSTING_PART_MAX_BYTES:
                if destination is not None:
                    destination.close()
                    destination = None
                if not parts or parts[-1] != output:
                    parts.append(output)
                output = _next_output_part(postings_dir, day, parts)
                size = output.stat().st_size if output.exists() else 0
            if destination is None:
                destination = output.open("ab")
            destination.write(encoded)
            size += len(encoded)
            previous[row["key"]] = dict(row)
    finally:
        if destination is not None:
            destination.close()
    return len(rows)


@dataclass(frozen=True)
class RefreshSummary:
    """Counts and status from one board refresh."""

    source: str
    board: str
    status: str
    observed: int
    new: int
    refreshed: int
    unknown: int
    closed: int

    @property
    def appended(self) -> int:
        return self.new + self.refreshed + self.unknown + self.closed


def refresh_postings(
    postings_dir: Path,
    postings: Sequence[Mapping[str, Any]],
    *,
    source: str | None = None,
    board: str | None = None,
    complete: bool = True,
    status: str = "ok",
    observed_at: str | None = None,
    closed_keys: Iterable[str] = (),
    closed_postings: Sequence[Mapping[str, Any]] = (),
    detail_only_keys: Iterable[str] = (),
    error: str | None = None,
) -> RefreshSummary:
    """Append a board observation while preserving first discovery.

    ``complete=True`` means the source established a trustworthy snapshot for
    this exact board.  Keys absent from that snapshot become ``unknown``; they
    never become ``closed`` from age or disappearance alone.  A failed or
    partial board leaves absent keys untouched.  Only ``closed_keys`` or an
    explicitly closed incoming observation can close a Posting.  Keys in
    ``detail_only_keys`` receive detail/retry metadata while retaining their
    listing lifecycle clocks; successful exact detail observations should be
    omitted from that set so they can establish an open listing.
    """

    status = status.casefold() if isinstance(status, str) else "failed"
    if status not in HEALTH_STATES:
        raise ValueError(f"unknown source health status: {status!r}")
    if not complete and status == "ok":
        status = "partial"
    observed_at = observed_at or _now()
    current = latest_postings(postings_dir)
    incoming: list[Mapping[str, Any]] = list(postings)
    all_rows = incoming + list(closed_postings)

    if source is None or board is None:
        pairs = {
            (str(item.get("source", "")), str(item.get("board", "")))
            for item in all_rows
            if item.get("source") and item.get("board")
        }
        if len(pairs) != 1:
            raise ValueError("refresh_postings needs source and board for an empty or mixed batch")
        source, board = next(iter(pairs))
    assert source is not None and board is not None

    board_prefix = f"{source}:{board}:"
    observed_keys: set[str] = set()
    rows: list[dict] = []
    new_count = refreshed_count = unknown_count = closed_count = 0
    explicit_closed = set(closed_keys)
    detail_only = {key for key in detail_only_keys if isinstance(key, str) and key}
    explicit_closed.update(
        key
        for item in closed_postings
        for key in [item.get("key")]
        if isinstance(key, str) and key
    )

    for item in incoming:
        key = item.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("Posting is missing a non-empty string key")
        # Do not let a caller accidentally mark another board as observed.
        if item.get("source") and item.get("board") and (
            item.get("source") != source or item.get("board") != board
        ):
            raise ValueError(f"Posting {key!r} does not belong to {source}:{board}")
        observed_keys.add(key)
        prior = current.get(key)
        explicit_status = _valid_status(item.get("listing_status"))
        if key in explicit_closed or explicit_status == "closed":
            state = "closed"
        elif complete and status == "ok":
            # A complete successful direct board establishes presence even
            # when the vendor does not expose a separate status field.
            state = "open"
        else:
            state = explicit_status or "open"
        row = _observation(
            item,
            prior,
            observed_at=observed_at,
            status=state,
            update_lifecycle=key not in detail_only,
        )
        rows.append(row)
        if state == "closed":
            closed_count += 1
        elif prior is None:
            new_count += 1
        else:
            refreshed_count += 1

    for item in closed_postings:
        key = item.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("closed Posting is missing a non-empty string key")
        explicit_closed.add(key)
        observed_keys.add(key)
        prior = current.get(key)
        row = _observation(item, prior, observed_at=observed_at, status="closed")
        row["closure_reason"] = row.get("closure_reason") or "definitive exact-job verification"
        rows.append(row)
        closed_count += 1

    # A key may be supplied without a complete Posting when an exact detail
    # request returned 404/410.  Preserve its previous content and close it.
    for key in explicit_closed:
        if key in observed_keys:
            continue
        prior = current.get(key)
        if prior is None:
            continue
        if not (prior.get("source") == source and prior.get("board") == board):
            continue
        row = _observation(
            {"key": key, "source": source, "board": board},
            prior,
            observed_at=observed_at,
            status="closed",
        )
        row["closure_reason"] = "definitive exact-job verification"
        rows.append(row)
        observed_keys.add(key)
        closed_count += 1

    if complete and status == "ok":
        for key, prior in current.items():
            if not key.startswith(board_prefix) or key in observed_keys:
                continue
            # A prior definitive closure remains closed even when a later full
            # board snapshot does not list the job.  All other missing records
            # become unknown, never an age-based closure.
            prior_state = _valid_status(prior.get("listing_status"))
            state = "closed" if prior_state == "closed" else "unknown"
            row = _observation(prior, prior, observed_at=observed_at, status=state)
            rows.append(row)
            if state == "closed":
                closed_count += 1
            else:
                unknown_count += 1

    _append_rows(postings_dir, rows)
    return RefreshSummary(
        source=source,
        board=board,
        status=status,
        observed=len(incoming),
        new=new_count,
        refreshed=refreshed_count,
        unknown=unknown_count,
        closed=closed_count,
    )


def append_detail_observations(
    postings_dir: Path,
    postings: Sequence[Mapping[str, Any]],
    *,
    closed_keys: Iterable[str] = (),
    detail_only_keys: Iterable[str] = (),
    observed_at: str | None = None,
) -> int:
    """Append mixed-board exact detail results with one store read.

    Successful exact details establish an open Posting. Failed details preserve
    listing lifecycle clocks, and definitive closures close only their exact
    Posting. This is the batch boundary used by the paced Workday drain.
    """

    observed_at = observed_at or _now()
    current = latest_postings(postings_dir)
    closed = set(closed_keys)
    detail_only = set(detail_only_keys)
    rows: list[dict] = []
    for item in postings:
        key = item.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("Posting is missing a non-empty string key")
        prior = current.get(key)
        state = "closed" if key in closed else (_valid_status(item.get("listing_status")) or "open")
        row = _observation(
            item,
            prior,
            observed_at=observed_at,
            status=state,
            update_lifecycle=key not in detail_only,
        )
        if state == "closed":
            row["closure_reason"] = row.get("closure_reason") or "definitive exact-job verification"
        rows.append(row)
    return _append_rows(postings_dir, rows, previous=current)


def append_observations(
    postings_dir: Path,
    postings: Sequence[Mapping[str, Any]],
    **kwargs: Any,
) -> int:
    """Append current observations and return the number of rows written.

    ``refresh_postings`` is the richer API for callers that need counts.  This
    compact wrapper is useful to a preparation stage that only needs to record
    the exact-posting result after checking it.
    """

    # This compact API records exact-posting observations.  A caller that has
    # a complete board snapshot should use ``refresh_postings(...,
    # complete=True)`` explicitly; one exact check must not mark every sibling
    # job missing from that check as unknown.
    kwargs.setdefault("complete", False)
    kwargs.setdefault("status", "partial")
    return refresh_postings(postings_dir, postings, **kwargs).appended


def _concise_error(error: object) -> str | None:
    if error in (None, ""):
        return None
    value = " ".join(str(error).split())
    return value[:300]


def source_health_path(postings_dir: Path) -> Path:
    return postings_dir / "source-health.json"


def load_source_health(postings_dir: Path) -> dict[str, dict[str, Any]]:
    path = source_health_path(postings_dir)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid source health JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"source health at {path} must be a JSON object")
    return {str(key): dict(item) for key, item in value.items() if isinstance(item, dict)}


def update_source_health(
    postings_dir: Path,
    source_board: str,
    *,
    status: str,
    count: int = 0,
    error: object = None,
    attempted_at: str | None = None,
    successful_at: str | None = None,
) -> dict[str, Any]:
    """Record one source attempt in ``source-health.json``."""

    status = status.casefold()
    if status not in HEALTH_STATES:
        raise ValueError(f"unknown source health status: {status!r}")
    health = load_source_health(postings_dir)
    previous = health.get(source_board, {})
    attempted_at = attempted_at or _now()
    entry: dict[str, Any] = {
        "status": status,
        "last_attempt_at": attempted_at,
        "last_success_at": previous.get("last_success_at"),
        "count": max(0, int(count)),
        "message": _concise_error(error),
    }
    if status == "ok":
        entry["last_success_at"] = successful_at or attempted_at
        entry["message"] = None
    health[source_board] = entry
    postings_dir.mkdir(parents=True, exist_ok=True)
    path = source_health_path(postings_dir)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as destination:
            destination.write(json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return entry


def posting_revision(posting: Mapping[str, Any]) -> str:
    """Hash meaningful job content, eligibility, and lifecycle state.

    Observation clocks are deliberately excluded so rechecking an unchanged
    board does not invalidate a matching decision.  Source update timestamps,
    descriptions, locations, eligibility fields, and ``listing_status`` remain
    in the hash because each can change what a person should see or apply to.
    """

    def canonical_key(key: object) -> str:
        value = str(key)
        value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
        return value.casefold()

    def exclude(key: object) -> bool:
        normalized = canonical_key(key)
        if normalized in OBSERVATION_TIMES or normalized in VERIFICATION_BOOKKEEPING:
            return True
        # Preserve source timestamps such as ``updatedAt`` because they are
        # meaningful content evidence, while dropping any vendor-specific
        # verification clocks/status/error fields nested in source_facts.
        if "verification" in normalized and (
            normalized.endswith(("_at", "_date", "_timestamp", "_status", "_error", "_reason"))
            or normalized.startswith("verification_")
        ):
            return True
        return normalized in {"revision", "posting_revision"}

    def scrub(value: object) -> object:
        if isinstance(value, Mapping):
            return {
                key: scrub(item)
                for key, item in value.items()
                if not exclude(key)
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [scrub(item) for item in value]
        return value

    meaningful = scrub(posting)
    if (
        isinstance(meaningful, dict)
        and meaningful.get("description_html")
        and not meaningful.get("description_kind")
    ):
        meaningful["description_kind"] = "full"
    payload = json.dumps(
        meaningful,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
