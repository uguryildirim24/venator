from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from venator.discover.store import posting_revision
from venator.match.store import append_decisions
from venator.profile import load_profile
from venator.score.compact import CompactStateBuilder, input_hash
from venator.score.model import KeepModel
from venator.score.selection import filter_version, inputs_for_passes
from venator.score.store import Score, append_score
from venator.view.build import build_database

FIXTURE = Path(__file__).parent / 'fixtures'


def test_view_carries_latest_same_model_until_current_binding_lands(tmp_path: Path, monkeypatch) -> None:
    model = KeepModel('test', 'test', 'sample-model', '/vol/sample-model/base',
                      '/vol/sample-model/adapter', 'a' * 64,
                      {'keep': {'type': 'noul', 'instructions': 'Keep?', 'criteria': {'false': 'Skip', 'true': 'Keep'}}})
    monkeypatch.setattr('venator.view.build.load_model', lambda _: model)
    profile = load_profile(FIXTURE / 'profile')
    profile.resume['experience'][0]['dates'] = 'January 2025 - Present'
    posting = json.loads((FIXTURE / 'posting.json').read_text())
    data = tmp_path / 'data'
    postings = data / 'postings'
    postings.mkdir(parents=True)
    view = tmp_path / 'build/venator.db'
    version = filter_version(profile)
    rows = [{**posting, 'key': key} for key in ('keep', 'explore', 'hidden', 'never', 'other')]
    (postings / '2026-09-29.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    hard = {row['key']: {'posting_key': row['key'], 'stage': 'hard_filter', 'verdict': 'pass',
                        'filters_version': version, 'posting_version': posting_revision(row),
                        'decided_at': '2026-09-29T00:00:00+00:00'} for row in rows}
    append_decisions(data / 'decisions', list(hard.values()))

    def score(key, month, probability, active=model, at=None):
        state = CompactStateBuilder(profile, as_of_month=month).build(posting)
        append_score(data / 'keep-scores', Score(key, input_hash(state), active.model_id,
                                               probability, at or f'{month}-29T00:00:00+00:00'))

    def rebuild():
        build_database(postings, data / 'decisions', view, data / 'track', data / 'runs.jsonl',
                       profile=profile, as_of_month='2026-10')
        with sqlite3.connect(view) as db:
            return {key: (probability, carried) for key, probability, carried in db.execute(
                'SELECT posting_key, probability, carried FROM keep_scores')}

    score('keep', '2026-08', 0.2)
    score('keep', '2026-09', 0.8)
    score('explore', '2026-09', 0.3)
    score('hidden', '2026-09', 0.04)
    other = replace(model, adapter_sha256='c' * 64)
    score('other', '2026-09', 0.9, other)
    # A later score from another adapter must not mask a same-model carry.
    score('keep', '2026-09', 0.1, other, '2026-09-30T00:00:00+00:00')
    assert rebuild() == {'keep': (0.8, 1), 'explore': (0.3, 1), 'hidden': (0.04, 1),
                         'never': (None, 0), 'other': (None, 0)}
    inputs = inputs_for_passes(rows, hard, profile, '2026-10', version, model, data / 'keep-scores')
    assert all(row.score is None for row in inputs)  # Carry never changes re-score selection.
    score('keep', '2026-10', 0.05, at='2026-10-01T00:00:00+00:00')
    assert rebuild()['keep'] == (0.05, 0)
    inputs = inputs_for_passes(rows, hard, profile, '2026-10', version, model, data / 'keep-scores')
    assert [row.posting_key for row in inputs if row.score is not None] == ['keep']
    # Retraining swaps cannot carry the previous adapter's remaining scores.
    model = replace(model, adapter_sha256='d' * 64)
    assert all(value == (None, 0) for value in rebuild().values())
