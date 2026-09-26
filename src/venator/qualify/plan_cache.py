"""Fingerprint-validated, disposable Jev plan for the dashboard and View rebuild.

A cache hit reads metadata only, never Posting bodies. The stores are append-only in
normal operation; inode, size, mtime and ctime also detect replacements and edits.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from venator.match.store import verify_decisions_dir
from venator.qualify.jev import current_mode, passing_postings, select_work
from venator.qualify.jev_release import JEV_RELEASE
from venator.qualify.store import as_of_month, verify_store

CACHE_REVISION = 1


def _files(directory: Path, pattern: str) -> list[Path]:
    return sorted(directory.glob(pattern)) if directory.exists() else []


def fingerprint(profile: Any, stores: Any, as_of: str) -> str:
    """All selection inputs, including ownership and release/code changes.

    A metadata fingerprint avoids the 1.3 GB read on a hit. A writer changing a
    file during selection is detected by checking again before publishing.
    """
    paths = [
        *(_files(stores.postings_dir, "*.jsonl")),
        *(_files(stores.decisions_dir, "*.jsonl")),
        stores.decisions_dir / ".profile",
        *(_files(stores.qualifications_dir, "*.jsonl")),
        stores.qualifications_dir / ".profile",
        stores.qualifications_dir / "promotions.jsonl",
        *(Path(profile.directory) / name for name in ("targeting.yaml", "constraints.yaml", "resume.yaml")),
        # Release, policy compilation, Hard Filter version, and selection code.
        *(_files(Path(__file__).parent, "*.py")),
        *(_files(Path(__file__).parents[1] / "profile", "*.py")),
        *(_files(Path(__file__).parents[1] / "match", "*.py")),
        Path(__file__).parents[1] / "discover" / "store.py",
    ]
    facts = []
    for path in paths:
        try:
            stat = path.stat()
            facts.append((str(path), stat.st_dev, stat.st_ino, stat.st_size,
                          stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            facts.append((str(path), None))
    payload = (CACHE_REVISION, as_of, profile.identifier, str(stores.decisions_dir.parent), facts)
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _cache_path(profile: Any, stores: Any) -> Path:
    root = Path(os.environ.get("VENATOR_PLAN_CACHE_DIR") or stores.decisions_dir.parent / "cache")
    return root / f"jev-plan-{hashlib.sha256((str(stores.decisions_dir.parent) + ':' + profile.identifier).encode()).hexdigest()}.json"


def _compute(profile: Any, stores: Any, as_of: str) -> dict:
    if stores.decisions_dir.exists():
        verify_decisions_dir(stores.decisions_dir, profile.identifier)
    if stores.qualifications_dir.exists():
        verify_store(stores.qualifications_dir, profile.identifier)
    month = as_of_month(as_of)
    mode = current_mode(profile, month, stores.qualifications_dir, JEV_RELEASE)
    revisions: dict[str, str] = {}
    postings = passing_postings(
        stores.postings_dir, stores.decisions_dir, stores.qualifications_dir, profile, month,
        eligible_only=True, revisions=revisions,
        decisions_verified=True, qualifications_verified=True,
    )
    work = select_work(
        postings, profile=profile, month=month, mode=mode,
        qualifications_dir=stores.qualifications_dir, named_keys=None, release=JEV_RELEASE,
        decided_at=f"{as_of}T00:00:00+00:00", bindings_only=True,
        posting_revisions=revisions, qualifications_verified=True,
    )
    count = work.report.selected
    selection_hash = hashlib.sha256(json.dumps(sorted(work.selection), separators=(",", ":")).encode()).hexdigest()
    return {"asOf": as_of, "mode": mode, "postingCount": count,
            "maximumUsd": 10.0 if count else 0.0, "selectionHash": selection_hash}


def jev_plan(profile: Any, stores: Any, as_of: str, *, refresh: bool = False) -> dict:
    path = _cache_path(profile, stores)
    before = fingerprint(profile, stores, as_of)
    if not refresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached["fingerprint"] == before and fingerprint(profile, stores, as_of) == before:
                return cached["plan"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    for _ in range(3):
        before = fingerprint(profile, stores, as_of)
        result = _compute(profile, stores, as_of)
        if fingerprint(profile, stores, as_of) != before:
            continue  # A writer changed an input mid-selection; do not return it.
        # Cache failure must not prevent a read-only plan or a View rebuild.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as output:
                    output.write(json.dumps({"fingerprint": before, "plan": result}))
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return result
    raise ValueError("Posting or qualification inputs changed during Jev plan selection; try again")
