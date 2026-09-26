"""Every input to the Jev selection must invalidate its disposable plan."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from venator.qualify import plan_cache


@pytest.mark.parametrize("changed", [
    "posting", "description", "decision", "decision_claim", "qualification",
    "promotion", "qualification_claim", "targeting", "constraints", "resume",
    "release", "policy", "filter_code", "profile_code",
])
def test_each_selection_input_invalidates_cache(tmp_path: Path, monkeypatch, changed: str) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    stores = SimpleNamespace(
        path=tmp_path, postings_dir=tmp_path / "postings",
        decisions_dir=tmp_path / "decisions",
        qualifications_dir=tmp_path / "qualifications",
    )
    targets = {
        "posting": stores.postings_dir / "2026-09-01.jsonl",
        "description": stores.postings_dir / "2026-09-02.jsonl",
        "decision": stores.decisions_dir / "2026-09-01.jsonl",
        "decision_claim": stores.decisions_dir / ".profile",
        "qualification": stores.qualifications_dir / "2026-09-01.jsonl",
        "promotion": stores.qualifications_dir / "promotions.jsonl",
        "qualification_claim": stores.qualifications_dir / ".profile",
        "targeting": profile_dir / "targeting.yaml",
        "constraints": profile_dir / "constraints.yaml",
        "resume": profile_dir / "resume.yaml",
    }
    # Code files live in the checkout and cannot be modified by a test. Pin
    # their membership by injecting a fixture into the matching glob instead.
    code_dir = {"release": "qualify", "policy": "qualify", "filter_code": "match",
                "profile_code": "profile"}.get(changed)
    original_files = plan_cache._files
    code_target = tmp_path / f"{changed}.py"
    if code_dir:
        def files(directory: Path, pattern: str) -> list[Path]:
            found = original_files(directory, pattern)
            if directory.name == code_dir and pattern == "*.py":
                return [*found, code_target]
            return found
        monkeypatch.setattr(plan_cache, "_files", files)
        targets[changed] = code_target
    profile = SimpleNamespace(identifier="test", directory=profile_dir)
    calls = []
    def compute(*args):
        calls.append(len(calls))
        return {"postingCount": len(calls)}
    monkeypatch.setattr(plan_cache, "_compute", compute)
    monkeypatch.setenv("VENATOR_PLAN_CACHE_DIR", str(tmp_path / "scratch"))
    first = plan_cache.jev_plan(profile, stores, "2026-09-26")
    assert plan_cache.jev_plan(profile, stores, "2026-09-26") == first
    target = targets[changed]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed")
    assert plan_cache.jev_plan(profile, stores, "2026-09-26") != first
    assert len(calls) == 2
    assert plan_cache.jev_plan(profile, stores, "2026-09-27") != first
    assert len(calls) == 3


def test_refresh_and_replacement_do_not_reuse_plan(tmp_path: Path, monkeypatch) -> None:
    profile = SimpleNamespace(identifier="test", directory=tmp_path)
    stores = SimpleNamespace(decisions_dir=tmp_path / "decisions", postings_dir=tmp_path / "postings",
                             qualifications_dir=tmp_path / "qualifications")
    calls = []
    monkeypatch.setattr(plan_cache, "_compute", lambda *args: calls.append(1) or {"n": len(calls)})
    monkeypatch.setenv("VENATOR_PLAN_CACHE_DIR", str(tmp_path / "scratch"))
    assert plan_cache.jev_plan(profile, stores, "2026-09-26") == {"n": 1}
    assert plan_cache.jev_plan(profile, stores, "2026-09-26", refresh=True) == {"n": 2}
    assert plan_cache.jev_plan(profile, stores, "2026-09-26") == {"n": 2}
    source = stores.postings_dir / "2026-09-01.jsonl"
    source.parent.mkdir()
    source.write_bytes(b"first")
    assert plan_cache.jev_plan(profile, stores, "2026-09-26") == {"n": 3}
    replacement = source.with_suffix(".tmp")
    replacement.write_bytes(b"other")
    os.utime(replacement, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))
    os.replace(replacement, source)
    assert plan_cache.jev_plan(profile, stores, "2026-09-26") == {"n": 4}
