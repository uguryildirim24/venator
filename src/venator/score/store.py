"""Append-only keep probabilities; a Posting key alone never makes a score current."""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

@dataclass(frozen=True)
class Score:
    posting_key: str
    input_hash: str
    model_id: str
    probability: float
    scored_at: str

    def __post_init__(self) -> None:
        if not self.posting_key or not self.model_id:
            raise ValueError('A score requires a Posting key and model identity')
        if re.fullmatch(r'[a-f0-9]{64}', self.input_hash) is None:
            raise ValueError('A score requires a compact-state sha256')
        if isinstance(self.probability, bool) or not math.isfinite(self.probability) or not 0 <= self.probability <= 1:
            raise ValueError('A keep probability must be finite and between zero and one')
        if re.match(r'^\d{4}-\d{2}-\d{2}T', self.scored_at) is None:
            raise ValueError('A score requires an ISO timestamp')


def read_scores(directory: Path) -> dict[tuple[str, str, str], Score]:
    result: dict[tuple[str, str, str], Score] = {}
    for path in sorted(directory.glob('????-??-??.jsonl')):
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                score = Score(**json.loads(line))
                result[(score.posting_key, score.input_hash, score.model_id)] = score
    return result


def append_score(directory: Path, score: Score) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{score.scored_at[:10]}.jsonl'
    with path.open('a', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(asdict(score), ensure_ascii=False, separators=(',', ':')) + '\n')
        stream.flush()
