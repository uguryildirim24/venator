from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from venator.discover.store import posting_revision
from venator.match.store import append_decisions
from venator.profile import load_profile
from venator.score.compact import CompactStateBuilder, input_hash
from venator.score.model import KeepModel
from venator.score.selection import filter_version
from venator.score.store import Score, append_score
from venator.view.build import build_database

FIXTURE = Path(__file__).parents[1] / 'qualify/fixtures/jev'


def test_view_never_materializes_a_score_for_changed_compact_input(tmp_path: Path, monkeypatch) -> None:
    model = KeepModel('test', 'test', 'sample-model', '/vol/sample-model/base',
                      '/vol/sample-model/adapter', 'a' * 64,
                      {'keep': {'type': 'noul', 'instructions': 'Keep?', 'criteria': {'false': 'Skip', 'true': 'Keep'}}})
    monkeypatch.setattr('venator.view.build.load_model', lambda _: model)
    profile = load_profile(FIXTURE / 'profile')
    posting = json.loads((FIXTURE / 'posting.json').read_text())
    data = tmp_path / 'data'
    postings = data / 'postings'
    postings.mkdir(parents=True)
    view = tmp_path / 'build/venator.db'
    version = filter_version(profile, data / 'qualifications')

    def rebuild(row):
        with (postings / '2026-09-29.jsonl').open('a') as output:
            output.write(json.dumps(row) + '\n')
        append_decisions(data / 'decisions', [{'posting_key': row['key'], 'stage': 'hard_filter',
                         'verdict': 'pass', 'filters_version': version,
                         'posting_version': posting_revision(row), 'decided_at': '2026-09-29T00:00:00+00:00'}])
        build_database(postings, data / 'decisions', view, data / 'track', data / 'runs.jsonl',
                       profile=profile, qualifications_dir=data / 'qualifications', as_of_month='2026-09')
        with sqlite3.connect(view) as db:
            return db.execute('SELECT probability FROM keep_scores WHERE posting_key = ?', (row['key'],)).fetchone()

    state = CompactStateBuilder(profile, as_of_month='2026-09').build(posting)
    append_score(data / 'keep-scores', Score(posting['key'], input_hash(state), model.model_id,
                                           0.8, '2026-09-29T00:00:00+00:00'))
    assert rebuild(posting) == (0.8,)
    assert rebuild({**posting, 'title': 'Different title'}) == (None,)
