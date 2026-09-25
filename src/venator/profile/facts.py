"""The common inclusion boundary for candidate source facts."""
from collections.abc import Mapping

_FALSE = frozenset({"false", "no", "0", "unconfirmed", "draft"})
_WITHHELD = frozenset({"draft", "unconfirmed", "pending", "suggested", "rejected", "proposed"})


def _false(value: object) -> bool:
    return value is False or value == 0 or isinstance(value, str) and value.strip().casefold() in _FALSE


def confirmed(value: object) -> bool:
    """Legacy facts are usable unless explicitly withheld or marked draft.

    Call this at every traversed mapping; confirming a parent does not confirm
    a draft child. Keep original collection indexes for source references.
    """
    if not isinstance(value, Mapping):
        return True
    if any(key in value and _false(value[key]) for key in ("confirmed", "owner_confirmed", "include")):
        return False
    if "draft" in value and not _false(value["draft"]):
        return False
    status = value.get("status")
    return not (isinstance(status, str) and status.strip().casefold() in _WITHHELD)
