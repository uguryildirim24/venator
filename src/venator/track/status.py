"""Print effective lifecycle states for every queued or tracked Posting.

Usage:
    python -m venator.track.status [--profile NAME]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from venator.match.store import load_latest_decisions, load_postings, verify_decisions_dir
from venator.paths import STORE_HELP, resolve_store_paths
from venator.profile import ProfileError, add_profile_argument, profile_from_arguments
from venator.track.store import STATE_ORDER, fold_states, load_events, verify_track_dir

_STATE_RANK = {state: index for index, state in enumerate(STATE_ORDER)}


def status_rows(
    postings_dir: Path | None = None,
    decisions_dir: Path | None = None,
    track_dir: Path | None = None,
    *,
    identifier: str | None = None,
) -> list[dict]:
    """The effective Application state of every queued or tracked Posting.

    ``identifier`` is the Profile this answer is for, and **both** stores it is
    folded from are checked against it.

    A lifecycle state is folded from TrackEvents *over a queue verdict*: the
    queue verdict comes from the decisions store, so answering out of another
    Profile's decisions reports that Profile's Review Queue as this one's. The
    events come from the Track store, and ``fold_states`` keys a state by
    ``posting_key`` alone — so answering out of another Profile's Track store
    reports that Owner's approvals, rejections and outcomes as this one's, which
    is how a Posting this Profile approved reads back as ``rejected``.

    Both are the same directory claim the write paths use; neither read claims
    anything. ``None`` skips both, for callers with no Profile to hand.
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
    posting_keys = {posting["key"] for posting in load_postings(postings_dir)}
    states = fold_states(load_events(track_dir), load_latest_decisions(decisions_dir))
    unknown = sorted(set(states) - posting_keys)
    if unknown:
        raise ValueError(f"Track state references unknown posting_key: {unknown[0]!r}")
    return sorted(
        states.values(),
        key=lambda row: (_STATE_RANK[row["state"]], row["posting_key"]),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
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
        rows = status_rows(
            args.postings_dir,
            args.decisions_dir,
            args.track_dir,
            identifier=profile.identifier,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
