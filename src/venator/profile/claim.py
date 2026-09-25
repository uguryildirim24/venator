"""One append-only store directory belongs to one Profile.

``data/decisions/`` and ``data/track/`` are both keyed by Posting alone —
effective state is the latest Filter Decision per (Posting, stage), and the
latest TrackEvent per Posting. Neither key carries a Profile, and by ADR-0002
neither ever will: there is no user column anywhere, because one checkout is
one person's boundary. That is exactly what makes two Profiles sharing one
store silent rather than loud — whichever ran last wins, and the answer looks
like an answer.

So the directory records the Profile that owns it, and a second Profile is
refused rather than interleaved. This module holds the mechanism both stores
use, and nothing else: reading the stamp, writing it, counting appended rows
that name another Profile, and saying so. The prose of a refusal stays with the
store that refuses, because what is lost differs — a Review Queue is not a
lifecycle history.

Three properties are the reason it is one mechanism rather than two:

* **A read never claims.** ``claim_store`` is called only from a write path.
  A read-only command that stamped a directory would decide whose store it is
  as a side effect of answering a question about it.
* **Unclaimed is unknown, not a mismatch.** No stamp, an empty stamp and a
  whitespace-only stamp all read as unclaimed, and the operation proceeds.
  Every store written before a claim existed stays usable, with nothing to
  migrate (ADR-0001).
* **Rows are evidence, never the lock.** ``profile_id`` on a row is reported
  and nothing more. Refusing on the strength of a row would be a second lock
  that can disagree with the first, and would make a store restored from a
  backup unusable rather than suspect. No row is ever dropped, filtered or
  rewritten.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

#: The file a store directory records its owning Profile in. One name for both
#: stores: a data directory holding ``decisions/.profile`` and ``track/.profile``
#: says the same thing twice about the same Profile, in the same words.
STORE_OWNER_FILE = ".profile"


def store_owner(directory: Path) -> str | None:
    """The Profile identifier a store is claimed by, or None if unclaimed.

    An empty or whitespace-only stamp reads as unclaimed rather than as a
    Profile with a blank name: a blank identifier would match nothing and
    refuse everything, turning a truncated write into a locked store.
    """
    stamp = directory / STORE_OWNER_FILE
    if not stamp.exists():
        return None
    return stamp.read_text(encoding="utf-8").strip() or None


def claim_store(directory: Path, identifier: str) -> None:
    """Record ``identifier`` as the owner of a store that has none yet.

    Only a write path may call this — see the module docstring. The directory
    is created if it does not exist, because the first write to a store is
    what claims it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / STORE_OWNER_FILE).write_text(
        identifier + "\n", encoding="utf-8", newline="\n"
    )


def rows_from_other_profiles(rows: Iterable[object], identifier: str) -> dict[str, int]:
    """Count appended rows that name a Profile other than ``identifier``.

    A row written before ``profile_id`` existed carries no identifier, and that
    is **unknown, never a mismatch** — it is skipped here rather than counted
    against anything. ADR-0001 makes ``data/`` append-only, so those rows are
    permanent and must stay ordinary.
    """
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        wrote = row.get("profile_id")
        if isinstance(wrote, str) and wrote and wrote != identifier:
            counts[wrote] = counts.get(wrote, 0) + 1
    return counts


def report_stray_rows(
    directory: Path,
    identifier: str,
    strays: Mapping[str, int],
    *,
    owner: str | None,
    flag: str,
) -> None:
    """Say out loud that a store holds another Profile's rows.

    ``owner`` is what the stamp actually says, so the warning describes the
    store as it is. An unclaimed store that holds a stray row is the more
    interesting of the two cases — nothing on disk says which Profile it
    belongs to — and reporting it as *claimed* would state the opposite of the
    truth to the one person in a position to check.
    """
    if not strays:
        return
    named = ", ".join(f"{count} by Profile {wrote!r}" for wrote, count in sorted(strays.items()))
    if owner is not None:
        holds = (
            f"{directory} is claimed by Profile {identifier!r} but holds rows written by "
            f"another Profile ({named})."
        )
    else:
        holds = (
            f"{directory} is claimed by no Profile — nothing on disk says whose store it is — "
            f"and holds rows written by a Profile other than {identifier!r} ({named})."
        )
    print(
        f"WARNING: {holds} No row has been dropped or changed — effective state still reads "
        f"every row. Check that {flag} points at this Profile's own store.",
        file=sys.stderr,
    )
