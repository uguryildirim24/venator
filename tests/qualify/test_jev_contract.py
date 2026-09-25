"""Jev contract types, response validator and golden fixtures."""

from __future__ import annotations

import json
import math
import pickle
from copy import deepcopy
from pathlib import Path

from venator.profile.loader import load_profile
from venator.qualify.jev_contract import (
    JevContext,
    JevExclusion,
    JevResolution,
    JEV_MODEL,
    PROBABILITY_TOLERANCE,
    SUM_BOUND,
    VALIDATOR_VERSION,
    validate_jev_response,
)
from venator.qualify.jev_policy import prepare_jev_case
from venator.qualify.jev_questions import (
    LEVEL_CRITERIA,
    REQUIREMENT_KIND_OPTIONS,
    YEARS_OPTIONS,
)

ROOT = Path(__file__).resolve().parent / "fixtures" / "jev"


def _case():
    profile = load_profile(ROOT / "profile")
    posting = json.loads((ROOT / "posting.json").read_text(encoding="utf-8"))
    prepared = prepare_jev_case(posting, profile, "2026-09")
    assert prepared.case is not None
    return prepared.case


def _mutate(body: dict, path: tuple[str, ...], value: object) -> str:
    clone = deepcopy(body)
    cursor = clone
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    return json.dumps(clone, separators=(",", ":"), ensure_ascii=False)


def test_golden_valid_response_passes_and_invalid_fails() -> None:
    case = _case()
    valid = (ROOT / "valid_raw_response.json").read_text(encoding="utf-8")
    result = validate_jev_response(case, valid)
    assert result.passed is True
    assert result.version == VALIDATOR_VERSION
    assert result.error_codes == ()
    assert result.response is not None
    assert result.response.model == JEV_MODEL
    assert len(result.response.nouls) == 9
    invalid = (ROOT / "invalid_response.json").read_text(encoding="utf-8")
    failed = validate_jev_response(case, invalid)
    assert failed.passed is False
    assert "duplicate_key" in failed.error_codes
    assert failed.response is None


def test_context_types_are_picklable() -> None:
    exclusion = JevExclusion(
        "restricted_role", "model_assisted_policy", ("citizenship_or_clearance",),
        (("citizenship_or_clearance", 0.9),), "fired", "exclude", True,
    )
    context = JevContext(
        "fixture:one", "rev", "2026-09", "input", "jev:abc", "accepted",
        "assess", "resp", "exclude", 0.1, 0.2, (exclusion,), "restricted_role",
        ("below_threshold",), ("sponsorship_note",),
    )
    resolution = JevResolution(context, "input", None)
    assert pickle.loads(pickle.dumps(context)) == context
    assert pickle.loads(pickle.dumps(resolution)).fallback_reason is None


def test_probability_endpoints_and_score_equality() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    body["answers"]["would_apply"]["noul"] = 0.0
    body["answers"]["plausible_candidate"]["noul"] = 1.0
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    assert validate_jev_response(case, raw).passed is True
    over = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    over["answers"]["would_apply"]["noul"] = 1.0000001
    assert "out_of_range" in validate_jev_response(case, json.dumps(over)).error_codes
    nan_body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    nan_raw = json.dumps(nan_body).replace("0.85", "NaN", 1)
    assert "non_finite" in validate_jev_response(case, nan_raw).error_codes or (
        "invalid_json" in validate_jev_response(case, nan_raw).error_codes
    )


def test_boolean_where_number_required() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    raw = _mutate(body, ("answers", "domain_match", "noul"), True)
    assert "boolean_as_number" in validate_jev_response(case, raw).error_codes


def test_noul_confidence_and_unknown_answer_rejected() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    body["answers"]["would_apply"]["confidence"] = 0.9
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    assert "noul_confidence_forbidden" in validate_jev_response(case, raw).error_codes
    extra = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    extra["answers"]["surprise"] = {"type": "noul", "noul": 0.1}
    raw_extra = json.dumps(extra, separators=(",", ":"), ensure_ascii=False)
    assert "unknown_field" in validate_jev_response(case, raw_extra).error_codes


def test_legend_must_match_frozen_criteria() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    body["answers"]["level"]["legend"]["0"] = "not the frozen criterion"
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    assert "legend_mismatch" in validate_jev_response(case, raw).error_codes
    assert body["answers"]["level"]["legend"]["1"] == LEVEL_CRITERIA[1]


def test_score_identity_is_diagnostic_and_records_returned_score() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    body["answers"]["level"]["score"] = 1.5
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    result = validate_jev_response(case, raw)
    assert result.passed is True
    assert result.error_codes == ("score_mismatch",)
    assert result.response is not None
    assert result.response.level.score == 1.5
    body["answers"]["level"]["score"] = 0.4 + 1e-9
    close = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    close_result = validate_jev_response(case, close)
    assert close_result.passed is True
    assert close_result.error_codes == ()
    body["answers"]["level"]["score"] = 1.4
    one_based = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    indexed = validate_jev_response(case, one_based)
    assert indexed.passed is True
    assert "score_mismatch" in indexed.error_codes
    assert indexed.response is not None
    assert indexed.response.level.score == 1.4


def test_choice_max_allows_ties() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    equal = 1.0 / len(YEARS_OPTIONS)
    body["answers"]["years_floor"]["probabilities"] = {option: equal for option in YEARS_OPTIONS}
    body["answers"]["years_floor"]["choice"] = "unknown"
    tied = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    assert validate_jev_response(case, tied).passed is True
    body["answers"]["years_floor"]["choice"] = "none"
    body["answers"]["years_floor"]["probabilities"] = {
        option: (0.4 if option == "none" else (0.6 if option == "unknown" else 0.0))
        for option in YEARS_OPTIONS
    }
    not_max = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    assert "choice_not_max" in validate_jev_response(case, not_max).error_codes


def test_requirement_kind_choice_is_closed() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    result = validate_jev_response(case, json.dumps(body, separators=(",", ":")))
    assert result.passed is True
    assert result.response is not None
    assert result.response.mandatory_requirement_kind.choice == "technique"
    assert tuple(dict(result.response.mandatory_requirement_kind.probabilities)) == REQUIREMENT_KIND_OPTIONS

    body["answers"]["mandatory_requirement_kind"]["choice"] = "credential"
    refused = validate_jev_response(case, json.dumps(body, separators=(",", ":")))
    assert refused.passed is False
    assert "invalid_option" in refused.error_codes


def test_model_mismatch_and_distribution_sum() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    body["model"] = "jev-latest"
    mismatched = validate_jev_response(case, json.dumps(body))
    assert mismatched.passed is False
    assert "model_mismatch" in mismatched.error_codes
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    body["answers"]["level"]["probabilities"]["0"] = 0.74
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    summed = validate_jev_response(case, raw)
    assert summed.passed is True
    assert "sum_mismatch" in summed.error_codes
    assert "sum_out_of_bound" not in summed.error_codes
    assert summed.response is not None
    packed = summed.response.level.probabilities
    assert math.isclose(packed[0], 0.74 / 1.04)
    assert math.isclose(sum(packed), 1.0, abs_tol=PROBABILITY_TOLERANCE)


def test_validator_ignores_prepared_subset() -> None:
    case = _case()
    valid = (ROOT / "valid_raw_response.json").read_text(encoding="utf-8")
    assert validate_jev_response(None, valid).passed is True
    assert validate_jev_response(case, valid).response is not None
    assert math.isclose(validate_jev_response(case, valid).response.noul("would_apply"), 0.85)


def test_sum_bound_rescales_inside_and_refuses_outside() -> None:
    case = _case()
    body = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    body["answers"]["level"]["probabilities"]["0"] = 0.74
    inside = validate_jev_response(case, json.dumps(body, separators=(",", ":")))
    assert inside.passed is True
    assert "sum_mismatch" in inside.error_codes
    assert inside.response is not None
    assert math.isclose(sum(inside.response.level.probabilities), 1.0, abs_tol=PROBABILITY_TOLERANCE)

    body["answers"]["level"]["probabilities"]["0"] = 0.76
    high = validate_jev_response(case, json.dumps(body, separators=(",", ":")))
    assert high.passed is False
    assert "sum_out_of_bound" in high.error_codes
    assert high.response is None

    body["answers"]["level"]["probabilities"]["0"] = 0.64
    low = validate_jev_response(case, json.dumps(body, separators=(",", ":")))
    assert low.passed is False
    assert "sum_out_of_bound" in low.error_codes
    assert low.response is None
    assert SUM_BOUND == 0.05

    years = json.loads((ROOT / "valid_raw_response.json").read_text(encoding="utf-8"))
    years["answers"]["years_floor"]["probabilities"]["none"] = 0.99
    years_ok = validate_jev_response(case, json.dumps(years, separators=(",", ":")))
    assert years_ok.passed is True
    assert "sum_mismatch" in years_ok.error_codes
    assert years_ok.response is not None
    year_total = sum(value for _, value in years_ok.response.years_floor.probabilities)
    assert math.isclose(year_total, 1.0, abs_tol=PROBABILITY_TOLERANCE)

    for option, value in (("under_1", 0.07), ("none", 0.89)):
        drifted = deepcopy(years)
        drifted["answers"]["years_floor"]["probabilities"]["none"] = 0.95
        drifted["answers"]["years_floor"]["probabilities"][option] = value
        refused = validate_jev_response(case, json.dumps(drifted, separators=(",", ":")))
        assert refused.passed is False
        assert "sum_out_of_bound" in refused.error_codes
        assert refused.response is None
