"""One Install-wide pipeline lock, shared with the dashboard's write actions."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextmanager
def pipeline_lock(install: Path, *, wait: bool = True) -> Iterator[None]:
    path = install / 'build/.pipeline.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    token = {'pid': os.getpid(), 'at': time.time_ns()}
    while True:
        try:
            with path.open('x', encoding='utf-8', newline='\n') as lock:
                json.dump(token, lock)
            break
        except FileExistsError:
            try:
                current = json.loads(path.read_text(encoding='utf-8'))
                pid = int(current['pid'])
                if pid > 0 and not alive(pid):
                    path.unlink(missing_ok=True)
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Another process may still be writing its newly created lock.
            if not wait:
                raise BlockingIOError('Wait for the current operation to finish.')
            time.sleep(0.25)
    try:
        yield
    finally:
        try:
            if json.loads(path.read_text(encoding='utf-8')) == token:
                path.unlink()
        except (OSError, ValueError):
            pass
