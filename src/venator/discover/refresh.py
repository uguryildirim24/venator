"""Bounded exact-posting refresh API.

The implementation lives with the source adapters so it shares their response
normalizers.  This module is the stable import surface for application stages
that need to verify one Posting immediately before preparation or handoff.
"""

from __future__ import annotations

from venator.discover.adapters import refresh_posting

__all__ = ["refresh_posting"]
