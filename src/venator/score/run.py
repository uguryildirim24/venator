"""Score current Hard Filter passes on Modal, then resume only missing scores.

    python -m venator.score.run --profile NAME --estimate
    python -m venator.score.run --profile NAME --execute

Estimate authorizes a bounded CPU token audit. Execute reuses that receipt or
prepares it first, then authorizes one bounded inference call. Neither trains.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from venator.paths import install_stores
from venator.profile import add_profile_argument, profile_from_arguments
from venator.schedule.loop import append_heartbeat
from venator.score.model import KeepModel
from venator.score.selection import Input, select_inputs
from venator.score.store import Score, append_score


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
    value = [model.model_id, [(row.posting_key, row.input_hash) for row in inputs]]
    return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode()).hexdigest()


def run_score(model: KeepModel | None, inputs: list[Input], install: Path, *,
              execute: bool, scored_at: str, runtime_factory=None) -> dict:
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
    key = receipt_key(model, waiting)
    receipt_path = install / 'build/keep-estimates' / f'{key}.json'
    try:
        runtime = runtime_factory(model)
        if receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        else:
            directory = f'/vol/{model.volume}/score/{uuid4().hex}'
            anonymous = [{'id': f'r{i:06d}', 'state': row.state} for i, row in enumerate(waiting)]
            receipt = runtime.prepare(anonymous, directory)
            receipt.update(directory=directory, model_id=model.model_id,
                           estimated_usd=projected_usd(receipt['padded_tokens']),
                           reserved_usd=RESERVED_USD)
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8', newline='\n')
    except Exception as error:
        return {**report, 'paused': pause_reason(error)}
    report['estimate'] = receipt
    if receipt['estimated_usd'] > MAXIMUM_USD or RESERVED_USD > MAXIMUM_USD:
        return {**report, 'paused': 'cap-reached'}
    if not execute:
        return report
    by_id = {f'r{i:06d}': row for i, row in enumerate(waiting)}
    received: set[str] = set()
    try:
        for chunk in runtime.infer(receipt['directory'], receipt['items_sha256']):
            if 'app_id' in chunk:
                report['inference_app_id'] = chunk['app_id']
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
            if 'seconds' in chunk:
                report['inference_seconds'] = chunk['seconds']
    except Exception as error:
        report['paused'] = pause_reason(error)
    if report['waiting'] and report['paused'] is None:
        report['paused'] = 'connection-unavailable'
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument('--as-of')
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--estimate', action='store_true')
    actions.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    as_of = args.as_of or now.date().isoformat()
    profile = profile_from_arguments(args)
    stores = install_stores()
    model, inputs = select_inputs(profile, stores, as_of)
    report = run_score(model, inputs, stores.path, execute=args.execute, scored_at=now.isoformat())
    if args.execute:
        append_heartbeat(stores.runs_file, 'score', 'paused' if report['paused'] else 'ok',
                         pause_reason=report['paused'], waiting=report['waiting'])
    print(json.dumps(report))


if __name__ == '__main__':
    main()
