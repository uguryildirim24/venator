"""Frozen Jev interface types and the pure response validator.

This module imports no Profile compiler, evidence engine, HTTP client or
``train/`` tree. Types use standard-library scalars, tuples and dataclasses
so a matching worker can pickle a ``JevContext``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from venator.qualify.jev_questions import (
    ANSWER_IDS,
    LEVEL_CRITERIA,
    NOUL_IDS,
    REQUIREMENT_KIND_OPTIONS,
    YEARS_OPTIONS,
)
from venator.qualify.versions import jev_response_sha256

VALIDATOR_VERSION = "4"
JEV_MODEL = "jev-1.13.0"
PROBABILITY_TOLERANCE = 1e-6
# Decision 15: Score identity is a note, never a rejection.
SCORE_IDENTITY_TOLERANCE = 1e-6
# Decision 16: |Σp − 1| above this bound refuses the response; inside it, rescale.
SUM_BOUND = 0.05
DIAGNOSTIC_CODES = frozenset({"score_mismatch", "sum_mismatch"})
TriageDecision = Literal["prioritize", "review", "exclude", "unassessed"]
UNASSESSED_REASONS: tuple[str, ...] = (
    "snippet",
    "missing",
    "too_long",
    "no_profile",
    "policy_missing",
    "credentials_missing",
    "transport_failed",
    "response_invalid",
    "model_mismatch",
    "budget_exhausted",
)

_TOP_FIELDS = frozenset({"model", "answers", "usage"})
_NOUL_FIELDS = frozenset({"type", "noul"})
_CHOICE_FIELDS = frozenset({"type", "choice", "probabilities", "confidence"})
_SCORE_FIELDS = frozenset({"type", "score", "legend", "probabilities", "confidence"})
_USAGE_FIELDS = frozenset({"input_tokens", "output_tokens"})


@dataclass(frozen=True)
class JevExclusion:
    rule: str
    basis: Literal["model_assisted_policy"]
    question_ids: tuple[str, ...]
    probabilities: tuple[tuple[str, float], ...]
    guard_code: str
    policy_value: str | None
    policy_exclusion: bool


@dataclass(frozen=True)
class JevContext:
    posting_key: str
    posting_revision: str
    as_of_month: str
    input_version: str
    qualifier_version: str
    accepted_profile_hash: str
    assessment_key: str
    response_sha256: str
    decision: Literal["prioritize", "review", "exclude"]
    fit_probability: float
    fit_score: float
    exclusions: tuple[JevExclusion, ...]
    primary_rule: str | None
    review_flags: tuple[str, ...]
    diagnostic_flags: tuple[str, ...]


@dataclass(frozen=True)
class JevResolution:
    context: JevContext | None
    input_version: str | None
    fallback_reason: str | None


@dataclass(frozen=True)
class ValidatedScore:
    score: float
    probabilities: tuple[float, ...]
    confidence: float
    legend: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedChoice:
    choice: str
    probabilities: tuple[tuple[str, float], ...]
    confidence: float


@dataclass(frozen=True)
class ValidatedJevResponse:
    model: str
    nouls: tuple[tuple[str, float], ...]
    level: ValidatedScore
    mandatory_requirement_kind: ValidatedChoice
    years_floor: ValidatedChoice
    usage_input_tokens: int
    usage_output_tokens: int
    raw: str
    response_sha256: str

    def noul(self, question_id: str) -> float:
        for name, value in self.nouls:
            if name == question_id:
                return value
        raise KeyError(question_id)


@dataclass(frozen=True)
class JevValidation:
    passed: bool
    version: str
    error_codes: tuple[str, ...]
    response: ValidatedJevResponse | None


@dataclass(frozen=True)
class JevResult:
    decision: TriageDecision
    fit_probability: float | None
    fit_score: float | None
    exclusions: tuple[JevExclusion, ...]
    primary_rule: str | None
    review_flags: tuple[str, ...]
    diagnostic_flags: tuple[str, ...]
    degree_timing_review: bool
    experience_timing_review: bool


class _DuplicateKey(ValueError):
    """JSON object repeated a key."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen[key] = value
    return seen


def _fail(*codes: str) -> JevValidation:
    return JevValidation(False, VALIDATOR_VERSION, tuple(dict.fromkeys(codes)), None)


def _diagnostic_notes(codes: list[str]) -> tuple[str, ...]:
    return tuple(code for code in dict.fromkeys(codes) if code in DIAGNOSTIC_CODES)


def _rejection_codes(codes: list[str]) -> tuple[str, ...]:
    return tuple(code for code in dict.fromkeys(codes) if code not in DIAGNOSTIC_CODES)


def _note(codes: list[str], code: str) -> None:
    if code not in codes:
        codes.append(code)


def _is_number(value: object) -> bool:
    return type(value) in (int, float) and not isinstance(value, bool)


def _finite_unit(value: object, codes: list[str]) -> float | None:
    if isinstance(value, bool):
        _note(codes, "boolean_as_number")
        return None
    if not _is_number(value):
        _note(codes, "wrong_type")
        return None
    number = float(value)
    if not math.isfinite(number):
        _note(codes, "non_finite")
        return None
    if number < 0.0 or number > 1.0:
        _note(codes, "out_of_range")
        return None
    return number


def _nonneg_int(value: object) -> int | None:
    if type(value) is not int or isinstance(value, bool) or value < 0:
        return None
    return value


def _shape(actual: Mapping[str, object], allowed: frozenset[str], codes: list[str]) -> None:
    if set(actual) - allowed:
        _note(codes, "unknown_field")
    if allowed - set(actual):
        _note(codes, "missing_field")


def _scaled_distribution(values: tuple[float, ...], codes: list[str]) -> tuple[float, ...] | None:
    """Refuse a distribution beyond SUM_BOUND; otherwise rescale to sum one."""
    total = sum(values)
    drift = abs(total - 1.0)
    if drift > SUM_BOUND:
        _note(codes, "sum_out_of_bound")
        return None
    if drift <= PROBABILITY_TOLERANCE:
        return values
    _note(codes, "sum_mismatch")
    return tuple(item / total for item in values)


def _check_noul(answer: object, codes: list[str]) -> float | None:
    if not isinstance(answer, dict):
        _note(codes, "wrong_type")
        return None
    if answer.get("type") != "noul":
        _note(codes, "wrong_type")
    if "confidence" in answer:
        _note(codes, "noul_confidence_forbidden")
    _shape(answer, _NOUL_FIELDS, codes)
    return _finite_unit(answer.get("noul"), codes)


def _check_score(answer: object, codes: list[str]) -> ValidatedScore | None:
    if not isinstance(answer, dict):
        _note(codes, "wrong_type")
        return None
    if answer.get("type") != "score":
        _note(codes, "wrong_type")
    _shape(answer, _SCORE_FIELDS, codes)
    score_raw = answer.get("score")
    if isinstance(score_raw, bool):
        _note(codes, "boolean_as_number")
        score_number = None
    elif not _is_number(score_raw):
        _note(codes, "wrong_type")
        score_number = None
    elif not math.isfinite(float(score_raw)):
        _note(codes, "non_finite")
        score_number = None
    else:
        score_number = float(score_raw)
    confidence = _finite_unit(answer.get("confidence"), codes)
    legend = answer.get("legend")
    expected_legend = {str(index): text for index, text in enumerate(LEVEL_CRITERIA)}
    if not isinstance(legend, dict) or dict(legend) != expected_legend:
        _note(codes, "legend_mismatch")
    probs = answer.get("probabilities")
    expected_keys = {str(index) for index in range(len(LEVEL_CRITERIA))}
    if not isinstance(probs, dict) or set(probs) != expected_keys:
        _note(codes, "invalid_option")
        return None
    level_probs: list[float] = []
    for index in range(len(LEVEL_CRITERIA)):
        item = _finite_unit(probs.get(str(index)), codes)
        if item is None:
            return None
        level_probs.append(item)
    packed = _scaled_distribution(tuple(level_probs), codes)
    if packed is None:
        return None
    # Legend keys and Σ index·p are 0-based. Identity is a diagnostic note.
    expected_score = sum(index * value for index, value in enumerate(packed))
    if score_number is not None and abs(score_number - expected_score) > SCORE_IDENTITY_TOLERANCE:
        _note(codes, "score_mismatch")
    if score_number is None or confidence is None:
        return None
    return ValidatedScore(score_number, packed, confidence, tuple(LEVEL_CRITERIA))


def _check_choice(
    answer: object,
    options: tuple[str, ...],
    codes: list[str],
) -> ValidatedChoice | None:
    if not isinstance(answer, dict):
        _note(codes, "wrong_type")
        return None
    if answer.get("type") != "choice":
        _note(codes, "wrong_type")
    _shape(answer, _CHOICE_FIELDS, codes)
    choice = answer.get("choice")
    if choice not in options:
        _note(codes, "invalid_option")
    confidence = _finite_unit(answer.get("confidence"), codes)
    probs = answer.get("probabilities")
    if not isinstance(probs, dict) or set(probs) != set(options):
        _note(codes, "invalid_option")
        return None
    option_probs: list[tuple[str, float]] = []
    for option in options:
        item = _finite_unit(probs.get(option), codes)
        if item is None:
            return None
        option_probs.append((option, item))
    scaled = _scaled_distribution(tuple(value for _, value in option_probs), codes)
    if scaled is None:
        return None
    packed = tuple((option, scaled[index]) for index, (option, _) in enumerate(option_probs))
    values = tuple(value for _, value in packed)
    if choice in options and packed:
        chosen = dict(packed)[choice]
        if chosen + PROBABILITY_TOLERANCE < max(values):
            _note(codes, "choice_not_max")
    if choice not in options or confidence is None:
        return None
    return ValidatedChoice(str(choice), packed, confidence)


def validate_jev_response(prepared: object, raw_body: str) -> JevValidation:
    """Validate a complete System One body against the frozen question package.

    ``prepared`` is accepted for the call shape other lanes use; the expected
    answers are the packaged questions, not a caller-supplied subset.
    """
    del prepared
    if not isinstance(raw_body, str) or raw_body == "":
        return _fail("missing_field")
    try:
        parsed = json.loads(
            raw_body,
            parse_constant=lambda _token: (_ for _ in ()).throw(ValueError("non_finite")),
            object_pairs_hook=_pairs,
        )
    except _DuplicateKey:
        return _fail("duplicate_key")
    except (json.JSONDecodeError, ValueError, TypeError, UnicodeDecodeError):
        return _fail("invalid_json")
    if not isinstance(parsed, dict):
        return _fail("wrong_type")
    codes: list[str] = []
    _shape(parsed, _TOP_FIELDS, codes)
    if parsed.get("model") != JEV_MODEL:
        _note(codes, "model_mismatch")
    answers = parsed.get("answers")
    usage = parsed.get("usage")
    if not isinstance(answers, dict):
        _note(codes, "wrong_type")
        return _fail(*codes)
    extra_answers = set(answers) - set(ANSWER_IDS)
    missing_answers = set(ANSWER_IDS) - set(answers)
    if extra_answers:
        _note(codes, "unknown_field")
    if missing_answers:
        _note(codes, "wrong_id")
    if not isinstance(usage, dict):
        _note(codes, "wrong_type")
        return _fail(*codes)
    _shape(usage, _USAGE_FIELDS, codes)
    input_tokens = _nonneg_int(usage.get("input_tokens"))
    output_tokens = _nonneg_int(usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        _note(codes, "usage_invalid")

    nouls: list[tuple[str, float]] = []
    for question_id in NOUL_IDS:
        value = _check_noul(answers.get(question_id), codes)
        if value is not None:
            nouls.append((question_id, value))
    validated_level = _check_score(answers.get("level"), codes)
    validated_requirement_kind = _check_choice(
        answers.get("mandatory_requirement_kind"), REQUIREMENT_KIND_OPTIONS, codes,
    )
    validated_years = _check_choice(answers.get("years_floor"), YEARS_OPTIONS, codes)
    rejections = _rejection_codes(codes)
    if rejections:
        return _fail(*codes)
    if (validated_level is None or validated_requirement_kind is None
            or validated_years is None or len(nouls) != len(NOUL_IDS)
            or input_tokens is None or output_tokens is None):
        return _fail(*(codes or ("missing_field",)))
    response = ValidatedJevResponse(
        model=JEV_MODEL,
        nouls=tuple(nouls),
        level=validated_level,
        mandatory_requirement_kind=validated_requirement_kind,
        years_floor=validated_years,
        usage_input_tokens=input_tokens,
        usage_output_tokens=output_tokens,
        raw=raw_body,
        response_sha256=jev_response_sha256(raw_body),
    )
    return JevValidation(True, VALIDATOR_VERSION, _diagnostic_notes(codes), response)
