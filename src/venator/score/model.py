"""Private model location and fixed question metadata belong to the Install."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class KeepModel:
    modal_profile: str
    app: str
    volume: str
    base_path: str
    adapter_path: str
    adapter_sha256: str
    question: dict[str, Any]

    @property
    def model_id(self) -> str:
        # The adapter is the model identity; its base and fixed question are also
        # bound so editing model metadata cannot resurrect incompatible scores.
        metadata = [self.adapter_sha256, self.base_path, self.question]
        digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return f'keep:{digest}'

    def inference_metadata(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in ('base_path', 'adapter_path', 'adapter_sha256', 'question')}


def load_model(install: Path) -> KeepModel | None:
    path = install / 'keep-model.json'
    if not path.is_file():
        return None
    metadata = json.loads(path.read_text(encoding='utf-8'))
    model_id = metadata.pop('model_id')
    model = KeepModel(**metadata)
    if model_id != model.model_id:
        raise ValueError('Keep model identity does not match its adapter and fixed question')
    if not model.volume or '/' in model.volume or model.volume in ('.', '..'):
        raise ValueError('Keep scoring requires a named Modal volume')
    for value in (model.base_path, model.adapter_path):
        parts = Path(value).parts
        if len(parts) < 4 or parts[:3] != ('/', 'vol', model.volume) or '..' in parts:
            raise ValueError('Keep model artifacts must be under the configured volume')
    if len(model.adapter_sha256) != 64 or any(c not in '0123456789abcdef' for c in model.adapter_sha256):
        raise ValueError('Keep adapter identity must be a sha256')
    if set(model.question) != {'keep'} or model.question['keep'].get('type') != 'noul':
        raise ValueError('Keep scoring requires one binary keep question')
    return model
