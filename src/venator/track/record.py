"""Validate and append one TrackEvent.

Usage:
    python -m venator.track.record <event> <posting_key> [--detail ...] [--profile NAME]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from venator.match.store import load_postings, verify_decisions_dir
from venator.paths import STORE_HELP, resolve_store_paths
from venator.profile import ProfileError, add_profile_argument, profile_from_arguments
from venator.track.store import (
    DETAIL_EVENTS,
    EVENT_ACTORS,
    OUTCOMES,
    append_events,
    claim_track_dir,
    verify_track_dir,
)


def validate_detail(event: str, detail: str | None) -> str | None:
    if event in DETAIL_EVENTS:
        if detail is None or not detail.strip():
            raise ValueError(f"--detail is required for event {event!r}")
        normalized = detail.strip()
        if event == "outcome" and normalized not in OUTCOMES:
            raise ValueError(f"outcome detail must be one of {sorted(OUTCOMES)}")
        return normalized
    if detail is not None:
        raise ValueError(f"--detail is not allowed for event {event!r}")
    return None


def record_event(
    event: str,
    posting_key: str,
    detail: str | None = None,
    *,
    postings_dir: Path | None = None,
    decisions_dir: Path | None = None,
    track_dir: Path | None = None,
    identifier: str | None = None,
) -> dict:
    """Validate one TrackEvent and append it.

    Any known event may follow any state: a person's application history can
    begin outside Venator, and a save, a correction or an undo needs no prior
    verdict.

    ``identifier`` is the Profile this event belongs to, and **both** stores of
    the Profile are checked against it before anything is appended.

    The decisions store, so a command pointed at another Profile's stores is
    refused whichever of the two it names.

    The Track store, because that check alone left the other half open: two
    Profiles with correctly separated, correctly claimed decisions stores could
    still be pointed at one ``--track-dir``, and ``fold_states`` keys a state by
    ``posting_key`` alone. One Owner's ``reject`` then became the other Owner's
    Posting's state, silently. So the Track store is guarded by the same
    mechanism the Match write path uses on ``data/decisions/``, and it is
    guarded **here, before anything is appended**.

    The refusal is a read; the stamp is a write, and it happens last, once the
    event has survived every check and is about to be appended. A refused
    ``track.record`` leaves the Track store byte-identical, an unclaimed one
    uncreated.

    ``None`` skips both, for callers with no Profile to hand: absent means "not
    configured", never "invalid".
    """
    stores = resolve_store_paths(
        postings_dir=postings_dir, decisions_dir=decisions_dir, track_dir=track_dir
    )
    postings_dir = stores["postings_dir"]
    decisions_dir = stores["decisions_dir"]
    track_dir = stores["track_dir"]
    if identifier is not None:
        verify_decisions_dir(decisions_dir, identifier)
        verify_track_dir(track_dir, identifier)
    if event not in EVENT_ACTORS:
        raise ValueError(f"event must be one of {sorted(EVENT_ACTORS)}")
    if not isinstance(posting_key, str) or not posting_key.strip():
        raise ValueError("posting_key must be a non-empty string")
    normalized_detail = validate_detail(event, detail)

    postings = {posting["key"] for posting in load_postings(postings_dir)}
    if posting_key not in postings:
        raise ValueError(f"unknown posting_key: {posting_key!r}")

    track_event = {
        "posting_key": posting_key,
        "event": event,
        "actor": EVENT_ACTORS[event],
        "detail": normalized_detail,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Evidence, not enforcement: the `.profile` stamp on the directory is what
    # refuses a second Profile. This makes the row itself say who recorded it,
    # so a store that outlives the checkout is still self-describing. A caller
    # with no Profile writes no field at all rather than a placeholder — absent
    # means unknown (coordination/CONTRACTS.md).
    if identifier is not None:
        track_event["profile_id"] = identifier
        # The store is verified above and stamped here, the last act before the
        # write: a refused run leaves the Track store byte-identical, and an
        # unclaimed one uncreated.
        claim_track_dir(track_dir, identifier)
    append_events(track_dir, [track_event])
    return track_event


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=sorted(EVENT_ACTORS))
    parser.add_argument("posting_key")
    parser.add_argument("--detail")
    parser.add_argument("--postings-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--decisions-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument("--track-dir", type=Path, default=None, help=STORE_HELP)
    add_profile_argument(parser)
    args = parser.parse_args()
    try:
        profile = profile_from_arguments(args)
    except ProfileError as error:
        parser.error(str(error))
    try:
        track_event = record_event(
            args.event,
            args.posting_key,
            args.detail,
            postings_dir=args.postings_dir,
            decisions_dir=args.decisions_dir,
            track_dir=args.track_dir,
            identifier=profile.identifier,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"recorded {track_event['event']} for {track_event['posting_key']}")


if __name__ == "__main__":
    main()
