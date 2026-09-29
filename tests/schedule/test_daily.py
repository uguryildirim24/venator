from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from venator.schedule import loop


def test_dry_run_lists_daily_stages(tmp_path: Path, capsys) -> None:
    assert loop.run_loop(dry_run=True, repository=tmp_path) == 0
    output = capsys.readouterr().out
    for stage in ('discover', 'filters', 'score', 'recheck', 'view', 'notify'):
        assert f'. {stage}:' in output
    assert '7. commit:' not in output


def test_no_new_picks_does_not_open_app(tmp_path: Path, monkeypatch) -> None:
    view = tmp_path / 'build/venator.db'
    view.parent.mkdir(parents=True)
    with sqlite3.connect(view) as db:
        db.executescript('''
            CREATE TABLE postings (key TEXT, title TEXT, discovered_at TEXT);
            CREATE TABLE keep_scores (posting_key TEXT, probability REAL, scored_at TEXT);
            CREATE TABLE assessments (posting_key TEXT, listing_status TEXT);
            CREATE TABLE hard_filter_latest (posting_key TEXT, decision_id INTEGER);
            CREATE TABLE decisions (id INTEGER, verdict TEXT);
            CREATE TABLE application_states (posting_key TEXT);
            CREATE TABLE runs (at TEXT, stage TEXT, status TEXT);
        ''')
    monkeypatch.setattr('venator.paths.install_stores', lambda: SimpleNamespace(path=tmp_path))
    monkeypatch.setattr(loop.subprocess, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('open called')))
    receipt = view.parent / 'picks-notification.json'
    receipt.write_text('{"count": 1, "title": "Old pick"}', encoding='utf-8')
    assert loop.notify(tmp_path) == {'new_picks': 0}
    assert not receipt.exists()


def test_notify_only_current_open_picks_from_rebuilt_view(tmp_path: Path, monkeypatch) -> None:
    view = tmp_path / 'build/venator.db'
    view.parent.mkdir(parents=True)
    today = date.today().isoformat()
    with sqlite3.connect(view) as db:
        db.executescript('''
            CREATE TABLE postings (key TEXT, title TEXT, discovered_at TEXT);
            CREATE TABLE keep_scores (posting_key TEXT, probability REAL, scored_at TEXT);
            CREATE TABLE assessments (posting_key TEXT, listing_status TEXT);
            CREATE TABLE hard_filter_latest (posting_key TEXT, decision_id INTEGER);
            CREATE TABLE decisions (id INTEGER, verdict TEXT);
            CREATE TABLE application_states (posting_key TEXT);
            CREATE TABLE runs (at TEXT, stage TEXT, status TEXT);
        ''')
        db.execute('INSERT INTO runs VALUES (?, ?, ?)', (today + 'T23:00:00+00:00', 'discover', 'ok'))
        for i, status in enumerate(('closed', 'open', 'open')):
            db.execute('INSERT INTO postings VALUES (?, ?, ?)', (str(i), f'Pick {i}', today + 'T00:00:00+00:00'))
            db.execute('INSERT INTO assessments VALUES (?, ?)', (str(i), status))
            db.execute('INSERT INTO decisions VALUES (?, ?)', (i, 'pass'))
            db.execute('INSERT INTO hard_filter_latest VALUES (?, ?)', (str(i), i))
        # The changed open Posting 2 has no current score in the rebuilt View.
        for i in (0, 1):
            db.execute('INSERT INTO keep_scores VALUES (?, ?, ?)', (str(i), 0.8, today))
    monkeypatch.setattr('venator.paths.install_stores', lambda: SimpleNamespace(path=tmp_path))
    calls = []
    monkeypatch.setattr(loop.subprocess, 'run', lambda args, **kwargs: calls.append(args))
    assert loop.notify(tmp_path) == {'new_picks': 1}
    assert calls == [['open', '-g', 'venator://picks']]
    assert 'Pick 1' in (view.parent / 'picks-notification.json').read_text(encoding='utf-8')


def test_recheck_changed_open_pick_refilters_before_view(tmp_path: Path, monkeypatch) -> None:
    from venator.score.selection import Input
    from venator.score.store import Score
    from venator.paths import install_stores
    monkeypatch.setenv('VENATOR_HOME', str(tmp_path))
    stores = install_stores()
    posting = {'key': 'fixture', 'board': 'fixture', 'source': 'greenhouse', 'title': 'Old',
               'discovered_at': date.today().isoformat() + 'T00:00:00+00:00'}
    stores.runs_file.parent.mkdir(parents=True, exist_ok=True)
    stores.runs_file.write_text('{"at": "2026-09-29T23:00:00+00:00", "stage": "discover", "status": "ok"}\n',
                                encoding='utf-8', newline='\n')
    score = Score('fixture', 'a' * 64, 'model', 0.8, date.today().isoformat() + 'T00:00:00+00:00')
    monkeypatch.setattr('venator.profile.resolve_profile', lambda name: object())
    monkeypatch.setattr('venator.score.selection.select_inputs', lambda *args: (None, [Input('fixture', 'digest', {}, score)]))
    monkeypatch.setattr('venator.discover.store.load_postings', lambda path: [posting])
    monkeypatch.setattr('venator.discover.refresh.refresh_posting', lambda row: {**row, 'title': 'Changed'})
    appended = []
    replay = []
    monkeypatch.setattr('venator.discover.store.append_observations', lambda path, rows: appended.extend(rows))
    monkeypatch.setattr(loop, 'run_module', lambda *args, **kwargs: replay.append(args[0]))
    assert loop.recheck(tmp_path, 'fixture') == {'changed': 1}
    assert appended[0]['title'] == 'Changed'
    assert replay == ['venator.match.run']


def test_recheck_reads_saved_posting_from_live_track_not_stale_view(tmp_path: Path, monkeypatch) -> None:
    from venator.paths import install_stores
    from venator.track.store import append_events
    monkeypatch.setenv('VENATOR_HOME', str(tmp_path))
    stores = install_stores()
    posting = {'key': 'saved', 'board': 'fixture', 'source': 'greenhouse', 'title': 'Old',
               'discovered_at': '2026-01-01T00:00:00+00:00'}
    stores.runs_file.parent.mkdir(parents=True, exist_ok=True)
    stores.runs_file.write_text(''.join(
        f'{{"at":"2026-09-{day}T12:00:00+00:00","stage":"discover","status":"ok"}}\n'
        for day in ('28', '29')), encoding='utf-8')
    append_events(stores.track_dir, [{
        'posting_key': 'saved', 'event': 'approve', 'actor': 'owner', 'detail': None,
        'at': '2026-09-29T13:00:00+00:00', 'profile_id': 'person',
    }])
    monkeypatch.setattr('venator.profile.resolve_profile', lambda name: SimpleNamespace(identifier='person'))
    monkeypatch.setattr('venator.score.selection.select_inputs', lambda *args: (None, []))
    monkeypatch.setattr('venator.discover.store.load_postings', lambda path: [posting])
    monkeypatch.setattr('venator.discover.refresh.refresh_posting', lambda row: {**row, 'title': 'Changed'})
    appended = []
    monkeypatch.setattr('venator.discover.store.append_observations', lambda path, rows: appended.extend(rows))
    monkeypatch.setattr(loop, 'run_module', lambda *args, **kwargs: {})
    assert loop.recheck(tmp_path, 'person') == {'changed': 1}
    assert appended[0]['title'] == 'Changed'
