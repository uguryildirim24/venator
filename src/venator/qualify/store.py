"""Append-only qualification rows and Profile-scoped promotion events."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from venator.profile.claim import claim_store, report_stray_rows, rows_from_other_profiles, store_owner
from venator.qualify.versions import (
    event_id,
    jev_accepted_profile_hash,
    promotion_state_revision,
    sha256_json,
)

EVENTS = {"finalized", "signed", "activated", "revoked", "inherited"}
CHECKPOINT_KIND = "checkpoint"
JEV_KIND = "typesafe_jev"
KNOWN_KINDS = {CHECKPOINT_KIND, JEV_KIND}
JEV_SCHEMA = "jev-assessment-2"
JEV_SUCCESS = {"prioritize", "review", "exclude"}
JEV_DECISIONS = JEV_SUCCESS | {"unassessed"}
JEV_UNASSESSED_REASONS = {
    "snippet", "missing", "too_long", "no_profile", "policy_missing",
    "credentials_missing", "transport_failed", "response_invalid",
    "model_mismatch", "budget_exhausted",
}
JEV_ROW_FIELDS = (
    "schema", "qualifier_kind", "posting_key", "profile_id",
    "posting_revision", "profile_hash", "policy_hash", "accepted_profile_hash",
    "as_of_month", "input_version", "qualifier_version", "qualifier_identity",
    "assessment_key", "request_key", "state_sha256", "questions_sha256",
    "mode", "decision", "fit_probability", "fit_score",
    "exclusions", "primary_rule", "review_flags", "diagnostic_flags",
    "response_validation", "response_sha256", "response_ref",
    "model_returned", "usage", "latency_ms", "decided_at",
    "cache_hit", "reason",
)
_RECEIPT_FIELDS = frozenset({"decided_at", "cache_hit"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
QUALIFICATION_PART_MAX_BYTES = 90_000_000


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON in {path}:{line_number}") from error
            if not isinstance(row, dict):
                raise ValueError(f"expected object in {path}:{line_number}")
            rows.append(row)
    return rows


def qualification_rows(directory: Path) -> list[dict[str, Any]]:
    rows = [row for path in sorted(directory.glob("????-??-??*.jsonl")) for row in _read(path)]
    # An explicit --as-of can append an old day's file after a newer day's file.
    # The operational decided_at clock, not the filename, orders such appends.
    rows.sort(key=lambda row: str(row.get("decided_at") or ""))
    return rows


def promotion_events(directory: Path) -> list[dict[str, Any]]:
    return _read(directory / "promotions.jsonl")


def verify_store(directory: Path, profile_id: str) -> None:
    owner = store_owner(directory)
    if owner is not None and owner != profile_id:
        raise ValueError(f"{directory} belongs to Profile {owner!r}, not {profile_id!r}")
    rows = [*qualification_rows(directory), *promotion_events(directory)]
    report_stray_rows(directory, profile_id, rows_from_other_profiles(rows, profile_id),
                      owner=owner, flag="--qualifications-dir")


def _append(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    values = list(rows)
    if not values:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as target:
        for row in values:
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return len(values)


def _append_daily(directory: Path, day: str, rows: Iterable[Mapping[str, Any]]) -> int:
    values = list(rows)
    if not values:
        return 0
    directory.mkdir(parents=True, exist_ok=True)
    base = directory / f"{day}.jsonl"
    parts = ([base] if base.exists() else []) + sorted(directory.glob(f"{day}_????.jsonl"))
    output = parts[-1] if parts else base
    size = output.stat().st_size if output.exists() else 0
    target = None
    try:
        for row in values:
            encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            if len(encoded) > QUALIFICATION_PART_MAX_BYTES:
                raise ValueError("qualification row exceeds the per-file store limit")
            if size and size + len(encoded) > QUALIFICATION_PART_MAX_BYTES:
                if target is not None:
                    target.close()
                    target = None
                if not parts or parts[-1] != output:
                    parts.append(output)
                match = re.fullmatch(rf"{re.escape(day)}_(\d{{4}})\.jsonl", output.name)
                number = int(match.group(1)) + 1 if match else 2
                output = directory / f"{day}_{number:04d}.jsonl"
                size = output.stat().st_size if output.exists() else 0
            if target is None:
                target = output.open("ab")
            target.write(encoded)
            size += len(encoded)
    finally:
        if target is not None:
            target.close()
    return len(values)


def qualifier_kind_of(row: Mapping[str, Any]) -> str:
    """Declared kind, or checkpoint when the field is absent. Never inferred."""
    kind = row.get("qualifier_kind")
    if kind is None or kind == "":
        return CHECKPOINT_KIND
    if not isinstance(kind, str) or kind not in KNOWN_KINDS:
        raise ValueError("unknown qualifier kind")
    return kind


def _marker(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (str(row.get("posting_key")), str(row.get("mode")),
            str(row.get("qualifier_version")), str(row.get("input_version")))


def _canonical(row: Mapping[str, Any]) -> str:
    payload = {key: row[key] for key in row if key not in _RECEIPT_FIELDS}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_jev_success(row: Mapping[str, Any]) -> bool:
    return row.get("decision") in JEV_SUCCESS


def _finite_unit(value: object) -> bool:
    return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(float(value))


def _assessment_key(row: Mapping[str, Any]) -> str:
    return sha256_json({
        "input_version": row.get("input_version"),
        "qualifier_version": row.get("qualifier_version"),
        "request_key": row.get("request_key"),
        "response_sha256": row.get("response_sha256"),
    })


def _jev_response_ref(request_key: str) -> str:
    if not isinstance(request_key, str) or _HEX64.fullmatch(request_key) is None:
        raise ValueError("invalid Jev request_key")
    return f"jev/responses/{request_key}.json"


def _validate_jev_row(row: Mapping[str, Any], profile_id: str) -> None:
    if set(row) != set(JEV_ROW_FIELDS):
        raise ValueError("Jev qualification row is not the closed schema")
    if row.get("schema") != JEV_SCHEMA or row.get("qualifier_kind") != JEV_KIND:
        raise ValueError("invalid Jev qualification schema")
    if row.get("profile_id") != profile_id or row.get("mode") not in {"shadow", "promoted"}:
        raise ValueError("invalid qualification row Profile or mode")
    if row.get("decision") not in JEV_DECISIONS:
        raise ValueError("invalid qualification decision")
    identity = row.get("qualifier_identity")
    if not isinstance(identity, dict) or identity.get("kind") != JEV_KIND:
        raise ValueError("invalid Jev qualifier_identity")
    accepted = jev_accepted_profile_hash(str(row.get("profile_hash")), str(row.get("policy_hash")))
    if row.get("accepted_profile_hash") != accepted:
        raise ValueError("Jev accepted_profile_hash does not match profile and policy hashes")
    if row.get("assessment_key") != _assessment_key(row):
        raise ValueError("Jev assessment_key does not match the binding")
    if not isinstance(row.get("cache_hit"), bool):
        raise ValueError("invalid Jev cache_hit")
    if not isinstance(row.get("decided_at"), str) or not row["decided_at"]:
        raise ValueError("invalid Jev decided_at")
    if not isinstance(row.get("review_flags"), list) or not isinstance(row.get("diagnostic_flags"), list):
        raise ValueError("invalid Jev flags")
    if not isinstance(row.get("exclusions"), list):
        raise ValueError("invalid Jev exclusions")
    if row["decision"] == "unassessed":
        if row.get("reason") not in JEV_UNASSESSED_REASONS:
            raise ValueError("invalid unassessed reason")
        for name in ("fit_probability", "fit_score", "primary_rule", "response_validation",
                     "response_sha256", "response_ref", "model_returned", "usage", "latency_ms"):
            if row.get(name) is not None:
                raise ValueError("unassessed Jev row must null unavailable response fields")
        if row["exclusions"]:
            raise ValueError("unassessed Jev row must have empty exclusions")
        return
    if row.get("reason") is not None:
        raise ValueError("successful Jev row must not carry an unassessed reason")
    if not _finite_unit(row.get("fit_probability")) or not _finite_unit(row.get("fit_score")):
        raise ValueError("invalid Jev scores")
    if not all(0.0 <= float(row[name]) <= 1.0 for name in ("fit_probability", "fit_score")):
        raise ValueError("Jev scores must lie in [0, 1]")
    validation = row.get("response_validation")
    if (not isinstance(validation, dict) or validation.get("passed") is not True
            or not isinstance(validation.get("version"), str)
            or validation.get("error_codes") not in ([], ())):
        raise ValueError("invalid Jev response_validation")
    request_key = row.get("request_key")
    if row.get("response_ref") != _jev_response_ref(str(request_key)):
        raise ValueError("invalid Jev response_ref")
    if not isinstance(row.get("response_sha256"), str) or _HEX64.fullmatch(str(row["response_sha256"])) is None:
        raise ValueError("invalid Jev response_sha256")
    if row.get("model_returned") != "jev-1.13.0":
        raise ValueError("invalid Jev model_returned")
    usage = row.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("invalid Jev usage")
    for name in ("input_tokens", "output_tokens"):
        value = usage.get(name)
        if type(value) is not int or value < 0:
            raise ValueError("invalid Jev usage")
    latency = row.get("latency_ms")
    if not _finite_unit(latency) or float(latency) < 0:
        raise ValueError("invalid Jev latency_ms")


def _jev_existing_action(incoming: Mapping[str, Any],
                         existing: Iterable[Mapping[str, Any]]) -> str:
    """Return write, skip, or raise on a conflicting success."""
    marker = _marker(incoming)
    successes = [row for row in existing
                 if qualifier_kind_of(row) == JEV_KIND and _marker(row) == marker and _is_jev_success(row)]
    if _is_jev_success(incoming):
        if not successes:
            return "write"
        if any(_canonical(row) == _canonical(incoming) for row in successes):
            return "skip"
        raise ValueError("conflicting Jev success at an existing marker")
    if successes:
        return "skip"
    return "write"


def append_rows(directory: Path, profile_id: str, rows: Iterable[Mapping[str, Any]], *, day: str) -> int:
    values = [dict(row) for row in rows]
    if not values:
        return 0
    verify_store(directory, profile_id)
    state = fold_events(promotion_events(directory), profile_id)
    prior = qualification_rows(directory)
    prior_successes: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in prior:
        if qualifier_kind_of(row) == JEV_KIND and _is_jev_success(row):
            prior_successes.setdefault(_marker(row), []).append(row)
    markers: set[tuple[str, str, str, str]] = set()
    to_write: list[dict[str, Any]] = []
    for row in values:
        kind = qualifier_kind_of(row)
        if kind != JEV_KIND:
            raise ValueError("new qualification rows must be Jev")
        _validate_jev_row(row, profile_id)
        marker = _marker(row)
        if marker in markers:
            raise ValueError("duplicate qualification row in one append")
        markers.add(marker)
        if row["mode"] == "promoted":
            active = state.current(str(row.get("accepted_profile_hash")), qualifier_kind=JEV_KIND)
            if active is None or active.qualifier_version != row.get("qualifier_version"):
                raise ValueError("promoted row requires an active, non-stale activation")
        if kind == JEV_KIND:
            action = _jev_existing_action(row, prior_successes.get(marker, ()))
            if action == "skip":
                continue
        to_write.append(row)
    if not to_write:
        return 0
    if store_owner(directory) is None:
        claim_store(directory, profile_id)
    return _append_daily(directory, day, to_write)


@dataclass(frozen=True)
class Activation:
    event_id: str
    qualifier_version: str
    accepted_hash: str
    qualifier_kind: str


@dataclass(frozen=True)
class PromotionState:
    active: Activation | None
    revision: str

    def current(self, profile_hash: str, *, qualifier_kind: str) -> Activation | None:
        if self.active is None or self.active.accepted_hash != profile_hash:
            return None
        if self.active.qualifier_kind != qualifier_kind:
            return None
        return self.active


def _event_kind(row: Mapping[str, Any]) -> str:
    return qualifier_kind_of(row)


def fold_events(events: Iterable[Mapping[str, Any]], profile_id: str) -> PromotionState:
    relevant = [row for row in events if row.get("profile_id") == profile_id]
    seen: dict[str, Mapping[str, Any]] = {}
    active: Activation | None = None
    for row in relevant:
        identifier = row.get("event_id")
        payload = {key: value for key, value in row.items() if key != "event_id"}
        if not isinstance(identifier, str) or identifier != event_id(payload) or identifier in seen:
            raise ValueError("invalid or duplicate promotion event identity")
        kind = row.get("event")
        if kind not in EVENTS:
            raise ValueError("invalid promotion event")
        event_kind = _event_kind(row)
        if kind == "inherited" and event_kind == JEV_KIND:
            raise ValueError("Jev inherited is unsupported")
        reference = row.get("refers_to")
        parent = seen.get(reference) if isinstance(reference, str) else None
        if kind == "finalized" and reference is not None:
            raise ValueError("finalization may not refer to another event")
        if kind in {"signed", "activated"}:
            needed = "finalized" if kind == "signed" else "signed"
            if parent is None or parent.get("event") != needed or parent.get("qualifier_version") != row.get("qualifier_version"):
                raise ValueError(f"{kind} requires a matching {needed} event")
        if kind in {"revoked", "inherited"} and (parent is None or parent.get("event") != "activated"):
            raise ValueError(f"{kind} requires an activated event")
        if kind == "activated":
            active = Activation(identifier, str(row["qualifier_version"]), str(row["profile_hash"]),
                                event_kind)
        elif kind == "revoked" and active is not None and active.event_id == reference:
            active = None
        elif kind == "inherited" and active is not None and active.event_id == reference:
            active = Activation(active.event_id, active.qualifier_version, str(row["profile_hash"]),
                                active.qualifier_kind)
        seen[identifier] = row
    return PromotionState(active, promotion_state_revision(relevant))


def _validate_jev_event(row: Mapping[str, Any]) -> None:
    if _event_kind(row) != JEV_KIND:
        return
    if row.get("event") == "inherited":
        raise ValueError("Jev inherited is unsupported")
    compiled = row.get("compiled_profile_hash")
    policy = row.get("policy_hash")
    if not isinstance(compiled, str) or not isinstance(policy, str):
        raise ValueError("Jev promotion event requires compiled_profile_hash and policy_hash")
    expected = jev_accepted_profile_hash(compiled, policy)
    if row.get("profile_hash") != expected:
        raise ValueError("Jev promotion event profile_hash must be the accepted binding")


def append_event(directory: Path, profile_id: str, row: Mapping[str, Any]) -> str:
    verify_store(directory, profile_id)
    if row.get("profile_id") != profile_id:
        raise ValueError("promotion event belongs to another Profile")
    if _event_kind(row) != JEV_KIND:
        raise ValueError("new promotion events must be Jev")
    _validate_jev_event(row)
    payload = {key: value for key, value in row.items() if key != "event_id"}
    identifier = event_id(payload)
    if row.get("event_id", identifier) != identifier:
        raise ValueError("promotion event identity disagrees with payload")
    complete = {**payload, "event_id": identifier}
    fold_events([*promotion_events(directory), complete], profile_id)
    if store_owner(directory) is None:
        claim_store(directory, profile_id)
    _append(directory / "promotions.jsonl", [complete])
    return identifier


def selected_shadow(rows: Iterable[Mapping[str, Any]], profile_id: str,
                    *, qualifier_kind: str) -> str | None:
    selected = None
    for row in rows:
        if (row.get("profile_id") == profile_id and row.get("mode") == "shadow"
                and qualifier_kind_of(row) == qualifier_kind):
            selected = str(row["qualifier_version"])
    return selected


def effective_rows(rows: Iterable[Mapping[str, Any]], *, profile_id: str, mode: str,
                   qualifier_version: str | None, input_versions: Mapping[str, str],
                   qualifier_kind: str) -> dict[str, Mapping[str, Any]]:
    effective: dict[str, Mapping[str, Any]] = {}
    successes: set[str] = set()
    if qualifier_version is None:
        return effective
    for row in rows:
        key = row.get("posting_key")
        if not (row.get("profile_id") == profile_id and row.get("mode") == mode
                and row.get("qualifier_version") == qualifier_version
                and isinstance(key, str) and row.get("input_version") == input_versions.get(key)
                and qualifier_kind_of(row) == qualifier_kind):
            continue
        if qualifier_kind == JEV_KIND:
            if _is_jev_success(row):
                effective[key] = row
                successes.add(key)
            elif key not in successes:
                effective[key] = row
        else:
            effective[key] = row
    return effective


def as_of_month(value: str) -> str:
    """Validate a date at an entry point, then send month precision below it."""
    from datetime import date

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("--as-of must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("--as-of must be a valid YYYY-MM-DD date") from error
    return value[:7]
