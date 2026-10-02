"""Score current Hard Filter passes in bounded, resumable Modal batches.

    python -m venator.score.run --profile NAME --estimate
    python -m venator.score.run --profile NAME --execute
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from venator.paths import install_stores
from venator.profile import add_profile_argument, profile_from_arguments
from venator.schedule.loop import append_heartbeat
from venator.score.model import KeepModel
from venator.score.selection import Input, select_inputs
from venator.score.store import Score, append_score

DAILY_USD = 1.0
MONTHLY_USD = 10.0
BATCH_SIZE = 500


def pause_reason(error: Exception) -> str:
    if isinstance(error, ModuleNotFoundError):
        return 'modal-unavailable'
    if isinstance(error, FileNotFoundError):
        return 'model-unavailable'
    text = str(error).casefold()
    if any(word in text for word in ('credit', 'payment', 'billing')):
        return 'out-of-credit'
    if type(error).__name__ in {'AuthError', 'NotFoundError'}:
        return 'account-unavailable'
    if type(error).__name__ == 'FunctionTimeoutError':
        return 'cap-reached'
    return 'connection-unavailable'


def receipt_key(model: KeepModel, inputs: list[Input]) -> str:
    value = ['prefix-v1', model.model_id, [(row.posting_key, row.input_hash) for row in inputs]]
    return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()


def spent(path: Path, at: str) -> tuple[float, float]:
    day = month = 0.0
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            if row.get('stage') != 'score' or not isinstance(row.get('usd'), (float, int)):
                continue
            when = str(row.get('at', ''))[:10]
            if when == at[:10]:
                day += row['usd']
            if when[:7] == at[:7]:
                month += row['usd']
    return day, month


def run_score(model: KeepModel | None, inputs: list[Input], install: Path, *,
              execute: bool, scored_at: str, runtime_factory=None,
              daily_usd: float = DAILY_USD, monthly_usd: float = MONTHLY_USD) -> dict:
    from venator.score.modal_runtime import MAXIMUM_USD, RESERVED_USD, projected_usd

    waiting = [row for row in inputs if row.score is None]
    report = {'scored': 0, 'waiting': len(waiting), 'paused': None}
    if not waiting:
        return report
    if model is None:
        return {**report, 'paused': 'model-unavailable'}
    if runtime_factory is None:
        from venator.score.modal_runtime import ModalRuntime
        runtime_factory = ModalRuntime
    runs = install / 'data/runs.jsonl'
    day, month = spent(runs, scored_at)

    def afford(usd: float) -> bool:
        return day + usd <= daily_usd + 1e-9 and month + usd <= monthly_usd + 1e-9

    def charge(usd: float, kind: str) -> None:
        nonlocal day, month
        append_heartbeat(runs, 'score', 'ok', {'usd': usd, 'batches': 1 if kind == 'batch' else 0,
                                             'waiting': report['waiting']}, at=scored_at)
        day += usd
        month += usd

    try:
        runtime = runtime_factory(model)
        # Bound the first preparation as well as inference. The partition uses the
        # complete selection so scores written on day 1 cannot renumber day 2.
        newest = sorted(sorted(inputs, key=lambda row: row.posting_key),
                        key=lambda row: row.discovered_at, reverse=True)
        for start in range(0, len(newest), BATCH_SIZE):
            batch = [row for row in newest[start:start + BATCH_SIZE] if row.score is None]
            if not batch:
                continue
            key = receipt_key(model, batch)
            receipt_path = install / 'build/keep-estimates' / f'{key}.json'
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
            else:
                if not afford(RESERVED_USD):
                    report['paused'] = 'budget-reached'
                    return report
                directory = f'/vol/{model.volume}/score/{uuid4().hex}'
                anonymous = [{'id': f'r{i:06d}', 'state': row.state} for i, row in enumerate(batch)]
                # Reserve before the paid call; a killed child cannot erase spend.
                charge(RESERVED_USD, 'estimate')
                receipt = runtime.prepare(anonymous, directory)
                receipt.update(directory=directory, model_id=model.model_id,
                               estimated_usd=projected_usd(receipt['padded_tokens']),
                               reserved_usd=RESERVED_USD)
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                receipt_path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8', newline='\n')
            report['estimate'] = receipt
            cost = receipt['estimated_usd']
            count = len(batch)
            if cost > MAXIMUM_USD or (execute and not afford(cost)):
                # Exact per-row padding lets inference use the largest newest-first
                # prefix that fits, without paying another preparation reservation.
                tokens = count = 0
                ceiling = min(MAXIMUM_USD, daily_usd - day, monthly_usd - month) if execute else MAXIMUM_USD
                for padded in receipt['row_padded_tokens']:
                    candidate = projected_usd(tokens + padded)
                    if candidate > ceiling + 1e-9:
                        break
                    tokens += padded
                    count += 1
                if not count:
                    report['paused'] = 'cap-reached' if cost > MAXIMUM_USD and len(batch) == 1 else 'budget-reached'
                    return report
                cost = projected_usd(tokens)
                report.setdefault('limited_batches', []).append({'selected': count, 'available': len(batch), 'usd': cost})
            if not execute:
                continue
            charge(cost, 'batch')
            by_id = {f'r{i:06d}': row for i, row in enumerate(batch[:count])}
            received: set[str] = set()
            chunks = (runtime.infer(receipt['directory'], receipt['items_sha256'], count)
                      if count < len(batch) else runtime.infer(receipt['directory'], receipt['items_sha256']))
            for chunk in chunks:
                for result in chunk.get('scores', []):
                    ident = result['id']
                    if ident not in by_id or ident in received:
                        raise ValueError('Unexpected score id')
                    row = by_id[ident]
                    score = Score(row.posting_key, row.input_hash, model.model_id,
                                  result['probability'], scored_at)
                    append_score(install / 'data/keep-scores', score)
                    received.add(ident)
                    report['scored'] += 1
                    report['waiting'] -= 1
            if len(received) != count:
                report['paused'] = 'connection-unavailable'
                return report
            if count < len(batch):
                report['paused'] = 'budget-reached' if not afford(projected_usd(0)) else 'cap-reached'
                return report
    except Exception as error:
        report['paused'] = pause_reason(error)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument('--as-of')
    parser.add_argument('--daily-usd', type=float, default=float(os.environ.get('VENATOR_SCORE_DAILY_USD', DAILY_USD)))
    parser.add_argument('--monthly-usd', type=float, default=float(os.environ.get('VENATOR_SCORE_MONTHLY_USD', MONTHLY_USD)))
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--estimate', action='store_true')
    actions.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not (0 < args.daily_usd < 1000 and 0 < args.monthly_usd < 10000):
        parser.error('Score ceilings must be positive dollar amounts')
    now = datetime.now(timezone.utc)
    as_of = args.as_of or now.date().isoformat()
    profile = profile_from_arguments(args)
    stores = install_stores()
    model, inputs = select_inputs(profile, stores, as_of)
    report = run_score(model, inputs, stores.path, execute=args.execute, scored_at=now.isoformat(),
                       daily_usd=args.daily_usd, monthly_usd=args.monthly_usd)
    if args.execute:
        append_heartbeat(stores.runs_file, 'score', 'paused' if report['paused'] else 'ok',
                         pause_reason=report['paused'], waiting=report['waiting'])
    print(json.dumps(report))


if __name__ == '__main__':
    main()
