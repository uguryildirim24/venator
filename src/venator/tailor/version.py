"""Lightweight Profile input fingerprint for prepared application freshness."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from venator.profile import Profile

PREPARATION_REVISION = 10


def _plain_copy(value: object) -> object:
    """Copy YAML-shaped data without trying to pickle MappingProxyType."""
    if isinstance(value, Mapping):
        return {str(key): _plain_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_copy(item) for item in value]
    if isinstance(value, set):
        return sorted((_plain_copy(item) for item in value), key=str)
    return value


def _input_version(profile: Profile, resume: Mapping[str, object], *, include_layout: bool = True) -> str:
    # The public freshness stamp must match application open; this additional
    # fingerprint also protects callers using in-memory Profile changes.
    payload = {"resume": _plain_copy(resume), "constraints": _plain_copy(profile.constraints),
               "targeting": _plain_copy(profile.targeting)}
    reference = profile.directory / "resume-reference"
    if include_layout and reference.exists():
        payload["resume_reference"] = {
            path.relative_to(reference).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(reference.rglob("*")) if path.is_file()
        }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
