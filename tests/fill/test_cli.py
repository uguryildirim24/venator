from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def run_plan(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "venator.fill.plan", *(str(arg) for arg in args)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_writes_plan_under_sanitized_key_and_prints_tally(tmp_path: Path) -> None:
    postings_dir = tmp_path / "postings"
    out_dir = tmp_path / "fill"
    posting_key = "source:board:1"
    write_jsonl(
        postings_dir / "2026-08-18.jsonl",
        [{"key": posting_key, "url": "https://example.test/apply"}],
    )

    result = run_plan(
        posting_key,
        "--profile", "example",
        "--out",
        out_dir,
        "--postings-dir",
        postings_dir,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"planned {posting_key}: 9 mapped, 3 unmapped"
    plan_path = out_dir / "source-board-1" / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["never_submit"] is True
    assert len(plan["fields"]) + len(plan["unmapped"]) == 12


def test_cli_refuses_unknown_posting_without_writing(tmp_path: Path) -> None:
    postings_dir = tmp_path / "postings"
    out_dir = tmp_path / "fill"
    write_jsonl(postings_dir / "2026-08-18.jsonl", [{"key": "known", "url": "https://x"}])

    result = run_plan("missing", "--profile", "example", "--out", out_dir, "--postings-dir", postings_dir)

    assert result.returncode == 2
    assert "unknown posting_key" in result.stderr
    assert not out_dir.exists()
