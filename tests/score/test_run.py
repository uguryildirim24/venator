from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from venator.discover.store import posting_revision
from venator.profile import load_profile
from venator.score.model import KeepModel
from venator.score.run import run_score
from venator.score.selection import Input, inputs_for_passes
from venator.score.store import read_scores

FIXTURE = Path(__file__).parent / 'fixtures'
MODEL = KeepModel('test', 'test', 'fixture-model', '/vol/fixture-model/base',
                  '/vol/fixture-model/adapter', 'a' * 64,
                  {'keep': {'type': 'noul', 'instructions': 'Keep?',
                            'criteria': {'false': 'Skip', 'true': 'Keep'}}})


class Runtime:
    def __init__(self, model):
        self.model = model

    def prepare(self, rows, directory):
        assert all(set(row) == {'id', 'state'} for row in rows)
        assert all(row['id'].startswith('r') for row in rows)
        return {'padded_tokens': 1000 * len(rows), 'row_padded_tokens': [1000] * len(rows),
                'items_sha256': 'b' * 64, 'postings': len(rows)}

    def infer(self, directory, items_sha256):
        yield {'scores': [{'id': 'r000000', 'probability': 0.8}]}
        raise ConnectionError('network offline')


def test_partial_scores_survive_pause_and_are_not_selected_again(tmp_path: Path) -> None:
    profile = load_profile(FIXTURE / 'profile')
    posting = json.loads((FIXTURE / 'posting.json').read_text())
    postings = [{**posting, 'key': key} for key in ('one', 'two')]
    hard = {p['key']: {'verdict': 'pass', 'filters_version': 'rules',
                      'posting_version': posting_revision(p)} for p in postings}
    directory = tmp_path / 'data/keep-scores'
    inputs = inputs_for_passes(postings, hard, profile, '2026-09', 'rules', MODEL, directory)
    report = run_score(MODEL, inputs, tmp_path, execute=True,
                       scored_at='2026-09-29T00:00:00+00:00', runtime_factory=Runtime)
    assert (report['scored'], report['waiting'], report['paused']) == (1, 1, 'connection-unavailable')
    assert len(read_scores(directory)) == 1
    resumed = inputs_for_passes(postings, hard, profile, '2026-09', 'rules', MODEL, directory)
    assert [row.posting_key for row in resumed if row.score is None] == ['two']
    changed = [{**postings[0], 'title': 'Changed title'}]
    hard['one']['posting_version'] = posting_revision(changed[0])
    assert inputs_for_passes(changed, hard, profile, '2026-09', 'rules', MODEL, directory)[0].score is None
    assert all(row.score is None for row in inputs_for_passes(
        postings, hard, profile, '2026-09', 'rules', replace(MODEL, adapter_sha256='c' * 64), directory))
    assert inputs_for_passes(postings, hard, profile, '2026-09', 'new rules', MODEL, directory) == []


def test_no_modal_no_model_no_credit_pause_without_scores(tmp_path: Path) -> None:
    rows = [Input('posting', 'a' * 64, {}, None)]
    for error, reason in [(ModuleNotFoundError('modal'), 'modal-unavailable'),
                          (FileNotFoundError('base snapshot'), 'model-unavailable'),
                          (RuntimeError('out of credit'), 'out-of-credit')]:

        def unavailable(model):
            raise error
        report = run_score(MODEL, rows, tmp_path, execute=True,
                           scored_at='2026-09-29T00:00:00+00:00', runtime_factory=unavailable)
        assert report == {'scored': 0, 'waiting': 1, 'paused': reason}
    assert run_score(None, rows, tmp_path, execute=True, scored_at='2026-09-29T00:00:00+00:00')['paused'] == 'model-unavailable'
    assert not (tmp_path / 'data/keep-scores').exists()


def test_batched_budget_ledger_resumes_next_day_without_repreparing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr('venator.score.run.BATCH_SIZE', 1)
    prepared = []

    class Complete(Runtime):
        def prepare(self, rows, directory):
            prepared.append(rows[0]['state']['title'])
            return super().prepare(rows, directory)

        def infer(self, directory, items_sha256):
            yield {'scores': [{'id': 'r000000', 'probability': 0.8}]}

    rows = [Input(f'post-{i}', f'{i:064x}', {'title': str(i)}, None) for i in range(3)]
    first = run_score(MODEL, rows, tmp_path, execute=True, scored_at='2026-09-29T00:00:00+00:00',
                      runtime_factory=Complete, daily_usd=0.7)
    assert (first['scored'], first['waiting'], first['paused']) == (1, 2, 'budget-reached')
    ledger = [json.loads(line) for line in (tmp_path / 'data/runs.jsonl').read_text().splitlines()]
    assert [row['batches'] for row in ledger] == [0, 1]
    assert all(row['stage'] == 'score' and row['usd'] > 0 for row in ledger)
    remaining = [replace(row, score=read_scores(tmp_path / 'data/keep-scores').get(
        (row.posting_key, row.input_hash, MODEL.model_id))) for row in rows]
    second = run_score(MODEL, remaining, tmp_path, execute=True, scored_at='2026-09-30T00:00:00+00:00',
                       runtime_factory=Complete, daily_usd=0.7)
    assert (second['scored'], second['waiting'], second['paused']) == (1, 1, 'budget-reached')
    assert prepared == ['0', '1']  # batch 1 was not prepared again
    remaining = [replace(row, score=read_scores(tmp_path / 'data/keep-scores').get(
        (row.posting_key, row.input_hash, MODEL.model_id))) for row in rows]
    third = run_score(MODEL, remaining, tmp_path, execute=True, scored_at='2026-10-01T00:00:00+00:00',
                      runtime_factory=Complete, daily_usd=0.7)
    assert (third['scored'], third['waiting'], third['paused']) == (1, 0, None)


def test_loop_rollover_scores_largest_newest_prefix_over_days(tmp_path: Path, capsys) -> None:
    from venator.schedule.loop import append_heartbeat, run_loop
    from venator.score.modal_runtime import RESERVED_USD, projected_usd
    from venator.score.run import spent

    prepared = {}
    inferred = []

    class Rollover(Runtime):
        def prepare(self, rows, directory):
            prepared[directory] = rows
            return {'padded_tokens': 100_000 * len(rows), 'row_padded_tokens': [100_000] * len(rows),
                    'items_sha256': 'b' * 64, 'postings': len(rows)}

        def infer(self, directory, items_sha256, max_postings=None):
            rows = prepared[directory][:max_postings]
            inferred.extend(row['state']['title'] for row in rows)
            yield {'scores': [{'id': row['id'], 'probability': 0.8} for row in rows]}

    rows = [Input(f'post-{i:03d}', f'{i:064x}', {'title': str(i)}, None,
                  f'2026-09-{i // 24 + 1:02d}T{i % 24:02d}:00:00+00:00') for i in range(200)]
    heartbeat = tmp_path / 'data/runs.jsonl'
    append_heartbeat(heartbeat, 'score', 'ok', {'usd': 10}, at='2026-09-30T00:00:00+00:00')
    reports = []
    for day in range(1, 4):
        at = f'2026-10-{day:02d}T00:00:00+00:00'
        earlier_spend = 0.05 if day == 1 else 0.0
        if earlier_spend:
            append_heartbeat(heartbeat, 'score', 'ok', {'usd': earlier_spend}, at=at)
        saved = read_scores(tmp_path / 'data/keep-scores')
        inputs = [replace(row, score=saved.get((row.posting_key, row.input_hash, MODEL.model_id))) for row in rows]

        def score_action():
            report = run_score(MODEL, inputs, tmp_path, execute=True, scored_at=at, runtime_factory=Rollover)
            reports.append(report)
            return report

        assert run_loop(only=['score', 'view'], repository=tmp_path, heartbeat_path=heartbeat,
                        stage_callables={'score': score_action, 'view': lambda: {}}) == 0
        daily, monthly = spent(heartbeat, at)
        assert daily <= 1.0 and monthly <= 10.0
        if reports[-1]['waiting']:
            count = reports[-1]['scored']
            assert earlier_spend + RESERVED_USD + projected_usd(count * 100_000) <= 1.0
            assert (earlier_spend + RESERVED_USD + projected_usd((count + 1) * 100_000) > 1.0
                    or projected_usd((count + 1) * 100_000) > 0.5)
    assert reports[0]['scored'] > 0 and reports[0]['paused'] == 'budget-reached'
    assert reports[-1]['waiting'] == 0 and reports[-1]['paused'] is None
    assert inferred == [str(i) for i in reversed(range(200))]
    assert 'newest' in capsys.readouterr().out


def test_monthly_ceiling_stops_while_daily_ceiling_has_room(tmp_path: Path) -> None:
    rows = [Input('posting', 'a' * 64, {}, None)]
    result = run_score(MODEL, rows, tmp_path, execute=True,
                       scored_at='2026-09-29T00:00:00+00:00', runtime_factory=Runtime,
                       daily_usd=2.0, monthly_usd=0.5)
    assert result['paused'] == 'budget-reached'
    assert result['scored'] == 0
    ledger = [json.loads(line) for line in (tmp_path / 'data/runs.jsonl').read_text().splitlines()]
    assert len(ledger) == 1 and ledger[0]['batches'] == 0


def test_modal_generator_registers_without_unsupported_retry_configuration(monkeypatch) -> None:
    from venator.score.modal_runtime import ModalRuntime
    monkeypatch.setenv('MODAL_PROFILE', 'test')
    monkeypatch.setenv('MODAL_ENVIRONMENT', 'main')
    runtime = ModalRuntime(MODEL)
    assert runtime.infer_function is not None
    assert runtime.prepare_function is not None


def test_estimate_over_cap_never_starts_inference(tmp_path: Path) -> None:
    class OverCap(Runtime):
        def prepare(self, rows, directory):
            return {'padded_tokens': 100_000_000, 'row_padded_tokens': [100_000_000], 'items_sha256': 'b' * 64}

        def infer(self, directory, items_sha256):
            raise AssertionError('inference must not start')
    report = run_score(MODEL, [Input('posting', 'a' * 64, {}, None)], tmp_path, execute=True,
                       scored_at='2026-09-29T00:00:00+00:00', runtime_factory=OverCap)
    assert report['paused'] == 'cap-reached'
    assert report['scored'] == 0
