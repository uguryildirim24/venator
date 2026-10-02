from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from venator.score.model import KeepModel, load_model
from venator.score.run import run_score
from venator.score.selection import Input
from venator.score.store import read_scores
from venator.score.train import Label, compare, preview, swap, track_labels, training_labels, upload_bytes

MODEL = KeepModel('test', 'test', 'example-model', '/vol/example-model/base',
                  '/vol/example-model/adapter', 'a' * 64,
                  {'keep': {'type': 'noul', 'instructions': 'Keep?',
                            'criteria': {'false': 'Skip', 'true': 'Keep'}}})


def event(name: str, key: str = 'private-key', index: int = 1) -> dict:
    return {'event': name, 'posting_key': key, 'at': f'2026-09-{index:02d}T00:00:00+00:00',
            'detail': 'private Track reason'}


@pytest.mark.parametrize(('names', 'expected'), [
    (['approve'], 1),
    (['approve', 'prepare'], 1),
    (['submit', 'reject'], 1),
    (['reject'], 0),
    (['reject', 'restore'], None),
])
def test_chronological_track_labels_not_folded_application_states(names: list[str], expected: int | None) -> None:
    labels = track_labels([event(name, index=i + 1) for i, name in enumerate(names)])
    assert ([row.keep for row in labels] or [None])[0] == expected
    if names[-1] == 'restore':
        assert training_labels([Label('private-key', 0, '0000-00000000')],
                               [event(name, index=i + 1) for i, name in enumerate(names)]) == []


@pytest.mark.parametrize('names', [[], ['approve'], ['reject'], ['submit'], ['reject', 'restore']])
def test_outreach_is_not_a_training_label_or_a_change_to_one(names: list[str]) -> None:
    events = [event(name, index=i + 1) for i, name in enumerate(names)]
    outreach = {**event('outreach', index=10), 'detail': 'Alex Example'}
    assert track_labels([outreach]) == []
    assert track_labels([outreach, *events, outreach]) == track_labels(events)
    base = [Label('private-key', 0, '0000-00000000')]
    assert training_labels(base, [outreach, *events, outreach]) == training_labels(base, events)


def rows():
    return [(Label(f'private-key-{i}', i % 2, f'2026-09-{i + 1:02d}T00:00:00+00:00'),
             {'title': f'Role {i}', 'employer': 'Example Lab', 'requirements_and_qualifications': 'lab work',
              'confirmed_profile': 'Education: biochemistry'}) for i in range(5)]


def test_exact_serialized_payload_excludes_keys_urls_contacts_and_reasons() -> None:
    data = rows()
    wire = upload_bytes(data)
    assert b'private-key' not in wire
    assert b'example.com' not in wire and b'@' not in wire
    assert b'private Track reason' not in wire
    assert set(json.loads(wire.splitlines()[0])) == {'id', 'keep', 'state'}
    assert 'employer names' in ' '.join(preview(MODEL, data)['leaves_mac'])
    for leak in ('private-key-0', 'https://example.com/job', 'person@example.com', '123-555-0123'):
        changed = [(data[0][0], {**data[0][1], 'requirements_and_qualifications': leak})]
        with pytest.raises(ValueError):
            upload_bytes(changed)
    with pytest.raises(ValueError):
        upload_bytes([(data[0][0], {**data[0][1], 'title': data[1][0].posting_key}), data[1]])


def test_fake_runtime_scores_both_adapters_on_newest_holdout_and_requires_preview() -> None:
    data = rows()
    class Fake:
        def __init__(self, model):
            pass
        def train(self, payload, model):
            assert len(payload.splitlines()) == 4
            return replace(model, adapter_sha256='b' * 64)
        def probabilities(self, model, states):
            assert states == [data[-1][1]]
            return [0.8 if model.adapter_sha256.startswith('b') else 0.2]
    approved = preview(MODEL, data)['sha256']
    assert preview(MODEL, [*data[:-1], (replace(data[-1][0], keep=1 - data[-1][0].keep), data[-1][1])])['sha256'] != approved
    assert preview(replace(MODEL, adapter_sha256='c' * 64), data)['sha256'] != approved
    with pytest.raises(ValueError, match='preview changed'):
        compare(MODEL, data, Fake, approved_sha256='0' * 64)
    result = compare(MODEL, data, Fake, approved_sha256=approved)
    assert result['old']['correct'] == 1 and result['new']['correct'] == 0
    assert result['old']['total'] == result['new']['total'] == 1


def test_swap_only_on_new_pick_stales_scores_and_next_score_batches_under_ceiling(tmp_path: Path, monkeypatch) -> None:
    import venator.score.run as scoring
    monkeypatch.setattr(scoring, 'BATCH_SIZE', 1)
    old = tmp_path / 'keep-model.json'
    old.write_text(json.dumps({**MODEL.__dict__, 'model_id': MODEL.model_id}))
    candidate = replace(MODEL, adapter_sha256='b' * 64)
    comparison = {'old': {'model_id': MODEL.model_id}, 'new': {'model_id': candidate.model_id},
                  'candidate': {**candidate.__dict__, 'model_id': candidate.model_id}}
    assert swap(tmp_path, comparison, 'old') is None and load_model(tmp_path) == MODEL
    swap(tmp_path, comparison, 'new')
    assert load_model(tmp_path) == candidate
    rows_to_score = [Input(f'post-{i}', f'{i:064x}', {'title': str(i)}, None) for i in range(3)]
    class Fake:
        def __init__(self, model):
            assert model == candidate
        def prepare(self, inputs, directory):
            return {'padded_tokens': 1000, 'items_sha256': 'c' * 64}
        def infer(self, directory, digest):
            yield {'scores': [{'id': 'r000000', 'probability': .75}]}
    first = run_score(candidate, rows_to_score, tmp_path, execute=True, scored_at='2026-09-29T00:00:00+00:00', runtime_factory=Fake, daily_usd=.7)
    assert first['scored'] == 1 and first['waiting'] == 2 and first['paused'] == 'budget-reached'
    assert all(key[2] == candidate.model_id for key in read_scores(tmp_path / 'data/keep-scores'))
    assert len([json.loads(line) for line in (tmp_path / 'data/runs.jsonl').read_text().splitlines()]) == 2
