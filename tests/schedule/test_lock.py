from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.schedule.lock import pipeline_lock


def test_shared_lock_busy_and_dead_pid_takeover(tmp_path: Path) -> None:
    path = tmp_path / 'build/.pipeline.lock'
    with pipeline_lock(tmp_path):
        with pytest.raises(BlockingIOError):
            with pipeline_lock(tmp_path, wait=False):
                pass
    path.write_text(json.dumps({'pid': 99999999, 'at': 1}), encoding='utf-8', newline='\n')
    with pipeline_lock(tmp_path, wait=False):
        assert json.loads(path.read_text(encoding='utf-8'))['pid'] != 99999999
    assert not path.exists()
