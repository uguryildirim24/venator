"""Report what a pipeline run would do, in JSON, for the dashboard's run surface.

    python plan.py --kind fetch-and-filter|jev-it [--profile NAME]

This is an **adapter**, not a second pipeline. Every rule it applies belongs to
``src/venator/``: which Profile a run would load and how a name resolves to a
directory (``venator.profile``), where a run would write and which view it would
rebuild (``venator.paths``), and whether this Install's Filter Decisions belong to
that Profile at all (``venator.match.store.verify_decisions_dir``). It imports
and composes; it decides nothing. A second copy of any of it in TypeScript is
the drift ``resolve_board.py`` was written to prevent, and that file's header
states the rule this one reuses verbatim.

It is the mirror of what ``server/onboarding/resolve_board.py`` does for
``venator.discover.register``: one subprocess, one JSON document on stdout, one
normalisation on the Node side.

Three properties are deliberate:

* **It writes nothing and starts nothing.** No store is created, no directory is
  stamped, no stage is run, and no request leaves this machine. Answering "what
  would a run do" must not itself be a run.
* **A refusal is data.** A Profile that cannot be chosen, or a store this
  Profile may not read, comes back as a ``failure`` object with the pipeline's
  own sentence in it — exit 0 — so the dashboard can put that sentence next to a
  disabled button rather than rendering a crash.
* **A caught exception's text is never invented over.** ``ProfileError`` and the
  store-claim refusal are written for a person by the modules that raise them,
  and they are passed along unchanged. Everything else is a category with a
  message written here, and the original goes to stderr where the server logs it.

Exit codes: ``0`` a JSON document on stdout, ``3`` the pipeline module could not
be imported, ``2`` this script was called wrongly.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

# The pipeline prints its Profile and store announcements as it resolves them.
# They go to stderr already, but `resolve_profile` and friends may print to
# stdout too, so stdout is held aside and handed back only for the document.
_STDOUT = sys.stdout

try:
    from venator.match.store import verify_decisions_dir
    from venator.paths import StoreRootError, install_stores
    from venator.profile import ProfileError, resolve_profile
    from venator.qualify.jev import current_mode, passing_postings, select_work
    from venator.qualify.jev_release import JEV_RELEASE
    from venator.qualify.store import as_of_month
except Exception as err:  # pragma: no cover - exercised by an Install without the pipeline
    print(f"plan: the pipeline could not be imported: {err}", file=sys.stderr)
    raise SystemExit(3) from err


def _failure(kind: str, message: str | None) -> dict:
    return {"failure": {"kind": kind, "message": message}}


def _jev_plan(profile, stores, as_of: str) -> dict:
    month = as_of_month(as_of)
    mode = current_mode(profile, month, stores.qualifications_dir, JEV_RELEASE)
    postings = passing_postings(
        stores.postings_dir, stores.decisions_dir, stores.qualifications_dir, profile, month,
    )
    work = select_work(
        postings,
        profile=profile,
        month=month,
        mode=mode,
        qualifications_dir=stores.qualifications_dir,
        named_keys=None,
        release=JEV_RELEASE,
        decided_at=f"{as_of}T00:00:00+00:00",
        bindings_only=True,
    )
    count = work.report.selected
    # The count and rounded allowance alone do not identify the work. A Profile
    # edit or a different set of Postings can leave both unchanged.
    selection_hash = hashlib.sha256(json.dumps(sorted(
        work.selection
    ), separators=(",", ":")).encode("utf-8")).hexdigest()
    maximum_usd = 10.0 if count else 0.0
    return {
        "asOf": as_of,
        "mode": mode,
        "postingCount": count,
        "maximumUsd": maximum_usd,
        "selectionHash": selection_hash,
    }


def _document(profile, stores, as_of: str, kind: str) -> dict:
    document = {
        "profile": {
            "name": profile.name,
            # Absolute, because the Node side compares it against the Profile
            # the dashboard listed and shows it to a person; the pipeline's own
            # `profiles/<name>` is relative to whatever it was run from.
            "directory": str(Path(profile.directory).resolve()),
            "identifier": profile.identifier,
        },
        "storeRoot": str(stores.path),
        "viewPath": str(stores.database_path),
        # How many ATS boards this Profile registers. Zero is not an error and
        # is not a guess about the search: it is why a first run can finish
        # cleanly and find nothing, which is worth saying before the run rather
        # than after it.
        "boards": sum(len(tokens) for tokens in profile.sources.boards.values()),
    }
    if kind == "jev-it":
        document["jev"] = _jev_plan(profile, stores, as_of)
    return document


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="What a pipeline run would do.")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--kind", choices=("fetch-and-filter", "jev-it"), required=True)
    args = parser.parse_args(argv)

    with contextlib.redirect_stdout(sys.stderr):
        try:
            profile = resolve_profile(requested=args.profile)
        except ProfileError as err:
            # `venator.profile` writes these for the person reading them: no
            # Profile, two Profiles and which, or a name that is not one of
            # them. Passed through unchanged.
            payload = _failure("profile", str(err))
        else:
            try:
                stores = install_stores()
            except StoreRootError as err:
                # Already names both candidate stores and the fix.
                payload = _failure("store", str(err))
            else:
                try:
                    # The read that a run would do first. A decisions store
                    # claimed by another Profile is refused here, before
                    # Discover has appended anything, rather than partway
                    # through a run — `data/` is append-only.
                    if stores.decisions_dir.exists():
                        verify_decisions_dir(stores.decisions_dir, profile.identifier)
                    payload = _document(profile, stores, date.today().isoformat(), args.kind)
                except (OSError, ValueError) as err:
                    payload = _failure("store", str(err))
                except Exception as err:  # pragma: no cover - the unclassified case
                    print(f"plan: {type(err).__name__}: {err}", file=sys.stderr)
                    payload = _failure("failed", None)

    json.dump(payload, _STDOUT)
    _STDOUT.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
