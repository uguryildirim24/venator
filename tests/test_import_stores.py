from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.import_stores import import_stores


def test_import_preserves_distinct_observations_and_is_repeatable(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    for root, values in [(source, [{"posting_key": "x", "revision": 1}, {"posting_key": "x", "revision": 2}]), (target, [{"revision": 1, "posting_key": "x"}])]:
        path = root / "data" / "postings" / "2026-01-01.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(value) + "\n" for value in values))
    assert import_stores(source, target) == (1, 0)
    assert import_stores(source, target) == (0, 0)
    rows = (target / "data/postings/2026-01-01.jsonl").read_text().splitlines()
    assert len(rows) == 2
    assert [json.loads(row)["revision"] for row in rows] == [1, 2]


def test_import_refuses_immutable_collision_before_any_append(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    for root, content in [(source, b"new"), (target, b"old")]:
        directory = root / "data" / "qualifications"
        directory.mkdir(parents=True)
        (directory / "promotions.json").write_bytes(content)
    with pytest.raises(ValueError, match="immutable store file differs"):
        import_stores(source, target)
    assert (target / "data/qualifications/promotions.json").read_bytes() == b"old"
