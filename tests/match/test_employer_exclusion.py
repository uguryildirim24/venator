"""Employer exclusions are operational Hard Filters, not Jev inputs."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import yaml

from venator.discover import run as discover
from venator.match.filters import apply_filters
from venator.match.store import FILTERS_REVISION, _hashed_bytes, filters_version
from venator.profile import load_profile
from venator.qualify.compile import compile_profile
from venator.qualify.versions import jev_accepted_profile_hash, jev_policy_hash

EXAMPLE = Path(__file__).parents[2] / "profiles" / "example"


def test_board_name_and_other_employers():
    profile = load_profile(EXAMPLE)
    base = profile.filters
    for excluded, posting in (
        ("cloudflare", {"board": "cloudflare"}),
        ("Cloudflare", {"board": "cloudflare"}),
        ("Cloudflare", {"board": "another", "company": "cloudflare"}),
    ):
        result = apply_filters(posting, replace(base, employer_exclude=(excluded,)))
        assert result.verdict == "kill"
        assert result.rule == "employer_excluded:Cloudflare" or result.rule == "employer_excluded:cloudflare"
    assert apply_filters({"board": "duolingo", "company": "Duolingo"},
                         replace(base, enabled=(), employer_exclude=("Cloudflare",))).verdict == "pass"


def test_discover_does_not_fetch_excluded_boards(tmp_path, monkeypatch):
    profile = load_profile(EXAMPLE)
    profile = replace(profile, filters=replace(profile.filters, employer_exclude=("Cloudflare",)))
    seen = []

    def process(_directory, _source, board, _adapter, **_kwargs):
        seen.append(board)
        return 0, 0, "ok"

    monkeypatch.setattr(discover, "_process_board", process)
    discover.run(profile, tmp_path, interactive=True)
    assert "cloudflare" not in seen
    assert set(seen) == {"duolingo", "linear"}
    assert "cloudflare" in profile.sources.boards["greenhouse"]


def test_hash_stability_and_replay(tmp_path):
    directory = tmp_path / "profile"
    directory.mkdir()
    for name in ("targeting.yaml", "constraints.yaml", "resume.yaml"):
        (directory / name).write_bytes((EXAMPLE / name).read_bytes())

    targeting = yaml.safe_load((directory / "targeting.yaml").read_text())
    targeting["profile"]["scaffold"] = False
    (directory / "targeting.yaml").write_text(yaml.safe_dump(targeting))

    def hashes():
        profile = load_profile(directory)
        compiled = compile_profile(profile, as_of_month="2026-09")
        policy = jev_policy_hash(profile.filters.jev)
        return (filters_version(profile.constraints_path, profile.targeting_path),
                compiled.profile_hash, policy,
                jev_accepted_profile_hash(compiled.profile_hash, policy))

    original = hashes()
    # Every Profile moves with the filter-code revision, with or without exclusions.
    digest = hashlib.sha256(FILTERS_REVISION)
    for path, skipped in ((directory / "constraints.yaml", ()),
                          (directory / "targeting.yaml", ("sources.boards", "profile.id")),
                          (directory / "resume.yaml", ())):
        digest.update(b"\0" + path.name.encode() + b"\0")
        digest.update(_hashed_bytes(path, skipped))
    assert original[0] == digest.hexdigest()[:12]
    targeting = yaml.safe_load((directory / "targeting.yaml").read_text())
    targeting["filters"]["employer"] = {"exclude": ["Cloudflare"]}
    (directory / "targeting.yaml").write_text(yaml.safe_dump(targeting))
    excluded = hashes()
    assert excluded[0] != original[0]
    assert excluded[1:] == original[1:]
    del targeting["filters"]["employer"]
    (directory / "targeting.yaml").write_text(yaml.safe_dump(targeting))
    assert hashes() == original
