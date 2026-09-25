"""Immutable successful-response cache under a claimed qualification root.

Path: ``jev/responses/<request_key>.json``. No request body, credentials or
response headers are stored. Publication is create-only.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from venator.llm.system_one import MODEL
from venator.qualify.versions import jev_response_sha256

CACHE_SCHEMA = "jev-response-2"
_KEY = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = (
    "schema",
    "request_key",
    "state_sha256",
    "questions_sha256",
    "model_requested",
    "raw_response",
    "response_sha256",
    "usage",
    "latency_ms",
    "received_at",
)
IDENTITY_FIELDS = (
    "schema",
    "request_key",
    "state_sha256",
    "questions_sha256",
    "model_requested",
    "raw_response",
    "response_sha256",
)


class CacheIdentityError(ValueError):
    """A different successful body already occupies this request key."""


class CacheCorruptError(ValueError):
    """An existing cache file is unreadable or not the closed wrapper."""


class CacheEscapeError(ValueError):
    """response_ref would leave the qualification root or follow a symlink."""


def response_ref(request_key: str) -> str:
    if not isinstance(request_key, str) or _KEY.fullmatch(request_key) is None:
        raise ValueError("invalid request key")
    return f"jev/responses/{request_key}.json"


def response_path(root: Path, request_key: str) -> Path:
    relative = Path(response_ref(request_key))
    return _contained(root, root / relative)


def _contained(root: Path, path: Path) -> Path:
    root_resolved = root.resolve()
    parent = path.parent
    if parent.exists():
        resolved_parent = parent.resolve()
        if not resolved_parent.is_relative_to(root_resolved):
            raise CacheEscapeError("response_ref escapes the qualification root")
        if parent.is_symlink():
            raise CacheEscapeError("response cache refuses a symbolic link")
    if path.exists() and path.is_symlink():
        raise CacheEscapeError("response cache refuses a symbolic link")
    return path


def _identity(wrapper: Mapping[str, Any]) -> dict[str, Any]:
    return {field: wrapper.get(field) for field in IDENTITY_FIELDS}


def _valid_wrapper(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != set(_FIELDS):
        return False
    if value.get("schema") != CACHE_SCHEMA:
        return False
    if value.get("model_requested") != MODEL:
        return False
    raw = value.get("raw_response")
    digest = value.get("response_sha256")
    request_key = value.get("request_key")
    if not isinstance(raw, str) or not isinstance(digest, str):
        return False
    if jev_response_sha256(raw) != digest:
        return False
    try:
        if response_ref(str(request_key)).split("/")[-1] != f"{request_key}.json":
            return False
    except ValueError:
        return False
    usage = value.get("usage")
    if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}:
        return False
    if type(usage.get("input_tokens")) is not int or type(usage.get("output_tokens")) is not int:
        return False
    if usage["input_tokens"] < 0 or usage["output_tokens"] < 0:
        return False
    latency = value.get("latency_ms")
    if type(latency) not in (int, float) or isinstance(latency, bool) or latency < 0:
        return False
    if not isinstance(value.get("received_at"), str) or not value["received_at"]:
        return False
    for name in ("state_sha256", "questions_sha256"):
        item = value.get(name)
        if not isinstance(item, str) or _KEY.fullmatch(item) is None:
            return False
    return True


def _atomic_create(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_response(root: Path, request_key: str) -> dict[str, Any] | None:
    path = response_path(root, request_key)
    if not path.exists():
        return None
    if path.is_symlink():
        raise CacheEscapeError("response cache refuses a symbolic link")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CacheCorruptError("corrupt Jev response cache") from None
    if not _valid_wrapper(loaded):
        raise CacheCorruptError("corrupt Jev response cache")
    return loaded


def publish_response(
    root: Path,
    *,
    request_key: str,
    state_sha256: str,
    questions_sha256: str,
    raw_response: str,
    usage: Mapping[str, int],
    latency_ms: float,
    received_at: str,
    model_requested: str = MODEL,
) -> dict[str, Any]:
    """Create-only publication. Matching existing success is reused."""
    if model_requested != MODEL:
        raise ValueError("cache admits only the pinned model")
    wrapper = {
        "schema": CACHE_SCHEMA,
        "request_key": request_key,
        "state_sha256": state_sha256,
        "questions_sha256": questions_sha256,
        "model_requested": MODEL,
        "raw_response": raw_response,
        "response_sha256": jev_response_sha256(raw_response),
        "usage": {
            "input_tokens": int(usage["input_tokens"]),
            "output_tokens": int(usage.get("output_tokens", 0)),
        },
        "latency_ms": float(latency_ms),
        "received_at": received_at,
    }
    if not _valid_wrapper(wrapper):
        raise ValueError("invalid Jev response cache wrapper")
    path = response_path(root, request_key)
    payload = (json.dumps(wrapper, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        _atomic_create(path, payload)
    except FileExistsError:
        existing = read_response(root, request_key)
        if existing is None:
            raise CacheCorruptError("corrupt Jev response cache")
        if _identity(existing) != _identity(wrapper):
            raise CacheIdentityError("different body at the same request key")
        return existing
    return wrapper
