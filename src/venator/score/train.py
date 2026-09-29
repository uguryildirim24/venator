"""Prepare private keep retraining, compare on newest labels, and swap by explicit choice.

Preparation reads the Install but writes nothing and makes no network call. Execute
requires a fresh local preview and an explicit press; neither preview nor comparison
changes the active adapter. The runtime seam keeps fixture checks off Modal.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from venator.discover.store import load_postings
from venator.paths import install_stores
from venator.profile import add_profile_argument, profile_from_arguments
from venator.score.compact import CompactStateBuilder
from venator.score.model import KeepModel, load_model, swap_model
from venator.track.store import load_events, verify_track_dir

_CONTACT = re.compile(rb'https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)', re.I)


@dataclass(frozen=True)
class Label:
    posting_key: str
    keep: int
    at: str


def track_labels(events: list[dict]) -> list[Label]:
    """Fold chronological Track events, never the disposable application state."""
    standing: dict[str, Label | None] = {}
    applied: set[str] = set()
    for row in events:
        key, event = row['posting_key'], row['event']
        if event == 'submit':
            applied.add(key)
            standing[key] = Label(key, 1, row['at'])
        elif key not in applied:
            if event == 'approve':
                standing[key] = Label(key, 1, row['at'])
            elif event == 'reject':
                standing[key] = Label(key, 0, row['at'])
            elif event == 'restore':
                standing[key] = None
    return sorted((label for label in standing.values() if label is not None), key=lambda row: (row.at, row.posting_key))


def csv_labels(path: Path) -> list[Label]:
    """The Owner supplies the private base CSV path; the CSV is never copied."""
    source = sys.stdin if str(path) == '-' else path.open(encoding='utf-8-sig', newline='')
    with source:
        reader = csv.DictReader(source)
        if not {'posting key', 'decision'} <= set(reader.fieldnames or ()):
            raise ValueError('Label CSV needs posting key and decision columns')
        labels = []
        for index, row in enumerate(reader):
            value = row['decision'].strip().lower()
            if value not in ('keep', 'skip'):
                raise ValueError('Expected keep or skip label')
            labels.append(Label(row['posting key'], int(value == 'keep'), f'0000-{index:08d}'))
        return labels


def training_labels(base: list[Label], events: list[dict]) -> list[Label]:
    result = {row.posting_key: row for row in base}
    result.update({row.posting_key: row for row in track_labels(events)})
    # A Restore clears a Dismiss even when that Posting appeared in the base CSV.
    cleared = {row['posting_key'] for row in events if row['event'] == 'restore'}
    cleared -= {row.posting_key for row in track_labels(events)}
    for key in cleared:
        result.pop(key, None)
    return sorted(result.values(), key=lambda row: (row.at, row.posting_key))


def upload_bytes(rows: list[tuple[Label, dict[str, str]]]) -> bytes:
    """Exact serialized upload, with only anonymous ids, labels and compact states."""
    output = bytearray()
    for index, (label, state) in enumerate(rows):
        line = json.dumps({'id': f'r{index:06d}', 'keep': label.keep, 'state': state},
                          ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
        output.extend(line)
    payload = bytes(output)
    if _CONTACT.search(payload) or any(label.posting_key.encode('utf-8') in payload for label, _ in rows):
        raise ValueError('A compact state contains a Posting key or contact detail')
    return payload


def preview(model: KeepModel, rows: list[tuple[Label, dict[str, str]]]) -> dict[str, Any]:
    training, holdout = split_labels(rows)
    payload = upload_bytes(training)
    if any(label.posting_key.encode('utf-8') in payload for label, _ in rows):
        raise ValueError('A compact state contains a Posting key')
    holdout_payload = json.dumps([state for _, state in holdout], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if _CONTACT.search(holdout_payload) or any(label.posting_key.encode('utf-8') in holdout_payload for label, _ in rows):
        raise ValueError('A holdout state contains a Posting key or contact detail')
    # Bind the approval to the active adapter and holdout labels too. Holdout
    # labels remain local, but changing one must invalidate the comparison.
    approval = json.dumps({'model_id': model.model_id, 'holdout_labels': [label.keep for label, _ in holdout]},
                          separators=(',', ':')).encode('utf-8')
    return {'rows': len(rows), 'training': len(training), 'holdout': len(holdout),
            'training_bytes': len(payload), 'holdout_bytes': len(holdout_payload),
            'sha256': hashlib.sha256(payload + b'\n' + holdout_payload + b'\n' + approval).hexdigest(),
            'leaves_mac': ['anonymous ids and 0/1 training labels', 'compact training and holdout states: employer names, titles, '
                           'Posting excerpts, confirmed résumé experiences, skills and education'],
            'stays_mac': ['holdout labels', 'Posting-key joins'],
            'not_uploaded': ['Posting keys', 'URLs', 'reasons', 'contact details']}


def split_labels(rows: list[Any]) -> tuple[list[Any], list[Any]]:
    if len(rows) < 2:
        raise ValueError('At least two labeled Postings are needed')
    cut = len(rows) - max(1, (len(rows) + 4) // 5)
    return rows[:cut], rows[cut:]


def prepare_rows(profile, stores, as_of: str, label_path: Path) -> tuple[KeepModel, list[tuple[Label, dict[str, str]]]]:
    model = load_model(stores.path)
    if model is None:
        raise ValueError('Score needs an active keep model')
    verify_track_dir(stores.track_dir, profile.identifier)
    labels = training_labels(csv_labels(label_path), load_events(stores.track_dir))
    postings = {row['key']: row for row in load_postings(stores.postings_dir)}
    builder = CompactStateBuilder(profile, as_of_month=as_of[:7])
    rows = []
    for label in labels:
        posting = postings.get(label.posting_key)
        if posting is None:
            raise ValueError('A labeled Posting is missing from this Install')
        company = posting.get('company') or profile.sources.names.get(str(posting.get('board', '')), '')
        rows.append((label, builder.build({**posting, 'company': company})))
    return model, rows


def compare(model: KeepModel, rows: list[tuple[Label, dict[str, str]]], runtime_factory: Callable,
            *, approved_sha256: str) -> dict[str, Any]:
    """Upload only after this exact payload was displayed and explicitly approved."""
    before = preview(model, rows)
    if before['sha256'] != approved_sha256:
        raise ValueError('Training preview changed; review the new payload before upload')
    training, holdout = split_labels(rows)
    runtime = runtime_factory(model)
    candidate = runtime.train(upload_bytes(training), model)
    old = runtime.probabilities(model, [state for _, state in holdout])
    new = runtime.probabilities(candidate, [state for _, state in holdout])
    if len(old) != len(holdout) or len(new) != len(holdout):
        raise ValueError('Incomplete holdout comparison')
    def correct(probabilities: list[float]) -> int:
        return sum(int((prob >= 0.5) == bool(label.keep)) for prob, (label, _) in zip(probabilities, holdout, strict=True))
    return {'old': {'correct': correct(old), 'total': len(holdout), 'model_id': model.model_id},
            'new': {'correct': correct(new), 'total': len(holdout), 'model_id': candidate.model_id},
            'candidate': {**asdict(candidate), 'model_id': candidate.model_id}, 'preview': before}


def swap(install: Path, comparison: dict, choice: str) -> KeepModel | None:
    if choice == 'old':
        return None
    if choice != 'new':
        raise ValueError('Pick old or new')
    current = load_model(install)
    if current is None or current.model_id != comparison['old']['model_id']:
        raise ValueError('Current adapter changed since the holdout comparison')
    candidate = KeepModel(**{key: value for key, value in comparison['candidate'].items() if key != 'model_id'})
    if candidate.model_id != comparison['new']['model_id']:
        raise ValueError('Candidate identity changed')
    swap_model(install, candidate)
    # Score selects by model_id: old rows are stale; score.run handles batches and both ceilings.
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument('--labels', type=Path)
    parser.add_argument('--approve-sha256', help='The exact hash shown in the pre-upload preview')
    parser.add_argument('--comparison', type=Path, help='A private comparison report in the Install')
    parser.add_argument('--pick', choices=('old', 'new'))
    args = parser.parse_args()
    profile = profile_from_arguments(args)
    stores = install_stores()
    if args.pick:
        if args.comparison is None:
            parser.error('Pick requires the private comparison report')
        report = json.loads(args.comparison.read_text(encoding='utf-8'))
        result = swap(stores.path, report, args.pick)
        print(json.dumps({'picked': args.pick, 'model_id': result.model_id if result else None}))
        return
    if args.labels is None:
        parser.error('Preview and training require a private label CSV')
    model, rows = prepare_rows(profile, stores, datetime.now(timezone.utc).date().isoformat(), args.labels)
    if args.approve_sha256 is None:
        print(json.dumps(preview(model, rows), indent=2))
        return
    from venator.score.train_runtime import ModalTrainRuntime
    report = compare(model, rows, ModalTrainRuntime, approved_sha256=args.approve_sha256)
    path = stores.build_dir / 'keep-training-comparison.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({'old': report['old'], 'new': report['new'], 'comparison': str(path)}))


if __name__ == '__main__':
    main()
