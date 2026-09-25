"""Bounded date, cohort, enrollment, and pay-range eligibility helpers.

These functions read only explicit Posting fields and short, local phrases from
the stored description.  Missing dates, pay, currency, and pay period stay
unknown; a missing value is never replaced with today's date or zero pay.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import Literal

from venator.profile.schema import TimingPolicy

Status = Literal["pass", "kill", "unknown"]


@dataclass(frozen=True)
class EligibilityFinding:
    """One deterministic eligibility observation."""

    status: Status
    reason: str
    fact: str


@dataclass(frozen=True)
class PayRange:
    """A Posting's stated pay, retaining the units needed to compare it."""

    minimum: float | None = None
    maximum: float | None = None
    currency: str | None = None
    period: str | None = None


def parse_date(value: object) -> date | None:
    """Parse a date or date-time without treating arbitrary text as a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if isfinite(number) else None
    if isinstance(value, str):
        match = re.search(r"\d+(?:[,.]\d+)?", value.replace(",", ""))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def _currency(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().upper()
    if not raw:
        return None
    aliases = {"$": "USD", "US$": "USD", "£": "GBP", "€": "EUR"}
    return aliases.get(raw, raw)


def _period(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().casefold().replace(" ", "_").replace("-", "_")
    aliases = {
        "hour": "hour",
        "hourly": "hour",
        "hr": "hour",
        "week": "week",
        "weekly": "week",
        "month": "month",
        "monthly": "month",
        "year": "year",
        "yearly": "year",
        "annual": "year",
        "annually": "year",
    }
    return aliases.get(raw, raw or None)


def pay_range(posting: Mapping[str, object]) -> PayRange | None:
    """Extract common structured pay shapes while retaining unknown units."""
    candidates: list[Mapping[str, object]] = []
    for key in ("salary", "salary_range", "compensation", "pay", "pay_range"):
        value = posting.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)

    direct = {
        "minimum": posting.get("salary_min", posting.get("min_salary")),
        "maximum": posting.get("salary_max", posting.get("max_salary")),
        "currency": posting.get("salary_currency", posting.get("currency")),
        "period": posting.get("salary_period", posting.get("pay_period")),
    }
    if any(value is not None for value in direct.values()):
        candidates.insert(0, direct)

    scalar = posting.get("salary")
    if scalar is not None and not isinstance(scalar, Mapping):
        value = _number(scalar)
        if value is not None:
            candidates.insert(0, {"minimum": value, "maximum": value})

    if not candidates:
        return None
    for candidate in candidates:
        minimum = _number(
            candidate.get("minimum", candidate.get("min", candidate.get("low", candidate.get("amount"))))
        )
        maximum = _number(
            candidate.get("maximum", candidate.get("max", candidate.get("high", candidate.get("amount"))))
        )
        currency = _currency(candidate.get("currency", candidate.get("currency_code")))
        period = _period(candidate.get("period", candidate.get("unit", candidate.get("pay_period"))))
        if any(value is not None for value in (minimum, maximum, currency, period)):
            return PayRange(minimum, maximum, currency, period)
    return None


_SEASON_MONTHS = {
    "spring": (3, 4, 5),
    "summer": (6, 7, 8),
    "fall": (9, 10, 11),
    "autumn": (9, 10, 11),
    "winter": (12, 1, 2),
}
_SEASON_ORDER = {"spring": 0, "summer": 1, "fall": 2, "autumn": 2, "winter": 3}
_COHORT = re.compile(r"\b(?P<season>spring|summer|fall|autumn|winter)\s+(?P<year>20\d{2})\b", re.I)
_INTERNSHIP = re.compile(r"\b(?:intern(?:ship)?|co[- ]?op|cooperative education|placement)\b", re.I)


def _cohort(text: str, posting: Mapping[str, object]) -> tuple[str, int] | None:
    opportunity = str(posting.get("opportunity_type") or "")
    title = str(posting.get("title") or "")
    description = str(posting.get("description") or posting.get("description_html") or "")
    source = f"{title} {opportunity} {description}"
    if not (_INTERNSHIP.search(source) or opportunity.casefold() in {"intern", "internship", "co-op", "coop"}):
        return None
    match = _COHORT.search(source)
    return (match["season"].casefold(), int(match["year"])) if match else None


def _season_for(month: int) -> str:
    for season, months in _SEASON_MONTHS.items():
        if month in months:
            return season
    return ""


def posting_start(posting: Mapping[str, object]) -> date | None:
    """Read a structured role/program start date, then a bounded text phrase."""
    for key in (
        "start_date",
        "start_at",
        "role_start",
        "program_start",
        "job_start",
        "employment_start",
    ):
        if found := parse_date(posting.get(key)):
            return found
    text = " ".join(
        str(posting.get(key) or "")
        for key in ("title", "description", "description_html")
    )
    # Only parse an ISO date or a month/year directly introduced as a start;
    # ordinary historical dates in a description are not a role start.
    iso = re.search(r"\b(?:start(?:s|ing)?|begin(?:s|ning)?)\s+(?:on\s+)?(20\d{2}-\d{2}-\d{2})\b", text, re.I)
    if iso and (found := parse_date(iso.group(1))):
        return found
    month = re.search(
        r"\b(?:start(?:s|ing)?|begin(?:s|ning)?)\s+(?:in\s+)?"
        r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(?P<year>20\d{2})\b",
        text,
        re.I,
    )
    if month:
        month_number = tuple(
            name.casefold() for name in (
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            )
        ).index(month["month"].casefold()) + 1
        return date(int(month["year"]), month_number, 1)
    return None


def _before(first: tuple[str, int], second: tuple[str, int]) -> bool:
    return (first[1], _SEASON_ORDER[first[0]]) < (second[1], _SEASON_ORDER[second[0]])


def timing_window_finding(posting: Mapping[str, object], policy: TimingPolicy) -> EligibilityFinding:
    """Check only an explicit start/cohort against an availability window.

    Missing start evidence stays ``unknown`` whenever this window is configured.
    """
    text = " ".join(
        str(posting.get(key) or "")
        for key in ("title", "description", "description_html")
    )
    reference = policy.reference_date or date.today()
    cohort = _cohort(text, posting)
    reference_cohort = (_season_for(reference.month), reference.year)
    if cohort and _before(cohort, reference_cohort):
        return EligibilityFinding("kill", f"explicit {cohort[0]} {cohort[1]} cohort is past", "past_cohort")
    if cohort and policy.allowed_years and cohort[1] not in set(policy.allowed_years):
        return EligibilityFinding(
            "kill",
            f"explicit {cohort[0]} {cohort[1]} cohort is outside the requested years",
            "year_mismatch",
        )
    if cohort and policy.allowed_seasons and cohort[0] not in set(policy.allowed_seasons):
        return EligibilityFinding(
            "kill",
            f"explicit {cohort[0]} cohort is outside the requested seasons",
            "season_mismatch",
        )

    start = posting_start(posting)
    if (policy.allowed_years or policy.allowed_seasons or policy.allowed_months) and not cohort and start is None:
        return EligibilityFinding(
            "unknown",
            "role season/year is not stated",
            "timing_unknown",
        )
    if start is not None and policy.allowed_seasons and not any(
        start.month in _SEASON_MONTHS[season] for season in policy.allowed_seasons
    ):
        return EligibilityFinding(
            "kill", f"role starts in month {start.month}, outside requested seasons", "season_mismatch"
        )
    if start is not None and policy.allowed_years and start.year not in set(policy.allowed_years):
        return EligibilityFinding(
            "kill", f"role starts in {start.year}, outside requested years", "year_mismatch"
        )
    if policy.allowed_months:
        months = set(policy.allowed_months)
        if start is not None and start.month not in months:
            return EligibilityFinding(
                "kill", f"role starts in month {start.month}, outside requested months", "month_mismatch"
            )
        if cohort and not months.intersection(_SEASON_MONTHS[cohort[0]]):
            return EligibilityFinding(
                "kill", f"{cohort[0]} cohort has no requested start month", "month_mismatch"
            )

    if policy.available_from is not None or policy.available_until is not None:
        if start is None:
            return EligibilityFinding("unknown", "role start date is not stated", "timing_unknown")
        if policy.available_from is not None and start < policy.available_from:
            return EligibilityFinding(
                "kill", f"role starts {start.isoformat()}, before availability", "before_available"
            )
        if policy.available_until is not None and start > policy.available_until:
            return EligibilityFinding(
                "kill", f"role starts {start.isoformat()}, after availability", "after_available"
            )
    return EligibilityFinding("pass", "explicit timing window is compatible", "timing_clear")


_DEGREE_REQUIRED = re.compile(
    r"\b(?:bachelor(?:'s|’s)?|master(?:'s|’s)?|ph\.?d\.?|doctorate|degree)\b"
    r"[^.;]{0,100}\b(?:by|before)\b(?:[^.;]{0,30})\b(?:start|first day|joining|commencement)\b",
    re.I,
)
_ENROLL_CURRENT = re.compile(
    r"\b(?:currently|now|must be|are)\s+(?:be\s+)?enrolled\b(?!\s+by\b)|"
    r"\bcurrent(?:ly)?\s+(?:a\s+)?student\b",
    re.I,
)
_ENROLL_FUTURE = re.compile(r"\b(?:will|must)\s+be\s+enrolled\s+by\b", re.I)


def _timing_finding(posting: Mapping[str, object], policy: TimingPolicy) -> EligibilityFinding:
    text = " ".join(
        str(posting.get(key) or "")
        for key in ("title", "description", "description_html")
    )
    reference = policy.reference_date or date.today()
    deadline = parse_date(posting.get("application_deadline"))
    if deadline and deadline < reference:
        return EligibilityFinding("kill", f"application deadline {deadline.isoformat()} has passed", "expired")

    window = timing_window_finding(posting, policy)
    if window.status == "kill":
        return window

    # A missing date cannot waive an independently explicit enrollment conflict.
    requirements = _education_timing_finding(posting, policy, text)
    if requirements.status == "kill":
        return requirements
    if window.status == "unknown":
        return window
    if requirements.status == "unknown":
        return requirements

    cohort = _cohort(text, posting)
    if any((deadline, cohort, posting_start(posting))) or policy.configured:
        return EligibilityFinding("pass", "explicit timing is compatible or no contradiction was found", "timing_clear")
    return EligibilityFinding("pass", "no explicit timing is stated", "timing_none")


def _requires_current_enrollment(text: str) -> bool:
    """A graduate-or-student offer does not require the student branch alone."""
    alternative = re.compile(
        r"\b(?:recent\s+)?graduate\b[^.;]{0,100}\bor\s+(?:a\s+)?current\s+student\b|"
        r"\bcurrent\s+student\b[^.;]{0,60}\bor\s+(?:a\s+)?(?:recent\s+)?graduate\b",
        re.I,
    )
    spans = [match.span() for match in alternative.finditer(text)]
    return any(
        not any(start <= match.start() < end for start, end in spans)
        for match in _ENROLL_CURRENT.finditer(text)
    )


def _education_timing_finding(posting: Mapping[str, object], policy: TimingPolicy, text: str) -> EligibilityFinding:
    start = posting_start(posting)
    # Definite current-enrollment conflicts outrank unknown degree dates.
    if _requires_current_enrollment(text) and policy.enrollment == "future":
        return EligibilityFinding("kill", "Posting requires current enrollment; Profile states future enrollment", "enrollment_mismatch")

    if policy.expected_graduation_confirmed and policy.expected_graduation is not None and _DEGREE_REQUIRED.search(text):
        if start is None:
            return EligibilityFinding("unknown", "degree is required by role start, but the start date is not stated", "degree_timing")
        if policy.expected_graduation_precision == "month":
            graduation_month = (
                policy.expected_graduation.year,
                policy.expected_graduation.month,
            )
            start_month = (start.year, start.month)
            if start_month < graduation_month:
                return EligibilityFinding(
                    "kill",
                    f"confirmed graduation month {policy.expected_graduation:%Y-%m} is after role start {start.isoformat()}",
                    "degree_after_start",
                )
            if start_month == graduation_month:
                return EligibilityFinding(
                    "unknown",
                    f"confirmed graduation month {policy.expected_graduation:%Y-%m} overlaps role start {start.isoformat()}",
                    "degree_timing",
                )
        elif policy.expected_graduation > start:
            return EligibilityFinding(
                "kill",
                f"confirmed graduation {policy.expected_graduation.isoformat()} is after role start {start.isoformat()}",
                "degree_after_start",
            )

    if _requires_current_enrollment(text):
        if policy.enrollment in {"current", "either"}:
            pass
        else:
            return EligibilityFinding("unknown", "Posting requires current enrollment, but Profile enrollment is not confirmed", "enroll_unknown")
    if _ENROLL_FUTURE.search(text) and policy.enrollment == "current":
        return EligibilityFinding("unknown", "Posting requires future enrollment timing; current enrollment is distinct", "enroll_timing")

    return EligibilityFinding("pass", "no education timing contradiction was found", "timing_clear")


def _salary_finding(posting: Mapping[str, object], floor: int | None, currency: str | None, period: str | None) -> EligibilityFinding:
    if floor is None:
        return EligibilityFinding("pass", "no salary floor is configured", "salary_none")
    stated = pay_range(posting)
    if stated is None or stated.minimum is None and stated.maximum is None:
        return EligibilityFinding("unknown", "salary is not stated; it was not treated as zero", "salary_unknown")
    expected_currency = _currency(currency)
    expected_period = _period(period)
    if expected_currency is None or expected_period is None:
        return EligibilityFinding(
            "unknown", "Profile salary floor currency or pay period is not stated", "salary_unknown"
        )
    if expected_currency and stated.currency != expected_currency:
        return EligibilityFinding(
            "unknown",
            f"salary currency is {stated.currency or 'not stated'}, while the Profile floor is {expected_currency}",
            "currency_unknown" if stated.currency is None else "salary_currency",
        )
    if expected_period and stated.period != expected_period:
        return EligibilityFinding(
            "unknown",
            f"salary period is {stated.period or 'not stated'}, while the Profile floor is per {expected_period}",
            "period_unknown" if stated.period is None else "salary_period",
        )
    maximum = stated.maximum
    if maximum is None and (stated.minimum is None or stated.minimum < floor):
        return EligibilityFinding(
            "unknown", "stated salary minimum is below the floor, but no ceiling is stated", "salary_unknown"
        )
    if maximum is not None and maximum < floor:
        return EligibilityFinding(
            "kill",
            f"stated salary ceiling {maximum:g} is below the floor {floor:g}"
            f"{f' {expected_currency}' if expected_currency else ''}"
            f"{f' per {expected_period}' if expected_period else ''}",
            "salary_below_floor",
        )
    return EligibilityFinding("pass", "stated salary meets or may meet the configured floor", "salary_clear")


def eligibility_finding(posting: Mapping[str, object], policy: TimingPolicy, *, salary_floor: int | None = None, salary_currency: str | None = None, salary_period: str | None = None) -> EligibilityFinding:
    """Combine timing and salary checks, preserving the first explicit kill."""
    timing = _timing_finding(posting, policy)
    salary = _salary_finding(posting, salary_floor, salary_currency, salary_period)
    if timing.status == "kill":
        return timing
    if salary.status == "kill":
        return salary
    if timing.status == "unknown":
        return timing
    if salary.status == "unknown":
        return salary
    if salary_floor is not None:
        return salary
    return timing


def eligibility_relevant(posting: Mapping[str, object], policy: TimingPolicy, *, salary_floor: int | None = None) -> bool:
    """Whether a row has explicit timing/pay evidence worth recording."""
    if salary_floor is not None or policy.configured:
        return True
    if posting.get("application_deadline") or posting.get("published_at"):
        return True
    if pay_range(posting) is not None:
        return True
    text = " ".join(str(posting.get(key) or "") for key in ("title", "description", "description_html"))
    return bool(_COHORT.search(text) and _INTERNSHIP.search(text))


__all__ = [
    "EligibilityFinding",
    "PayRange",
    "eligibility_finding",
    "eligibility_relevant",
    "parse_date",
    "pay_range",
    "posting_start",
    "timing_window_finding",
]
