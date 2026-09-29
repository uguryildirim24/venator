"""Compact input bytes must not drift or retain Profile identity."""
from __future__ import annotations

import json
from pathlib import Path

from venator.profile import load_profile
from venator.score.compact import CompactStateBuilder, input_hash, serialize

FIXTURE = Path(__file__).parent / 'fixtures'


def test_compact_input_contract() -> None:
    profile = load_profile(FIXTURE / 'profile')
    builder = CompactStateBuilder(profile, as_of_month='2026-09')
    posting = json.loads((FIXTURE / 'posting.json').read_text())
    state = builder.build(posting)
    assert input_hash(state) == 'cde76fc9e40aee896eb3668de345182575c27930f6c299817f07ebb30329a73a'
    assert list(state) == ['title', 'employer', 'requirements_and_qualifications', 'confirmed_profile']
    for identity in builder.identity:
        assert identity.casefold() not in serialize(state).decode().casefold()
    changed = builder.build({**posting, 'title': 'Changed Posting title'})
    assert input_hash(changed) != input_hash(state)
