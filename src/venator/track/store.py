"""Append-only TrackEvent storage and deterministic lifecycle folding."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

from venator.match.store import iter_jsonl
from venator.profile.claim import (
    STORE_OWNER_FILE,
    claim_store,
    report_stray_rows,
    rows_from_other_profiles,
    store_owner,
)

#: The Track store records its owning Profile in the same file the decisions
#: store does (``venator.match.store.DECISIONS_OWNER_FILE``), with the same
#: opaque identifier, through the same mechanism (``venator.profile.claim``).
TRACK_OWNER_FILE = STORE_OWNER_FILE

EVENT_ACTORS = {
    "approve": "owner",
    "reject": "owner",
    "fill": "pipeline",
    "submit": "owner",
    "prepare": "pipeline",
    "restore": "owner",
    "outcome": "owner",
    "withdraw": "owner",
}
EVENT_STATES = {
    "approve": "approved",
    "reject": "rejected",
    "fill": "filled",
    "submit": "submitted",
    "prepare": "prepared",
    "restore": "queued",
    "outcome": "concluded",
    "withdraw": "withdrawn",
}
OUTCOMES = frozenset({"interview", "offer", "rejected", "no_response"})
DETAIL_EVENTS = frozenset({"fill", "submit", "outcome", "prepare"})
STATE_ORDER = (
    "queued",
    "approved",
    "prepared",
    "filled",
    "submitted",
    "concluded",
    "rejected",
    "withdrawn",
)
_EVENT_FIELDS = {"posting_key", "event", "actor", "detail", "at"}
#: ``profile_id`` records which Profile recorded a TrackEvent, exactly as it does
#: on a Filter Decision (``coordination/CONTRACTS.md``). It is **optional, and a
#: missing value means unknown** — never a mismatch, never a default Profile:
#: every event written before the field existed lacks it, ``data/`` is
#: append-only (ADR-0001), and a reader that treated absence as anything else
#: would condemn every event already recorded. An explicit ``null`` reads the
#: same way, so a store cannot be made unloadable by one row.
_OPTIONAL_EVENT_FIELDS = {"profile_id"}


def _validate_at(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.at must be a non-empty UTC ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label}.at must be a valid UTC ISO timestamp") from error
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label}.at must use UTC")


def validate_event(value: object, *, label: str = "TrackEvent") -> dict:
    """Validate one stored TrackEvent against the shared contract."""
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if not _EVENT_FIELDS <= set(value) or not set(value) <= _EVENT_FIELDS | _OPTIONAL_EVENT_FIELDS:
        missing = sorted(_EVENT_FIELDS - set(value), key=str)
        extra = sorted(set(value) - _EVENT_FIELDS - _OPTIONAL_EVENT_FIELDS, key=str)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if extra:
            details.append(f"unexpected {extra}")
        raise ValueError(f"{label} has invalid fields: {', '.join(details)}")

    posting_key = value["posting_key"]
    event = value["event"]
    actor = value["actor"]
    detail = value["detail"]
    if not isinstance(posting_key, str) or not posting_key.strip():
        raise ValueError(f"{label}.posting_key must be a non-empty string")
    if not isinstance(event, str) or event not in EVENT_ACTORS:
        raise ValueError(f"{label}.event must be one of {sorted(EVENT_ACTORS)}")
    expected_actor = EVENT_ACTORS[event]
    legacy_submission = event == "submit" and actor == "pipeline"
    if actor != expected_actor and not legacy_submission:
        raise ValueError(f"{label}.actor must be {expected_actor!r} for event {event!r}")
    if event in DETAIL_EVENTS:
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError(f"{label}.detail is required for event {event!r}")
        if event == "outcome" and detail not in OUTCOMES:
            raise ValueError(f"{label}.detail must be one of {sorted(OUTCOMES)} for outcome")
    elif detail is not None:
        raise ValueError(f"{label}.detail must be null for event {event!r}")
    written_by = value.get("profile_id")
    if written_by is not None and (not isinstance(written_by, str) or not written_by.strip()):
        raise ValueError(f"{label}.profile_id must be a non-empty string when present")
    _validate_at(value["at"], label)
    return value


def load_events(track_dir: Path) -> list[dict]:
    events = list(iter_jsonl(track_dir))
    for index, event in enumerate(events):
        validate_event(event, label=f"TrackEvent[{index}]")
    return events


def append_events(track_dir: Path, events: Iterable[dict], *, day: date | None = None) -> int:
    """Validate and append a batch of TrackEvents; return the number written."""
    values = list(events)
    for index, event in enumerate(values):
        validate_event(event, label=f"TrackEvent[{index}]")
    if not values:
        return 0
    track_dir.mkdir(parents=True, exist_ok=True)
    output = track_dir / f"{(day or date.today()).isoformat()}.jsonl"
    with output.open("a", encoding="utf-8", newline="\n") as destination:
        for event in values:
            destination.write(json.dumps(event, ensure_ascii=False) + "\n")
    return len(values)


def events_from_other_profiles(track_dir: Path, identifier: str) -> dict[str, int]:
    """Count appended TrackEvents that name a Profile other than ``identifier``.

    Read with ``iter_jsonl`` rather than ``load_events`` on purpose: this is the
    diagnostic, and a store worth diagnosing is exactly the one whose contents
    may not validate. An event with no ``profile_id`` is unknown, never a
    mismatch.
    """
    return rows_from_other_profiles(iter_jsonl(track_dir), identifier)


def track_dir_owner(track_dir: Path) -> str | None:
    """The Profile identifier a Track store is claimed by, or None if unclaimed.

    An empty stamp reads as unclaimed rather than as a Profile with a blank
    name: a blank identifier would match nothing and refuse everything.
    """
    return store_owner(track_dir)


def _foreign_track_store(track_dir: Path, owner: str, identifier: str) -> ValueError:
    """The one refusal, for reading this store and for writing to it alike.

    ``match.store`` words its two refusals separately because the claim and the
    verify are reached from different commands there. The Track store is not
    like that: the fix is the same whether the command reads it or appends to
    it, so the message is one message and names both.
    """
    stamp = track_dir / TRACK_OWNER_FILE
    return ValueError(
        f"{track_dir} holds the Application lifecycle for Profile {owner!r}, and effective "
        f"state is keyed by Posting alone — using it as {identifier!r} would report that "
        "Profile's approvals, rejections and outcomes as this one's, and append this Profile's "
        "TrackEvents into that Profile's history, where the state of a Posting is whichever "
        "Profile acted on it last. Point at this Profile's own store, e.g. --track-dir "
        f"data/{identifier}/track (or delete {stamp} if {owner!r} was renamed)"
    )


def claim_track_dir(track_dir: Path, identifier: str) -> None:
    """Refuse to interleave two Profiles' TrackEvents in one Track store.

    The Track store fails open harder than the decisions store did. A Filter
    Decision is at least keyed by (Posting, stage); ``fold_states`` keys a
    lifecycle state by ``posting_key`` **alone**, and the last event for a
    Posting wins outright. Two Profiles sharing one ``--track-dir`` therefore
    do not merely mix — one Owner's ``reject`` becomes the other Owner's
    Posting's state, with nothing on screen to say a second Profile was ever
    involved. Both Profiles can have correctly separated, correctly claimed
    decisions stores and still land in it, because nothing about a Posting key
    names a Profile.

    So the directory records the Profile that owns it and refuses another, and
    **the stamp is the enforcement** — the same shape ``match.store`` uses for
    ``data/decisions/`` (``venator.profile.claim``), not a second mechanism.
    The identifier compared is the Profile's opaque ``profile_id``, not its
    name, so renaming ``profiles/<name>/`` does not lock the Owner out of their
    own lifecycle history.

    The effective-state key stays ``posting_key``. Adding the Profile to it
    would silently resurrect superseded events the moment one directory did
    hold two Profiles, which is the outcome this refusal exists to prevent.

    Claiming is the last thing before a write, never the first thing in a
    command. ``track.record`` calls ``verify_track_dir`` up front — a read-only
    check, so refusing costs nothing and touches nothing — and stamps the store
    here, once an event has passed validation and is about to be appended. A refused ``track.record`` therefore leaves the Track store
    byte-identical, and an absent one uncreated, which is the property
    ``tests/track/test_cli.py`` has pinned since the stage existed.

    This refuses a foreign store too, so it is safe on its own, but it does not
    report stray events: that is ``verify_track_dir``'s job, every write path
    verifies before it claims, and one command saying it twice is noise rather
    than a second finding. Nothing is enforced on a stray event either way — no
    event is dropped, filtered or rewritten, and the fold still reads every one
    of them (ADR-0001).
    """
    owner = track_dir_owner(track_dir)
    if owner is None:
        claim_store(track_dir, identifier)
    elif owner != identifier:
        raise _foreign_track_store(track_dir, owner, identifier)


def verify_track_dir(track_dir: Path, identifier: str) -> None:
    """Refuse to *read* one Profile's Application lifecycle as another's.

    The same mechanism as the claim, with the read path's three properties
    (``venator.profile.claim``):

    **A read never claims.** ``track.status`` and ``view.build`` leave an
    unstamped store unstamped; deciding whose Track store a directory is, as a
    side effect of printing a status, is not a read.

    **An unclaimed store is unknown, not a mismatch.** No stamp, an empty one,
    a whitespace-only one, or no directory at all: the read proceeds. A Track
    store that predates this guard keeps being readable, with nothing to
    migrate.

    **Events are evidence, never the lock.** An event naming another Profile is
    reported and nothing more.
    """
    owner = track_dir_owner(track_dir)
    if owner is not None and owner != identifier:
        raise _foreign_track_store(track_dir, owner, identifier)
    _report_stray_events(track_dir, identifier)


def _report_stray_events(track_dir: Path, identifier: str) -> None:
    """Say out loud that a Track store holds another Profile's events.

    The owner is re-read rather than assumed, because an *unclaimed* store is
    the ordinary case on the read path: describing one as claimed would tell
    the person checking the opposite of the truth.
    """
    report_stray_rows(
        track_dir,
        identifier,
        events_from_other_profiles(track_dir, identifier),
        owner=track_dir_owner(track_dir),
        flag="--track-dir",
    )


def _still_queued(posting_key: str, latest_decisions: Mapping[tuple[str, str], dict]) -> bool:
    """Whether a historical queued ``llm_score`` still stands, given the hard filters.

    A queue entry is derived from the latest ``llm_score``, but that is not the
    only decision the Posting has. Dedup runs at ``hard_filter`` and the corpus
    grows underneath a historical score: a Posting queued before is killed as a
    duplicate later, when the employer's own record of the same job arrives.
    Reading the score alone would leave it queued for good, because nothing
    writes an ``llm_score`` row any more.

    So the latest hard-filter decision has a veto, which is the ladder the
    dashboard's own status column already uses (``ui/server/queries.ts`` tests
    ``hf.verdict = 'kill'`` before it ever looks at the score).
    """
    hard_filter = latest_decisions.get((posting_key, "hard_filter"))
    return hard_filter is None or hard_filter.get("verdict") != "kill"


def fold_states(
    events: Iterable[dict],
    latest_decisions: Mapping[tuple[str, str], dict],
) -> dict[str, dict]:
    """Fold derived queue entries and TrackEvents into effective Posting states."""
    states: dict[str, dict] = {}
    queued = sorted(
        (
            posting_key,
            decision,
        )
        for (posting_key, stage), decision in latest_decisions.items()
        if stage == "llm_score"
        and decision.get("verdict") == "queue"
        and _still_queued(posting_key, latest_decisions)
    )
    for posting_key, decision in queued:
        states[posting_key] = {
            "posting_key": posting_key,
            "state": "queued",
            "detail": None,
            "since": decision.get("decided_at"),
        }

    for index, event in enumerate(events):
        validate_event(event, label=f"TrackEvent[{index}]")
        posting_key = event["posting_key"]
        states[posting_key] = {
            "posting_key": posting_key,
            "state": EVENT_STATES[event["event"]],
            "detail": event["detail"],
            "since": event["at"],
        }
    return states
