"""Offline city identities shared with the dashboard's location picker.

place_names.json derives from GeoNames cities500 and admin2Codes
(see docs/geonames-attribution.md).
A name may have several readings; never pick an outside reading over a New England one.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _names() -> dict[str, list[str]]:
    return json.loads(Path(__file__).with_name("place_names.json").read_text(encoding="utf-8"))


def readings(name: str) -> tuple[str, ...]:
    return tuple(_names().get(name.strip().casefold(), ()))
