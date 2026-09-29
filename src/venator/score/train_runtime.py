"""Modal adapter for an explicitly approved private keep training run."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from venator.score.model import KeepModel
from venator.score import train_worker


class ModalTrainRuntime:
    def __init__(self, model: KeepModel):
        os.environ['MODAL_PROFILE'] = model.modal_profile
        os.environ['MODAL_ENVIRONMENT'] = 'main'
        import modal

        self.model = model
        self.app = modal.App(model.app + '-train', include_source=False)
        self.volume = modal.Volume.from_name(model.volume, create_if_missing=False)
        root = f'/vol/{model.volume}'
        image = (modal.Image.debian_slim(python_version='3.12').apt_install('git')
                 .pip_install('torch==2.14.0', 'transformers==5.17.0', 'flash-linear-attention==0.5.2',
                              'numpy==1.26.4', 'peft==0.18.1',
                              'decider-ai @ git+https://github.com/Mapika/decider.git@a5120cce45b9ff70964fac54ea6e8c1ac5b08c7f')
                 .env({'HF_HOME': root + '/public-cache', 'HF_HUB_DISABLE_TELEMETRY': '1',
                       'HF_HUB_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false', 'PYTHONPATH': '/root',
                       'TRITON_CACHE_DIR': root + '/triton-cache', 'TORCHINDUCTOR_CACHE_DIR': root + '/torch-cache'})
                 .add_local_file(Path(__file__).with_name('inference.py'), '/root/venator/score/inference.py')
                 .add_local_file(Path(__file__).with_name('train_worker.py'), '/root/venator/score/train_worker.py'))
        self.worker = self.app.function(image=image, gpu='H100', cpu=(2, 2), memory=(16384, 16384),
                                        volumes={'/vol': self.volume}, timeout=1200, startup_timeout=180,
                                        retries=0, max_containers=1, scaledown_window=2)(train_worker.train)

    def train(self, payload: bytes, model: KeepModel) -> KeepModel:
        directory = f'/vol/{model.volume}/retrain/{uuid4().hex}/epoch-2-adapter'
        with self.app.run():
            result = self.worker.remote(payload, model.inference_metadata(), directory, self.volume)
        return replace(model, adapter_path=result['adapter_path'], adapter_sha256=result['adapter_sha256'])

    def probabilities(self, model: KeepModel, states: list[dict[str, str]]) -> list[float]:
        from venator.score.modal_runtime import ModalRuntime
        runtime = ModalRuntime(model)
        directory = f'/vol/{model.volume}/retrain/{uuid4().hex}/holdout'
        receipt = runtime.prepare([{'id': f'r{i:06d}', 'state': state} for i, state in enumerate(states)], directory)
        scores = {}
        for chunk in runtime.infer(directory, receipt['items_sha256']):
            for row in chunk.get('scores', []):
                scores[row['id']] = row['probability']
        return [scores[f'r{i:06d}'] for i in range(len(states))]
