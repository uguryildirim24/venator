"""Nightly pre-drafts use isolated Postings and a stubbed preparation seam."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from venator.discover.store import append_observations
from venator.profile import load_profile
from venator.schedule import predraft


@pytest.fixture
def night(tmp_path: Path, monkeypatch):
    install = tmp_path / "Install"
    directory = install / "profiles" / "fixture"
    directory.mkdir(parents=True)
    (directory / "targeting.yaml").write_text("profile:\n  name: fixture\n  id: fixture\n")
    monkeypatch.setenv("VENATOR_HOME", str(install))
    postings = [{"key": f"greenhouse:fixture:{index}", "source": "greenhouse", "board": "fixture",
                 "title": "Fixture Lab", "listing_status": "open", "description_html": "Fixture",
                 "last_verified_at": f"2026-09-{index + 1:02}T00:00:00Z"} for index in range(15)]
    for posting in postings:
        append_observations(install / "data" / "postings", [posting], observed_at=posting["last_verified_at"])
    inputs = [SimpleNamespace(posting_key=row["key"], score=SimpleNamespace(probability=0.8)) for row in postings]
    monkeypatch.setattr(predraft, "select_inputs", lambda *_args: (None, inputs))
    ready = set()
    monkeypatch.setattr(predraft.applications, "ready_application", lambda _paths, _profile, posting:
                        {} if posting["key"] in ready else None)
    calls = []
    def prepare(args):
        assert args.action == "predraft"
        calls.append(args.key)
        ready.add(args.key)
        return {"prepared": True}
    monkeypatch.setattr(predraft.applications, "perform", prepare)
    return SimpleNamespace(directory=directory, calls=calls, ready=ready, inputs=inputs)


def test_default_ten_newest_and_repeated_loop_stays_within_nightly_ceiling(night):
    result = predraft.run("fixture", "2026-09-20")
    assert result == {"drafted": 10, "attempted": 10}
    assert night.calls == [f"greenhouse:fixture:{index}" for index in range(14, 4, -1)]
    assert predraft.run("fixture", "2026-09-20") == {"drafted": 0}
    assert predraft.run("fixture", "2026-09-21") == {"drafted": 5, "attempted": 5}


def test_toggle_off_and_profile_count(night):
    path = night.directory / "targeting.yaml"
    path.write_text(path.read_text() + "predraft:\n  enabled: false\n  limit: 2\n")
    assert predraft.run("fixture", "2026-09-20") == {"drafted": 0}
    assert night.calls == []
    path.write_text(path.read_text().replace("enabled: false", "enabled: true"))
    assert predraft.run("fixture", "2026-09-20") == {"drafted": 2, "attempted": 2}
    assert len(night.calls) == 2


def test_only_for_you_and_skips_ready_drafts(night):
    night.inputs[14].score.probability = 0.2
    night.inputs[13].score = None
    night.ready.add("greenhouse:fixture:12")
    predraft.run("fixture", "2026-09-20")
    assert night.calls[0] == "greenhouse:fixture:11"
    assert len(night.calls) == 10


def test_failed_draft_consumes_one_attempt_and_pauses(night, monkeypatch):
    def fail(_args):
        raise RuntimeError("Fixture completion unavailable")
    monkeypatch.setattr(predraft.applications, "perform", fail)
    assert predraft.run("fixture", "2026-09-20") == {"drafted": 0, "attempted": 1, "paused": "drafting-unavailable"}
    assert predraft.run("fixture", "2026-09-20")["attempted"] == 1
    receipt = night.directory.parents[1] / "data" / "predrafts" / "2026-09-20.jsonl"
    assert len(receipt.read_text().splitlines()) == 2


def test_profile_defaults_and_rejects_invalid_count(night):
    profile = load_profile(night.directory)
    assert profile.predraft_enabled is True and profile.predraft_limit == 10
    path = night.directory / "targeting.yaml"
    path.write_text(path.read_text() + "predraft:\n  limit: -1\n")
    with pytest.raises(ValueError, match="predraft.limit"):
        load_profile(night.directory)
