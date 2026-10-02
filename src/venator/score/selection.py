"""Fresh Hard Filter passes and their exact keep-model input bindings."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from venator.discover.store import posting_revision, load_postings
from venator.match.store import load_latest_decisions, verify_decisions_dir
from venator.paths import InstallStores
from venator.profile import Profile
from venator.match.store import filters_version
from venator.score.compact import CompactStateBuilder, input_hash
from venator.score.model import KeepModel, load_model
from venator.score.store import Score, read_scores


@dataclass(frozen=True)
class Input:
    posting_key: str
    input_hash: str
    state: dict[str, str]
    score: Score | None
    discovered_at: str = ''


def filter_version(profile: Profile) -> str:
    return filters_version(profile.constraints_path, profile.targeting_path)


def inputs_for_passes(
    postings: Sequence[Mapping[str, object]], latest_hard: Mapping[str, Mapping[str, object]],
    profile: Profile, month: str, version: str, model: KeepModel | None,
    scores_dir: Path,
) -> list[Input]:
    passes = [posting for posting in postings if (
        (decision := latest_hard.get(str(posting['key']))) is not None
        and decision.get('verdict') == 'pass'
        and decision.get('filters_version') == version
        and decision.get('posting_version') == posting_revision(posting)
    )]
    if not passes:
        return []
    if model is None:
        return [Input(str(posting['key']), '', {}, None) for posting in passes]
    builder = CompactStateBuilder(profile, as_of_month=month)
    scores = read_scores(scores_dir)
    result = []
    for posting in sorted(passes, key=lambda p: str(p['key'])):
        company = posting.get('company') or profile.sources.names.get(str(posting.get('board', '')), '')
        state = builder.build({**posting, 'company': company})
        digest = input_hash(state)
        key = str(posting['key'])
        score = scores.get((key, digest, model.model_id)) if model else None
        result.append(Input(key, digest, state, score, str(posting.get('discovered_at') or '')))
    return result


def select_inputs(profile: Profile, stores: InstallStores, as_of: str) -> tuple[KeepModel | None, list[Input]]:
    verify_decisions_dir(stores.decisions_dir, profile.identifier)
    latest = load_latest_decisions(stores.decisions_dir)
    hard = {key: row for (key, stage), row in latest.items() if stage == 'hard_filter'}
    model = load_model(stores.path)
    inputs = inputs_for_passes(load_postings(stores.postings_dir), hard, profile, as_of[:7],
                              filter_version(profile), model,
                              stores.data_dir / 'keep-scores')
    return model, inputs


def score_plan(profile: Profile, stores: InstallStores, as_of: str) -> dict:
    model, inputs = select_inputs(profile, stores, as_of)
    waiting = [row for row in inputs if row.score is None]
    binding = [model.model_id if model else None, [(row.posting_key, row.input_hash) for row in waiting]]
    return {'asOf': as_of, 'postingCount': len(waiting), 'maximumUsd': 0.50,
            'selectionHash': hashlib.sha256(json.dumps(binding, separators=(',', ':')).encode()).hexdigest()}
