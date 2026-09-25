"""Historical three-way aggregation retained by the Jev development baseline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Root:
    modality: str
    status: str
    node: object
    findings: Mapping[str, Mapping[str, Any]]


def decide(roots_list: list[Root]) -> str:
    """Aggregate historical required/unknown roots without running a judge."""
    required = [root for root in roots_list if root.modality == "required"]
    unknown = [root for root in roots_list if root.modality == "unknown"]
    if any(root.status == "not_met" for root in required):
        return "not_qualified"
    if (not roots_list
            or any(root.status == "undecidable" for root in required)
            or any(root.status != "met" for root in unknown)):
        return "cannot_tell"
    return "qualified"
