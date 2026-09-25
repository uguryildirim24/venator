"""Jev response cache: immutable publication, no request body, no symlink escape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from venator.qualify.jev_cache import (
    CACHE_SCHEMA,
    CacheCorruptError,
    CacheEscapeError,
    CacheIdentityError,
    publish_response,
    read_response,
    response_path,
    response_ref,
)
from venator.qualify.versions import jev_response_sha256, sha256_utf8

KEY = "a" * 64
STATE = "b" * 64
QUESTIONS = "c" * 64
RAW = '{"model":"jev-1.13.0","answers":{},"usage":{"input_tokens":3,"output_tokens":0}}'


def _publish(root: Path, raw: str = RAW, **changes: object) -> dict:
    kwargs: dict = {
        "request_key": KEY,
        "state_sha256": STATE,
        "questions_sha256": QUESTIONS,
        "raw_response": raw,
        "usage": {"input_tokens": 3, "output_tokens": 0},
        "latency_ms": 12.5,
        "received_at": "2026-09-18T12:00:00+00:00",
    }
    kwargs.update(changes)
    return publish_response(root, **kwargs)


def test_publish_then_read_round_trip(tmp_path: Path) -> None:
    wrapper = _publish(tmp_path)
    assert wrapper["schema"] == CACHE_SCHEMA
    assert wrapper["response_sha256"] == jev_response_sha256(RAW)
    loaded = read_response(tmp_path, KEY)
    assert loaded == wrapper
    path = response_path(tmp_path, KEY)
    assert path == tmp_path / "jev" / "responses" / f"{KEY}.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "state" not in on_disk
    assert "questions" not in on_disk
    assert "Authorization" not in json.dumps(on_disk)
    assert "headers" not in on_disk
    assert "policy_version" not in on_disk
    assert on_disk["raw_response"] == RAW


def test_matching_publish_is_reuse(tmp_path: Path) -> None:
    first = _publish(tmp_path, received_at="2026-09-18T12:00:00+00:00")
    second = _publish(tmp_path, received_at="2026-09-18T12:00:01+00:00")
    assert second["raw_response"] == first["raw_response"]
    assert second["received_at"] == first["received_at"]
    lines = (tmp_path / "jev" / "responses" / f"{KEY}.json").read_text().count("\n")
    assert lines == 1


def test_different_body_at_same_key_is_identity_error(tmp_path: Path) -> None:
    _publish(tmp_path)
    other = '{"model":"jev-1.13.0","answers":{"x":1},"usage":{"input_tokens":3,"output_tokens":0}}'
    with pytest.raises(CacheIdentityError):
        _publish(tmp_path, raw=other)
    loaded = read_response(tmp_path, KEY)
    assert loaded is not None
    assert loaded["raw_response"] == RAW


def test_corrupt_cache_does_not_authorize_replace(tmp_path: Path) -> None:
    path = tmp_path / "jev" / "responses" / f"{KEY}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CacheCorruptError):
        read_response(tmp_path, KEY)
    with pytest.raises(CacheCorruptError):
        _publish(tmp_path)
    assert path.read_text(encoding="utf-8") == "{not json"


def test_traversal_request_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid request key"):
        response_ref("../" + "a" * 60)
    with pytest.raises(ValueError, match="invalid request key"):
        response_path(tmp_path, "../secret")


def test_symlink_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    directory = tmp_path / "jev" / "responses"
    directory.mkdir(parents=True)
    link = directory / f"{KEY}.json"
    link.symlink_to(target)
    with pytest.raises(CacheEscapeError):
        read_response(tmp_path, KEY)
    with pytest.raises(CacheEscapeError):
        _publish(tmp_path)


def test_missing_key_reads_as_none(tmp_path: Path) -> None:
    assert read_response(tmp_path, KEY) is None


def test_request_key_is_hex_identity(tmp_path: Path) -> None:
    digest = sha256_utf8("fixture-request")
    wrapper = _publish(tmp_path, request_key=digest)
    assert wrapper["request_key"] == digest
    assert response_ref(digest) == f"jev/responses/{digest}.json"
