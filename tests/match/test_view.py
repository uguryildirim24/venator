from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from venator.view.build import build_database


def test_view_build_is_idempotent(tmp_path: Path) -> None:
    postings_dir = tmp_path / "postings"
    decisions_dir = tmp_path / "decisions"
    database_path = tmp_path / "build" / "venator.db"
    postings_dir.mkdir()
    decisions_dir.mkdir()
    posting = {
        "key": "greenhouse:board:1",
        "source": "greenhouse",
        "board": "board",
        "title": "Intern",
        "location": "Boston, MA",
        "url": "https://example.test/1",
        "posted_at": "2026-08-01T00:00:00+00:00",
        "discovered_at": "2026-08-18T00:00:00+00:00",
        "description_html": "<p>Description</p>",
    }
    decision = {
        "posting_key": posting["key"],
        "stage": "hard_filter",
        "verdict": "pass",
        "rule": None,
        "reason": "passed",
        "filters_version": "abcdef123456",
        "decided_at": "2026-08-18T19:00:00+00:00",
    }
    (postings_dir / "2026-08-18.jsonl").write_text(json.dumps(posting) + "\n")
    (decisions_dir / "2026-08-18.jsonl").write_text(json.dumps(decision) + "\n")

    assert build_database(postings_dir, decisions_dir, database_path) == (1, 1)
    assert build_database(postings_dir, decisions_dir, database_path) == (1, 1)

    with sqlite3.connect(database_path) as database:
        assert database.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert database.execute("SELECT key, title FROM postings").fetchone() == (posting["key"], "Intern")
        columns = [row[1] for row in database.execute("PRAGMA table_info(decisions)")]
        assert columns == [
            "id",
            "posting_key",
            "stage",
            "verdict",
            "rule",
            "score",
            "reason",
            "filters_version",
            "decided_at",
        ]
