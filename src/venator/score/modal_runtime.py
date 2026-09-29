"""Bounded, explicit Modal execution. Imported only by a Score execute/estimate."""
from __future__ import annotations

import os
from pathlib import Path

from venator.score.model import KeepModel
from venator.score import inference

# H100 + two CPU cores + 16 GiB, and CPU preflight with two cores + 8 GiB.
# Reservations include startup and scaledown, with retries disabled.
GPU_HOURLY_USD = 4.1726
CPU_HOURLY_USD = 0.1586
CPU_TIMEOUT = 240
GPU_TIMEOUT = 320
STARTUP_TIMEOUT = 90
SCALEDOWN = 2
MAXIMUM_USD = 0.50
RESERVED_USD = ((CPU_TIMEOUT + STARTUP_TIMEOUT + SCALEDOWN) * CPU_HOURLY_USD
                + (GPU_TIMEOUT + STARTUP_TIMEOUT + SCALEDOWN) * GPU_HOURLY_USD) / 3600


def projected_usd(padded_tokens: int) -> float:
    # Estimate inference time from measured token throughput.
    # Include 90 s for model loading/startup and the entire CPU reservation.
    seconds = padded_tokens * 546.080500023 / 12841984 + 90
    return seconds * GPU_HOURLY_USD / 3600 + (CPU_TIMEOUT + STARTUP_TIMEOUT + SCALEDOWN) * CPU_HOURLY_USD / 3600


class ModalRuntime:
    def __init__(self, model: KeepModel):
        # Select this client's profile only. Never activate a profile or edit Modal's file.
        os.environ['MODAL_PROFILE'] = model.modal_profile
        os.environ['MODAL_ENVIRONMENT'] = 'main'
        import modal

        self.model = model
        self.app = modal.App(model.app, include_source=False)
        volume = modal.Volume.from_name(model.volume, create_if_missing=False)
        self.volume = volume
        # The same pinned inference stack used to train and grade this adapter.
        root = f'/vol/{model.volume}'
        image = (modal.Image.debian_slim(python_version='3.12').apt_install('git')
                 .pip_install('torch==2.14.0', 'transformers==5.17.0', 'flash-linear-attention==0.5.2',
                              'numpy==1.26.4',
                              'decider-ai @ git+https://github.com/Mapika/decider.git@a5120cce45b9ff70964fac54ea6e8c1ac5b08c7f')
                 .pip_install('peft==0.18.1')
                 .env({'HF_HOME': root + '/public-cache', 'HF_HUB_DISABLE_TELEMETRY': '1',
                       'HF_HUB_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false', 'PYTHONPATH': '/root',
                       'TRITON_CACHE_DIR': root + '/triton-cache', 'TORCHINDUCTOR_CACHE_DIR': root + '/torch-cache'})
                 .add_local_file(Path(__file__).with_name('inference.py'), '/root/venator/score/inference.py'))
        options = dict(image=image, cpu=(2, 2), volumes={'/vol': volume},
                       startup_timeout=STARTUP_TIMEOUT, max_containers=1,
                       scaledown_window=SCALEDOWN)
        # Importable source functions, not serialized Python bytecode: the local
        # interpreter and pinned inference image need not share a Python version.
        self.prepare_function = self.app.function(
            memory=(8192, 8192), timeout=CPU_TIMEOUT, retries=0, **options,
        )(inference.prepare)
        self.infer_function = self.app.function(
            gpu='H100', memory=(16384, 16384), timeout=GPU_TIMEOUT,
            is_generator=True, **options,
        )(inference.score)

    def prepare(self, rows: list[dict], directory: str) -> dict:
        with self.app.run():
            receipt = self.prepare_function.remote(rows, self.model.inference_metadata(), directory, self.volume)
            return {**receipt, 'prepare_app_id': self.app.app_id}

    def infer(self, directory: str, items_sha256: str):
        with self.app.run():
            yield {'app_id': self.app.app_id}
            yield from self.infer_function.remote_gen(self.model.inference_metadata(), directory, items_sha256)
