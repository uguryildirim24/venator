"""Install the suite's no-completion guard for this directory.

The implementation is one module for all three directories that can reach an
LLM runtime; see ``tests/spend_guard.py``, including why it is not a single
``tests/conftest.py`` (the never-submit coverage guard pins that file's
absence, and this arrangement leaves that invariant exactly as strong).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spend_guard import never_spawn_a_billed_runtime  # noqa: E402

__all__ = ["never_spawn_a_billed_runtime"]
