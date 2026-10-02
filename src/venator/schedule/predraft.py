"""Pre-draft newest For you Postings without opening an application browser."""

from __future__ import annotations

import argparse
import json
from datetime import date

from venator import applications
from venator.discover.store import load_postings
from venator.paths import install_stores, resolve_store_paths
from venator.profile import resolve_profile
from venator.profile.claim import claim_store
from venator.score.selection import select_inputs
from venator.track.store import fold_states, load_events, verify_track_dir


def run(profile_name: str | None, as_of: str | None = None) -> dict:
    profile = resolve_profile(profile_name)
    if not profile.predraft_enabled or profile.predraft_limit == 0:
        return {"drafted": 0}
    day = as_of or date.today().isoformat()
    stores = install_stores()
    paths = resolve_store_paths()
    ledger = stores.data_dir / "predrafts"
    claim_store(ledger, profile.identifier)
    receipt = ledger / f"{day}.jsonl"
    attempts = [json.loads(line) for line in receipt.read_text(encoding="utf-8").splitlines()] if receipt.exists() else []
    remaining = max(0, profile.predraft_limit - len(attempts))
    if not remaining:
        return {"drafted": 0}
    _, inputs = select_inputs(profile, stores, day)
    keys = {row.posting_key for row in inputs if row.score is not None and row.score.probability >= 0.5}
    verify_track_dir(stores.track_dir, profile.identifier)
    states = fold_states(load_events(stores.track_dir), {})
    keys -= {key for key, state in states.items() if state["state"] in {
        "approved", "rejected", "prepared", "filled", "submitted", "concluded", "withdrawn"}}
    keys -= {row["key"] for row in attempts}
    postings = sorted(load_postings(stores.postings_dir),
                      key=lambda row: (str(row.get("last_verified_at") or ""),
                                       str(row.get("discovered_at") or ""), row["key"]), reverse=True)
    drafted = 0
    attempted = 0
    for posting in postings:
        if (posting["key"] not in keys or posting.get("listing_status") != "open"
                or posting.get("source") not in {"greenhouse", "ashby", "workday", "lever"}
                or applications.ready_application(paths, profile, posting) is not None):
            continue
        # Reserve an attempt before drafting. Repeated daily loops, including a
        # failed completion, cannot multiply the Profile's nightly ceiling.
        with receipt.open("a", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps({"key": posting["key"], "profile_id": profile.identifier}) + "\n")
        attempted += 1
        try:
            applications.perform(argparse.Namespace(action="predraft", key=posting["key"],
                                                    profile=profile.directory.name, profile_dir=None))
        except Exception:
            return {"drafted": drafted, "attempted": attempted, "paused": "drafting-unavailable"}
        drafted += 1
        if attempted >= remaining:
            break
    return {"drafted": drafted, "attempted": attempted}
