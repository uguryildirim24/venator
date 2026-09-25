"""Durable cursors for bounded source scans.

The Posting corpus is append-only, while a large Workday board cannot be read
in one run.  This module stores the *next listing offset* separately from the
corpus.  A cursor is a resume hint, never a snapshot claim: callers must use a
partial refresh while walking it because Workday can reorder jobs between
requests and between runs.

The file is a small JSON object keyed by ``source:board``.  Writes are atomic
and replace the complete metadata file after the corresponding Posting rows
have been committed.  If a process dies before that replacement, the previous
offset is retried; retrying a page is safe because Posting identity is stable
and the store de-duplicates observations.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROGRESS_FILENAME = "source-progress.json"
PROGRESS_VERSION = 1
PROGRESS_STATES = frozenset({"new", "partial", "complete", "stalled", "failed"})
_UNSET = object()


def source_progress_path(postings_dir: Path) -> Path:
    """Return the durable source-progress path beside the JSONL corpus."""

    return postings_dir / PROGRESS_FILENAME


def _default(source_board: str) -> dict[str, Any]:
    source, separator, board = source_board.partition(":")
    if not separator or not source or not board:
        raise ValueError("source progress key must read 'source:board'")
    return {
        "version": PROGRESS_VERSION,
        "source": source,
        "board": board,
        "next_offset": 0,
        "page_limit": 20,
        "status": "new",
        "last_attempt_at": None,
        "last_progress_at": None,
        "last_error": None,
        "last_page_signature": None,
    }


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"source progress field {field!r} must be an integer >= {minimum}")
    return value


def _validate(source_board: str, value: Mapping[str, Any]) -> dict[str, Any]:
    expected = _default(source_board)
    result = dict(expected)
    # Keep the on-disk contract deliberately small. Unknown keys from an
    # earlier experiment or a newer writer are ignored rather than copied
    # forward on the next atomic update.
    result.update({key: value[key] for key in expected if key in value})
    if result.get("version") != PROGRESS_VERSION:
        raise ValueError(f"unsupported source progress version for {source_board!r}")
    if result.get("source") != expected["source"] or result.get("board") != expected["board"]:
        raise ValueError(f"source progress entry {source_board!r} has mismatched source or board")
    for field in ("next_offset", "page_limit"):
        result[field] = _integer(result.get(field), field, minimum=1 if field == "page_limit" else 0)
    status = result.get("status")
    if not isinstance(status, str) or status not in PROGRESS_STATES:
        raise ValueError(f"source progress field 'status' is invalid for {source_board!r}")
    for field in (
        "last_attempt_at", "last_progress_at",
        "last_error", "last_page_signature",
    ):
        item = result.get(field)
        if item is not None and not isinstance(item, str):
            raise ValueError(f"source progress field {field!r} must be a string or null")
    return result


def load_source_progress(postings_dir: Path) -> dict[str, dict[str, Any]]:
    """Read and validate all persisted source cursors.

    A malformed cursor raises instead of silently starting at an arbitrary
    offset.  The safe recovery is to remove or repair this small metadata file;
    the append-only Posting corpus is untouched.
    """

    path = source_progress_path(postings_dir)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid source progress JSON at {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"source progress at {path} must be a JSON object")
    result: dict[str, dict[str, Any]] = {}
    for source_board, entry in value.items():
        if not isinstance(source_board, str) or not isinstance(entry, Mapping):
            raise ValueError(f"source progress at {path} contains an invalid entry")
        result[source_board] = _validate(source_board, entry)
    return result


def get_source_progress(postings_dir: Path, source_board: str) -> dict[str, Any]:
    """Return one validated cursor, or a zero-offset cursor on first use."""

    return load_source_progress(postings_dir).get(source_board, _default(source_board))


def resume_offset(
    progress: Mapping[str, Any],
    *,
    now: datetime | None = None,
    expires_after: timedelta | None = None,
) -> tuple[int, bool]:
    """Return ``(offset, expired)`` for a cursor that may have gone stale.

    Cursors do not expire by default: resetting a large board before it reaches
    its reported total can keep its tail permanently unseen. A caller may still
    supply an expiry for a diagnostic or migration; completed scans explicitly
    return to offset zero.
    """

    offset = progress.get("next_offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("source progress field 'next_offset' must be an integer >= 0")
    if offset == 0 or expires_after is None:
        return offset, False
    stamp = progress.get("last_progress_at")
    if not isinstance(stamp, str):
        return 0, True
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 0, True
    if parsed.tzinfo is None:
        return 0, True
    current = now or datetime.now(timezone.utc)
    if parsed > current:
        return 0, True
    if current - parsed >= expires_after:
        return 0, True
    return offset, False


def _write(postings_dir: Path, value: Mapping[str, Mapping[str, Any]]) -> None:
    postings_dir.mkdir(parents=True, exist_ok=True)
    path = source_progress_path(postings_dir)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as destination:
            destination.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def update_source_progress(
    postings_dir: Path,
    source_board: str,
    *,
    next_offset: int | object = _UNSET,
    page_limit: int | object = _UNSET,
    status: str | object = _UNSET,
    last_attempt_at: str | None | object = _UNSET,
    last_progress_at: str | None | object = _UNSET,
    last_error: str | None | object = _UNSET,
    last_page_signature: str | None | object = _UNSET,
) -> dict[str, Any]:
    """Merge one progress event and atomically persist the resulting cursor.

    ``next_offset`` should only move after the Posting observations for the
    corresponding pages have been appended.  A failed request can update
    ``status`` and ``last_error`` while leaving the offset untouched.
    """

    all_progress = load_source_progress(postings_dir)
    current = all_progress.get(source_board, _default(source_board))
    updates = {
        "next_offset": next_offset,
        "page_limit": page_limit,
        "status": status,
        "last_attempt_at": last_attempt_at,
        "last_progress_at": last_progress_at,
        "last_error": last_error,
        "last_page_signature": last_page_signature,
    }
    current.update({key: value for key, value in updates.items() if value is not _UNSET})
    current = _validate(source_board, current)
    all_progress[source_board] = current
    _write(postings_dir, all_progress)
    return current


__all__ = [
    "PROGRESS_FILENAME",
    "PROGRESS_STATES",
    "get_source_progress",
    "load_source_progress",
    "resume_offset",
    "source_progress_path",
    "update_source_progress",
]
