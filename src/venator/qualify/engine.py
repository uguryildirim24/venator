"""Pure evidence arithmetic and clause structure, without a verdict."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable, Literal, Mapping

from venator.qualify.posting import Atom, CanonicalPosting, Span, free_atoms
from venator.qualify.schema import CompiledProfile, Evidence as BaseEvidence, Segment

ENGINE_VERSION = "6"
_RANK = {"high_school": 0, "associate": 1, "bachelor": 2, "master": 3, "doctorate": 4}
_WORD = re.compile(r"[a-z]+", re.I)
_AUTH = (
    ("citizenship_required", re.compile(r"\b(?:citizenship|citizen|U\.?S\.? person)\b", re.I)),
    ("permanent_residency_required", re.compile(r"\b(?:permanent resident|permanent residency|green card)\b", re.I)),
    ("clearance_required", re.compile(r"\b(?:security clearance|clearance)\b", re.I)),
    ("export_control", re.compile(r"\b(?:export.control(?:led)?|ITAR|EAR)\b", re.I)),
    ("authorization_required", re.compile(r"\b(?:work authori[sz]ation|authorized to work)\b", re.I)),
)
_RECENCY = re.compile(r"graduated within|recent graduate|within the last\s+\w+\s+years|class of\s+\d{4}|graduating in\s+\d{4}", re.I)
_GPA = re.compile(r"\bGPA\b|grade point", re.I)
_START = re.compile(r"\b(?:start(?:ing)?|available|begin(?:ning)?)\s+(?:on|in|by|from)?\s*"
                    r"(?P<month>\d{4}-\d{2}(?:-\d{2})?|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})", re.I)
_GRAD = re.compile(r"\bgraduat(?:e|ed|ing|ion)\s+(?:by|in|on|before)?\s*"
                   r"(?P<month>\d{4}-\d{2}(?:-\d{2})?|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})", re.I)
_MONTH_NAMES = {name.casefold(): i for i, name in enumerate(
    "January February March April May June July August September October November December".split(), 1
)}


@dataclass(frozen=True)
class AuthFlag:
    span_id: str
    kind: str
    quote: str


@dataclass(frozen=True)
class AvailabilityClause:
    span_id: str
    quote: str
    comparison: Literal["compatible", "incompatible", "unknown"]


@dataclass(frozen=True)
class Evidence(BaseEvidence):
    as_of_month: str
    canonical: CanonicalPosting
    compiled: CompiledProfile
    spans: tuple[Span, ...]
    auth_flags: tuple[AuthFlag, ...]
    sponsorship_conflict: bool | None
    recency_clause: str | None
    gpa_clause: str | None
    start_date_clauses: tuple[AvailabilityClause, ...]
    graduation_clauses: tuple[AvailabilityClause, ...]

    def locate(self, quote: str) -> list[tuple[int, int]]:
        if not quote:
            return []
        text = self.canonical.description_text
        found: list[tuple[int, int]] = []
        start = 0
        while (position := text.find(quote, start)) >= 0:
            found.append((position, position + len(quote)))
            start = position + 1
        return found

    def locate_in_span(self, quote: str, span_id: str) -> tuple[int, int] | None:
        span = next((item for item in self.spans if item.id == span_id), None)
        if span is None or not quote:
            return None
        for start, end in self.locate(quote):
            if span.start <= start and end <= span.end:
                return start - span.start, end - span.start
        return None

    def parse_free(self, quote: str) -> tuple[Atom, ...]:
        return free_atoms(quote)


def _ordinal(month: str) -> int:
    year, number = month[:7].split("-")
    number_int = int(number)
    if not 1 <= number_int <= 12:
        raise ValueError(f"invalid month: {month}")
    return int(year) * 12 + number_int - 1


def _month_from_clause(text: str) -> str | None:
    if re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", text):
        return text[:7] if len(text) == 7 else None
    name, year = text.split()
    return f"{int(year):04d}-{_MONTH_NAMES[name.casefold()]:02d}"


def _intervals(compiled: CompiledProfile, cutoff: str) -> tuple[
    tuple[Segment, ...], Mapping[str, tuple[int, int] | None], int, frozenset[str], frozenset[str]
]:
    limit = _ordinal(cutoff)
    guaranteed: dict[str, set[int]] = {}
    occupancy: dict[str, set[int]] = {}
    months: dict[str, tuple[int, int] | None] = {}
    unknown: set[str] = set()
    future: set[str] = set()
    for entry in compiled.experience:
        if entry.fact_status != "confirmed" or entry.role_withheld or entry.interval is None:
            months[entry.id] = None
            if entry.fact_status == "confirmed" and not entry.role_withheld:
                unknown.add(entry.id)
            continue
        try:
            start = _ordinal(entry.interval.start)
            end = min(_ordinal(entry.interval.end), limit) if entry.interval.end else limit
        except (ValueError, AttributeError):
            months[entry.id] = None
            unknown.add(entry.id)
            continue
        if entry.interval.end and _ordinal(entry.interval.end) < start:
            months[entry.id] = None
            unknown.add(entry.id)
            continue
        if start > limit:
            future.add(entry.id)
            guaranteed[entry.id] = set()
            occupancy[entry.id] = set()
            months[entry.id] = (0, 0)
            continue
        guaranteed[entry.id] = set(range(start + 1, end))
        occupancy[entry.id] = set(range(start, end + 1))
        months[entry.id] = (len(guaranteed[entry.id]), len(occupancy[entry.id]))
    all_months = sorted(set().union(*guaranteed.values())) if guaranteed else []
    segments: list[Segment] = []
    previous_month: int | None = None
    previous_active: frozenset[str] = frozenset()
    for month in all_months:
        active = frozenset(key for key, month_set in guaranteed.items() if month in month_set)
        consecutive = previous_month is not None and month == previous_month + 1
        if segments and consecutive and active == previous_active:
            last = segments[-1]
            segments[-1] = replace(last, months=last.months + 1)
        else:
            segments.append(Segment(f"seg-{len(segments) + 1:02d}", 1, active,
                                    bool(segments and consecutive)))
        previous_month = month
        previous_active = active
    occupied = set().union(*occupancy.values()) if occupancy else set()
    return tuple(segments), months, len(occupied), frozenset(unknown), frozenset(future)


def enrich_degree(atom: Atom, compiled: CompiledProfile) -> Atom:
    if atom.kind != "degree" or atom.required_level not in _RANK:
        return atom
    target = _RANK[atom.required_level]
    awarded: list[str] = []
    progress: list[str] = []
    overlap: list[tuple[str, str]] = []
    for entry in compiled.education:
        if (entry.fact_status != "confirmed" or entry.withheld or
                _RANK.get(entry.degree_level, -1) < target):
            continue
        if entry.status == "awarded" and not compiled.education_policy.kill_completed_degree:
            awarded.append(entry.id)
        elif entry.status == "in_progress":
            progress.append(entry.id)
        fields = set(_WORD.findall((entry.field_of_study or "").casefold()))
        word = next((field for field in atom.field_words if field in fields), "none")
        overlap.append((entry.id, word))
    return replace(atom, awarded_at_or_above=tuple(awarded),
                   in_progress_at_or_above=tuple(progress), field_overlap=tuple(overlap))


def _authorization(spans: tuple[Span, ...], compiled: CompiledProfile) -> tuple[tuple[AuthFlag, ...], bool | None]:
    flags: list[AuthFlag] = []
    for span in spans:
        for kind, pattern in _AUTH:
            for match in pattern.finditer(span.text):
                flags.append(AuthFlag(span.id, kind, match.group(0)))
        for match in re.finditer(r"\bsponsor(?:ship)?\b", span.text, re.I):
            before = re.split(r"[.;:]", span.text[max(0, match.start() - 38):match.start()])[-1].casefold()
            after = span.text[match.end():match.end() + 22].casefold()
            no = bool(re.search(r"\b(?:no|without|not|unable to|cannot|won't)\b", before)) or bool(re.search(r"\b(?:unavailable|not available)\b", after))
            flags.append(AuthFlag(span.id, "no_sponsorship" if no else "sponsorship_available", match.group(0)))
    no_sponsor = any(flag.kind == "no_sponsorship" for flag in flags)
    requires = compiled.authorization.requires_sponsorship
    conflict = None if no_sponsor and requires is None else bool(no_sponsor and requires)
    return tuple(flags), conflict


def _availability(spans: tuple[Span, ...], compiled: CompiledProfile) -> tuple[tuple[AvailabilityClause, ...], tuple[AvailabilityClause, ...]]:
    starts: list[AvailabilityClause] = []
    graduations: list[AvailabilityClause] = []
    avail = compiled.availability
    for span in spans:
        for match in _START.finditer(span.text):
            month = _month_from_clause(match.group("month"))
            if month is None or avail.fact_status != "confirmed" or avail.available_from is None:
                comparison = "unknown"
            else:
                comparison = "compatible" if _ordinal(avail.available_from) <= _ordinal(month) and (
                    avail.available_until is None or _ordinal(avail.available_until) >= _ordinal(month)) else "incompatible"
            starts.append(AvailabilityClause(span.id, match.group(0), comparison))
        for match in _GRAD.finditer(span.text):
            month = _month_from_clause(match.group("month"))
            if month is None or avail.fact_status != "confirmed" or not avail.expected_graduation_confirmed or not avail.expected_graduation:
                comparison = "unknown"
            else:
                comparison = "compatible" if _ordinal(avail.expected_graduation) <= _ordinal(month) else "incompatible"
            graduations.append(AvailabilityClause(span.id, match.group(0), comparison))
    return tuple(starts), tuple(graduations)


def evidence(compiled: CompiledProfile, canonical: CanonicalPosting, as_of_month: str) -> Evidence:
    """Compute facts only. The cutoff month is never guaranteed credit."""
    as_of = as_of_month[:7]
    _ordinal(as_of)
    reference = compiled.availability.reference_date
    if reference:
        reference = reference[:7]
        _ordinal(reference)
    cutoff = min(as_of, reference) if reference else as_of
    source: Literal["as_of", "reference_date"] = "reference_date" if reference and reference < as_of else "as_of"
    segments, months, occupied, unknown, future = _intervals(compiled, cutoff)
    spans = tuple(replace(span, atoms=tuple(enrich_degree(atom, compiled) for atom in span.atoms)) for span in canonical.spans)
    auth_flags, conflict = _authorization(spans, compiled)
    starts, graduations = _availability(spans, compiled)
    education_refutable = all(
        entry.fact_status == "confirmed" and not entry.withheld and entry.degree_level != "unknown" and entry.status != "unknown"
        for entry in compiled.education
    )
    experience_refutable = all(
        entry.fact_status == "confirmed" and not entry.role_withheld and entry.id not in unknown and entry.id not in future
        for entry in compiled.experience
    )
    return Evidence(segments, months, occupied, unknown, future, cutoff, source,
                    experience_refutable, education_refutable, as_of, canonical, compiled, spans,
                    auth_flags, conflict,
                    next((span.id for span in spans if _RECENCY.search(span.text)), None),
                    next((span.id for span in spans if _GPA.search(span.text)), None),
                    starts, graduations)
