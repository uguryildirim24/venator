"""Pure qualification identity construction with source-bound versions."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from venator.qualify.compile import COMPILER_VERSION
from venator.qualify.engine import ENGINE_VERSION
from venator.qualify.posting import TEXT_VERSION
from venator.qualify.prompt import SYSTEM_PROMPT


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {field.name: _jsonable(getattr(obj, field.name)) for field in fields(obj)}
    if isinstance(obj, Mapping):
        return {str(key): _jsonable(value) for key, value in obj.items()}
    if isinstance(obj, (tuple, list)):
        return [_jsonable(value) for value in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(_jsonable(value) for value in obj)
    return obj


def canon(obj: Any) -> str:
    return json.dumps(_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def bytes_version(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:12]


def schema_version(profile_schema_bytes: bytes, output_schema_bytes: bytes) -> str:
    return bytes_version(profile_schema_bytes + output_schema_bytes)


def deny_version(deny_terms_py_bytes: bytes) -> str:
    return bytes_version(deny_terms_py_bytes)


_DIR = Path(__file__).resolve().parent
PROMPT_VERSION = h(SYSTEM_PROMPT)
SCHEMA_VERSION = schema_version((_DIR / "schema.py").read_bytes(),
                                (_DIR / "schemas/qualify-output-2.json").read_bytes())
DENY_VERSION = deny_version((_DIR / "deny_terms.py").read_bytes())


def contract_version(*, compiler_version: str = COMPILER_VERSION,
                     engine_version: str = ENGINE_VERSION, text_version: str = TEXT_VERSION,
                     prompt_version: str = PROMPT_VERSION, schema_version_value: str = SCHEMA_VERSION,
                     deny_version_value: str = DENY_VERSION) -> str:
    return h("|".join([compiler_version, engine_version, text_version,
                       prompt_version, schema_version_value, deny_version_value]))


def profile_hash(compiled_profile: Any) -> str:
    value = _jsonable(compiled_profile)
    value["profile_hash"] = ""
    return h(canon(value))


def input_version(posting_key: str, posting_revision: str, profile_hash_value: str,
                  as_of_month: str, *, contract_version_value: str | None = None) -> str:
    return h("|".join([contract_version_value or contract_version(), posting_key,
                       posting_revision, profile_hash_value, as_of_month]))


def qualifier_version(checkpoint_sha256: str, artifact_sha256: str, tokenizer_sha256: str,
                      chat_template_sha256: str, runner_id: str, runner_version: str,
                      precision: str = "bf16", decoding: Mapping[str, Any] | None = None,
                      *, contract_version_value: str | None = None) -> str:
    return h(canon({"contract_version": contract_version_value or contract_version(),
                    "checkpoint_sha256": checkpoint_sha256, "artifact_sha256": artifact_sha256,
                    "tokenizer_sha256": tokenizer_sha256, "chat_template_sha256": chat_template_sha256,
                    "runner_id": runner_id, "runner_version": runner_version, "precision": precision,
                    "decoding": decoding if decoding is not None else
                    {"greedy": True, "max_new_tokens": 3072, "seed": 1729}}))


def event_id(promotion_event_row_without_event_id: Mapping[str, Any]) -> str:
    return h(canon(promotion_event_row_without_event_id))


def promotion_state_revision(events: Iterable[Mapping[str, Any]]) -> str:
    return h("|".join(str(event["event_id"]) for event in events
                      if event.get("event") in {"activated", "revoked", "inherited"}))


def _reject_nonfinite(obj: Any) -> Any:
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ValueError("non-finite values are forbidden in Jev hashes")
    if isinstance(obj, dict):
        return {key: _reject_nonfinite(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_reject_nonfinite(value) for value in obj]
    return obj


def sha256_utf8(text: str) -> str:
    """Full SHA-256, lower-case hex, over UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(obj: Any) -> str:
    """Full SHA-256 over compact sorted-key UTF-8 JSON. Non-finite values are refused."""
    dumped = json.dumps(_reject_nonfinite(_jsonable(obj)), sort_keys=True,
                        separators=(",", ":"), ensure_ascii=False)
    return sha256_utf8(dumped)


def jev_policy_hash(policy: Any) -> str:
    return sha256_json(policy)


def jev_accepted_profile_hash(profile_hash_value: str, policy_hash_value: str) -> str:
    return sha256_json({"profile_hash": profile_hash_value, "policy_hash": policy_hash_value})


def jev_input_version(*, posting_key: str, posting_revision: str, profile_hash_value: str,
                      policy_hash_value: str, as_of_month: str, canonicalizer_hash: str,
                      compiler_hash: str, evidence_hash: str, projection_hash: str,
                      deny_hash: str, state_renderer_version: str) -> str:
    return sha256_json({
        "schema": "jev-input-2",
        "posting_key": posting_key,
        "posting_revision": posting_revision,
        "profile_hash": profile_hash_value,
        "policy_hash": policy_hash_value,
        "as_of_month": as_of_month,
        "canonicalizer_hash": canonicalizer_hash,
        "compiler_hash": compiler_hash,
        "evidence_hash": evidence_hash,
        "projection_hash": projection_hash,
        "deny_hash": deny_hash,
        "state_renderer_version": state_renderer_version,
    })


def jev_qualifier_identity(*, model_requested: str, model_expected: str,
                           question_package: str, questions_sha256: str,
                           release_hash: str) -> dict[str, str]:
    return {
        "kind": "typesafe_jev",
        "provider": "typesafe",
        "api_contract": "systemone-v1",
        "model_requested": model_requested,
        "model_expected": model_expected,
        "question_package": question_package,
        "questions_sha256": questions_sha256,
        "release_hash": release_hash,
    }


def jev_qualifier_version(identity: Mapping[str, str]) -> str:
    return "jev:" + sha256_json(dict(identity))


def jev_state_sha256(state: Any) -> str:
    return sha256_json(state)


def jev_request_key(*, model_requested: str, state_sha256: str, questions_sha256: str,
                    api_contract: str = "systemone-v1") -> str:
    return sha256_json({
        "api_contract": api_contract,
        "model_requested": model_requested,
        "state_sha256": state_sha256,
        "questions_sha256": questions_sha256,
    })


def jev_response_sha256(raw_body: str) -> str:
    return sha256_utf8(raw_body)


def jev_assessment_key(*, input_version_value: str, qualifier_version_value: str,
                       request_key: str, response_sha256: str | None) -> str:
    return sha256_json({
        "input_version": input_version_value,
        "qualifier_version": qualifier_version_value,
        "request_key": request_key,
        "response_sha256": response_sha256,
    })


def jev_release_hash(release: Any) -> str:
    return sha256_json(release)
