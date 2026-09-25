"""Import another Install's append-only history without losing Posting revisions.

Run ``python -m venator.import_stores --from ROOT`` after the other writer exits.
ROOT contains ``data/``. No importer may run concurrently with either writer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from venator.paths import install_stores

SNAPSHOTS = {"postings/source-health.json", "postings/source-progress.json"}


def _identity(line: bytes) -> str:
    return json.dumps(json.loads(line), sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _rows(path: Path) -> list[bytes]:
    with path.open("rb") as stream:
        lines = stream.readlines()
    for line in lines:
        if not line.endswith(b"\n"):
            raise ValueError(f"incomplete JSONL row in {path}")
        _identity(line)
    return lines


def _same(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    with a.open("rb") as left, b.open("rb") as right:
        return hashlib.file_digest(left, "sha256").digest() == hashlib.file_digest(right, "sha256").digest()


def _snapshot(source: Path, destination: Path) -> dict[str, object]:
    """Keep the later observation of each board, not the older base snapshot."""
    earlier = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {}
    later = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(earlier, dict) or not isinstance(later, dict):
        raise ValueError(f"invalid board metadata at {source}")
    merged = dict(earlier)
    for board, value in later.items():
        if not isinstance(value, dict) or (board in earlier and not isinstance(earlier[board], dict)):
            raise ValueError(f"invalid board metadata for {board}")
        previous = earlier.get(board, {})
        if (value.get("last_attempt_at") or "") >= (previous.get("last_attempt_at") or ""):
            merged[board] = value
    return merged


def import_stores(source: Path, target: Path) -> tuple[int, int]:
    """Return (new rows, new immutable files); verify all imported identities."""
    source = source / "data"
    target = target / "data"
    if not source.is_dir() or source.resolve() == target.resolve():
        raise ValueError("--from must name a different Install with a data/ directory")
    files = sorted(p for p in source.rglob("*") if p.is_file())
    changed: list[tuple[Path, Path]] = []
    snapshots: list[tuple[Path, dict[str, object]]] = []
    # Preflight collisions and syntax before the first write. Ignore identical
    # files; the Jev response cache alone can contain thousands of them.
    for path in files:
        relative = path.relative_to(source)
        destination = target / relative
        if destination.exists() and _same(path, destination):
            continue
        if relative.as_posix() in SNAPSHOTS:
            snapshots.append((destination, _snapshot(path, destination)))
        elif path.suffix == ".jsonl":
            _rows(path)
            if destination.exists():
                _rows(destination)
            changed.append((path, destination))
        elif destination.exists():
            raise ValueError(f"immutable store file differs: {destination}")
        else:
            changed.append((path, destination))
    added_rows = added_files = 0
    for path, destination in changed:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix != ".jsonl":
            shutil.copy2(path, destination)
            added_files += 1
            if not _same(path, destination):
                raise ValueError(f"import verification failed: {destination}")
            continue
        existing = {_identity(row) for row in _rows(destination)} if destination.exists() else set()
        source_ids = {_identity(row) for row in _rows(path)}
        with destination.open("ab") as output:
            for row in _rows(path):
                identity = _identity(row)
                if identity not in existing:
                    output.write(row)
                    existing.add(identity)
                    added_rows += 1
        if not source_ids <= {_identity(row) for row in _rows(destination)}:
            raise ValueError(f"import verification failed: {destination}")
    for destination, value in snapshots:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.import-tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        if json.loads(destination.read_text(encoding="utf-8")) != value:
            raise ValueError(f"import verification failed: {destination}")
    return added_rows, added_files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", type=Path, required=True)
    args = parser.parse_args()
    root = install_stores()
    root.announce()
    rows, files = import_stores(args.source, root.path)
    print(f"Verified import: {rows} new rows, {files} new immutable files")


if __name__ == "__main__":
    main()
