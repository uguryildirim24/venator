"""The rebuild lets go of the temporary database before it renames it.

`sqlite3`'s context manager commits the transaction and leaves the connection
**open** — it is a transaction manager, not a resource manager. So the build was
handing `os.replace` a source file it still held a handle on. POSIX renames over
an open file happily, which is why this never surfaced; Windows does not, unless
the holder opened with `FILE_SHARE_DELETE`, and SQLite's Windows VFS opens with
`FILE_SHARE_READ | FILE_SHARE_WRITE` only. `MoveFileExW` then fails with
WinError 32 — with no dashboard running and no second process involved at all.

The `except` beside it had the same shape and a worse consequence: `unlink` on a
file this process still holds would raise from inside the handler, replacing a
genuine build error (bad JSONL, a constraint violation) with a `PermissionError`
and demoting the real diagnosis to `__context__`.

Both are asserted here by ordering, not by platform, so they hold on the machine
that runs this suite.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from venator.view import build as view_build

POSTING = {
    "key": "greenhouse:example:1",
    "source": "greenhouse",
    "board": "example",
    "title": "Scientist",
    "location": "Boston, MA",
    "url": "https://example.test/1",
}


def stores(tmp_path: Path) -> dict[str, Path]:
    postings = tmp_path / "data" / "postings"
    postings.mkdir(parents=True)
    (postings / "2026-08-18.jsonl").write_text(
        json.dumps(POSTING) + "\n", encoding="utf-8"
    )
    decisions = tmp_path / "data" / "decisions"
    decisions.mkdir(parents=True)
    track = tmp_path / "data" / "track"
    track.mkdir(parents=True)
    return {
        "postings_dir": postings,
        "decisions_dir": decisions,
        "database_path": tmp_path / "build" / "venator.db",
        "track_dir": track,
        "runs_file": tmp_path / "data" / "runs.jsonl",
    }


def watch_connections(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    """Record, in order, every time a view connection is actually closed.

    A `sqlite3.Connection`'s `close` is read-only, so this goes through the
    documented `factory=` seam rather than trying to patch the method.
    """

    class Watched(sqlite3.Connection):
        def close(self) -> None:
            log.append("closed")
            super().close()

    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs.setdefault("factory", Watched)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(view_build.sqlite3, "connect", connect)


def test_the_connection_is_closed_before_the_view_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    watch_connections(monkeypatch, log)
    real_replace = view_build.os.replace

    def replace(source: object, destination: object) -> None:
        log.append("replaced")
        real_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(view_build.os, "replace", replace)

    view_build.build_database(**stores(tmp_path))

    assert log == ["closed", "replaced"], (
        "the temporary database is still open when it is renamed; on Windows "
        "that is WinError 32 in this same process"
    )


def test_a_failed_build_closes_the_database_before_it_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    watch_connections(monkeypatch, log)
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name.endswith(".tmp"):
            log.append("unlinked")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    def explode(source: object, destination: object) -> None:
        raise RuntimeError("the build itself went wrong")

    monkeypatch.setattr(view_build.os, "replace", explode)

    with pytest.raises(RuntimeError, match="the build itself went wrong"):
        view_build.build_database(**stores(tmp_path))

    # The pre-build unlink runs first (nothing holds that file), then the
    # connection closes, and only then does the cleanup touch the temporary.
    assert log[-2:] == ["closed", "unlinked"]


def test_a_cleanup_that_cannot_run_does_not_replace_the_real_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diagnosis a person reads is the build's, not the housekeeping's."""
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name.endswith(".tmp") and self.exists():
            raise PermissionError(32, "The process cannot access the file")
        real_unlink(self, missing_ok=missing_ok)

    def explode(source: object, destination: object) -> None:
        raise RuntimeError("the build itself went wrong")

    monkeypatch.setattr(view_build.os, "replace", explode)
    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(RuntimeError, match="the build itself went wrong"):
        view_build.build_database(**stores(tmp_path))
