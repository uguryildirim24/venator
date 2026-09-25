"""Evidence arithmetic, atoms, clauses, and quote binding."""

from __future__ import annotations

import hashlib
import calendar
import random
from dataclasses import fields, replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from venator.discover.store import load_postings
from venator.profile.loader import load_profile
from venator.qualify.compile import compile_profile
from venator.qualify.engine import Evidence, evidence
from venator.qualify.posting import canonical_posting, parse_atoms
from venator.qualify.schema import CompiledProfile, Interval

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def base():
    return compile_profile(load_profile(ROOT / "tests/qualify/fixtures/profiles/two-degrees"), as_of_month="2026-09")


def posting(text: str):
    return canonical_posting({"key": "fixture:test", "description_html": f"<h2>Requirements</h2><p>{text}</p>"}, {})


def history(base, intervals: list[tuple[str, str, str | None]], *, reference: str | None = None):
    template = base.experience[0]
    entries = tuple(replace(template, id=key, fact_status="confirmed", role_withheld=None,
                            interval=Interval(start, end, end is None), current=end is None)
                    for key, start, end in intervals)
    return replace(base, experience=entries,
                   availability=replace(base.availability, reference_date=reference))


@pytest.mark.parametrize("case,intervals,as_of,reference,guaranteed,occupancy,future", [
    ("G1", [("exp-01", "2025-01", "2027-01")], "2025-03", None, 1, 3, False),
    ("G2", [("exp-01", "2026-09", None)], "2026-09", None, 0, 1, False),
    ("G3", [("exp-01", "2025-09", None)], "2026-09", "2026-03", 5, 7, False),
    ("G4", [("exp-01", "2026-11", "2027-05")], "2026-09", None, 0, 0, True),
    ("G5", [("exp-01", "2025-01", "2026-01")], "2026-02", None, 11, 13, False),
    ("G6", [("exp-01", "2025-01", "2025-06"), ("exp-02", "2025-06", "2025-12")], "2026-09", None, 9, 12, False),
    ("G7", [("exp-01", "2026-09", None)], "2026-09", None, 0, 1, False),
])
def test_named_month_cases(base, case, intervals, as_of, reference, guaranteed, occupancy, future) -> None:
    result = evidence(history(base, intervals, reference=reference), posting("1 year experience required"), as_of)
    assert result.guaranteed(key for key, _, _ in intervals) == guaranteed, case
    assert result.occupancy_all == occupancy, case
    assert bool(result.entries_future) == future, case
    assert result.cutoff_source == ("reference_date" if reference else "as_of")
    if case == "G6":
        assert [segment.months for segment in result.segments] == [4, 5]
        assert result.segments[1].contiguous_with_previous is False


def test_same_month_day_is_identical(base) -> None:
    compiled = history(base, [("exp-01", "2026-09", None)])
    one = evidence(compiled, posting("1 year experience required"), "2026-09-29")
    two = evidence(compiled, posting("1 year experience required"), "2026-09-30")
    assert one == two


def test_boundary_nested_unknown_and_segments(base) -> None:
    boundary = evidence(history(base, [("exp-01", "2026-08", "2026-09")]), posting("1 year experience"), "2026-09")
    assert boundary.months_by_entry["exp-01"] == (0, 2)
    nested = evidence(history(base, [("exp-01", "2024-01", "2025-12"),
                                     ("exp-02", "2024-03", "2025-10"),
                                     ("exp-03", "2024-05", "2025-08")]), posting("1 year experience"), "2026-01")
    assert any(len(segment.active) == 3 for segment in nested.segments)
    assert nested.guaranteed({"exp-01", "exp-02"}) <= sum(nested.months_by_entry[key][0] for key in ("exp-01", "exp-02"))
    assert sum(segment.months for segment in nested.segments) == nested.guaranteed({"exp-01", "exp-02", "exp-03"})
    invalid = history(base, [("exp-01", "2026-05", "2026-03")])
    result = evidence(invalid, posting("1 year experience"), "2026-09")
    assert "exp-01" in result.entries_unknown_interval and result.months_by_entry["exp-01"] is None


def test_two_histories_keep_provenance(base) -> None:
    a = history(base, [("exp-01", "2024-01", "2024-12"), ("exp-02", "2024-01", "2024-12"),
                       ("exp-03", "2025-01", "2025-12")])
    b = history(base, [("exp-01", "2024-01", "2024-12"), ("exp-03", "2024-01", "2024-12"),
                       ("exp-02", "2025-01", "2025-12")])
    ea = evidence(a, posting("1 year experience"), "2026-01")
    eb = evidence(b, posting("1 year experience"), "2026-01")
    assert ea.guaranteed({"exp-01", "exp-02"}) == 10
    assert eb.guaranteed({"exp-01", "exp-02"}) == 20
    assert ea.segments != eb.segments


def test_one_thousand_history_calendar_bound(base) -> None:
    rng = random.Random(73117)
    for _ in range(1_000):
        intervals: list[tuple[str, str, str | None]] = []
        for index in range(rng.randint(1, 4)):
            start = 2025 * 12 + rng.randrange(1, 24)
            end = start + rng.randrange(0, 18)
            def fmt(value: int) -> str:
                return f"{value // 12:04d}-{value % 12 + 1:02d}"
            intervals.append((f"exp-{index + 1:02d}", fmt(start), None if rng.randrange(4) == 0 else fmt(end)))
        cutoff = f"2026-{rng.randrange(1, 13):02d}"
        reference = f"2026-{rng.randrange(1, 13):02d}" if rng.randrange(2) else None
        compiled = history(base, intervals, reference=reference)
        result = evidence(compiled, posting("1 year experience required"), cutoff)
        assert result.segments == evidence(replace(compiled, experience=tuple(reversed(compiled.experience))),
                                           posting("1 year experience required"), cutoff).segments
        observed = date(int(result.cutoff_month[:4]), int(result.cutoff_month[5:]), 1)
        guaranteed_months: set[int] = set()
        # A month in a segment is recovered from its active entries below.
        for _, start, end in intervals:
            s = int(start[:4]) * 12 + int(start[5:]) - 1
            e = int((end or result.cutoff_month)[:4]) * 12 + int((end or result.cutoff_month)[5:]) - 1
            x = int(result.cutoff_month[:4]) * 12 + int(result.cutoff_month[5:]) - 1
            guaranteed_months.update(range(s + 1, min(e, x)))
        assert len(guaranteed_months) == result.guaranteed(key for key, _, _ in intervals)
        for _sample in range(100):
            assignments: list[tuple[date, date]] = []
            for _, start, end in intervals:
                sy, sm = int(start[:4]), int(start[5:])
                begin = date(sy, sm, rng.randint(1, calendar.monthrange(sy, sm)[1]))
                if end:
                    ey, em = int(end[:4]), int(end[5:])
                    finish = date(ey, em, rng.randint(1, calendar.monthrange(ey, em)[1]))
                else:
                    finish = observed - timedelta(days=1)
                if begin < observed and finish >= begin:
                    assignments.append((begin, min(finish, observed - timedelta(days=1))))
            touched: set[int] = set()
            for begin, finish in assignments:
                month = begin.year * 12 + begin.month - 1
                last = finish.year * 12 + finish.month - 1
                touched.update(range(month, last + 1))
            assert result.occupancy_all >= len(touched)
            for month in guaranteed_months:
                year, number = divmod(month, 12)
                first = date(year, number + 1, 1)
                last = date(year, number + 1, calendar.monthrange(year, number + 1)[1])
                assert any(begin <= first and finish >= last for begin, finish in assignments)


def test_thirty_year_phrasings_and_high_years() -> None:
    numbers = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    clauses = [f"{number} years of laboratory experience" for number in numbers]
    clauses += [f"{i} years in business" for i in (1, 2, 3, 4, 5, 10, 15, 20, 25, 30)]
    assert len(clauses) == 30
    for index, clause in enumerate(clauses):
        atoms = parse_atoms(clause, "req-01")
        assert len(atoms) == 1 and atoms[0].kind == "years", clause
        assert atoms[0].years_min == (index + 1 if index < 20 else (1, 2, 3, 4, 5, 10, 15, 20, 25, 30)[index - 20])
    high = parse_atoms("25 years in business", "req-01")[0]
    assert high.high_years and high.scope_text == "in business"
    assert parse_atoms("3–7 years experience", "req-01")[0].years_max == 7


def test_degree_lattice_aliases_attainment_and_draft(base) -> None:
    for phrase, level in (("high school diploma", "high_school"), ("associate degree", "associate"),
                          ("BS", "bachelor"), ("BA", "bachelor"), ("MS", "master"),
                          ("MBA", "master"), ("PhD", "doctorate")):
        assert parse_atoms(phrase, "req-01")[0].required_level == level
    for phrase, attainment in (("completed BS", "awarded"), ("holds a BS", "awarded"),
                               ("currently pursuing a BS", "either"), ("enrolled in MS", "either"),
                               ("BS", "unknown")):
        assert parse_atoms(phrase, "req-01")[0].attainment == attainment
    for level, rank in (("associate", 1), ("bachelor", 2), ("master", 3), ("doctorate", 4)):
        result = evidence(base, posting(f"{level} degree required"), "2026-09")
        atom = result.spans[0].atoms[0]
        assert all({"associate": 1, "bachelor": 2, "master": 3, "doctorate": 4}[base.education[int(key[-2:]) - 1].degree_level] >= rank
                   for key in (*atom.awarded_at_or_above, *atom.in_progress_at_or_above))
    draft = replace(base, education=(replace(base.education[0], fact_status="draft"),))
    assert not evidence(draft, posting("BS required"), "2026-09").spans[0].atoms[0].awarded_at_or_above


def test_expression_tree_and_modality_boundary() -> None:
    assert posting("BS or MS").spans[0].tree is not None
    assert posting("(BS and 4 years) or (MS and 2 years) or 8 years").spans[0].tree is not None
    assert posting("BS, MS or PhD").spans[0].tree is not None
    assert posting("BS, four years of experience, and certification").spans[0].tree is None
    mixed = posting("BS required and MS preferred").spans[0]
    assert mixed.tree is None
    assert [atom.modality_hint for atom in mixed.atoms] == ["required", "preferred"]


def test_authorization_recency_gpa_and_availability(base) -> None:
    clauses = ["US citizenship required", "permanent residency required", "security clearance required",
               "export-controlled role required", "work authorization required", "no sponsorship available",
               "sponsorship available required"]
    result = evidence(base, posting(". ".join(clauses)), "2026-09")
    assert {flag.kind for flag in result.auth_flags} >= {
        "citizenship_required", "permanent_residency_required", "clearance_required",
        "export_control", "authorization_required", "no_sponsorship", "sponsorship_available",
    }
    recency = ["recent graduate required", "graduated within 2 years required", "within the last 3 years required",
               "class of 2026 required", "graduating in 2027 required"]
    gpa = ["GPA 3.0 required", "grade point average required"]
    for clause in recency + gpa:
        item = evidence(base, posting(clause), "2026-09")
        assert (item.recency_clause is not None) if clause in recency else (item.gpa_clause is not None)
    for month in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05"):
        item = evidence(base, posting(f"Must start in {month}. Must graduate by {month}."), "2026-09")
        assert item.start_date_clauses and item.graduation_clauses


def test_quote_index_and_free_atoms(base) -> None:
    canonical = canonical_posting({"key": "x", "description_html": "<h2>Requirements</h2><p>BS required.</p><p>BS required.</p>"}, {})
    result = evidence(base, canonical, "2026-09")
    assert len(result.locate("BS required.")) == 2
    assert result.locate_in_span("BS required.", "req-02") == (0, len("BS required."))
    quote = "BS and 4 years experience"
    atoms = result.parse_free(quote)
    digest = hashlib.sha256(quote.encode()).hexdigest()[:8]
    assert [atom.atom_id for atom in atoms] == [f"free.{digest}.a1", f"free.{digest}.a2"]
    assert result.parse_free(quote) == atoms
    assert result.locate_in_span("missing", "req-01") is None
    assert not {"satisfied", "unsatisfied", "status", "comparison"} & {field.name for field in fields(Evidence)}


def test_reviewed_compiler_field_overlap_and_public_free_degree(base: CompiledProfile) -> None:
    from venator.qualify.engine import enrich_degree
    assert base.education[0].field_of_study == 'Chemistry'
    atom = evidence(base, posting('BS in Chemistry required'), '2026-09').spans[0].atoms[0]
    assert ('edu-01', 'chemistry') in atom.field_overlap
    free = enrich_degree(parse_atoms('BS in Chemistry', 'free.12345678')[0], base)
    assert free.awarded_at_or_above == atom.awarded_at_or_above
    assert free.field_overlap == atom.field_overlap


def test_free_offsets_are_quote_relative(base: CompiledProfile) -> None:
    quote = 'BS and 4 years experience'
    ev = evidence(base, canonical_posting({'key': 'free-repeat', 'description_html':
        f'<p>{quote}</p><p>{quote}</p>'}, {}), '2026-09')
    assert len(ev.locate(quote)) == 2
    for atom in ev.parse_free(quote):
        assert quote[atom.start:atom.end] == atom.quote


@pytest.mark.parametrize('required,required_rank', [
    ('high school', 0), ('associate', 1), ('BS', 2), ('MS', 3), ('PhD', 4),
])
@pytest.mark.parametrize('held,held_rank', [
    ('high_school', 0), ('associate', 1), ('bachelor', 2), ('master', 3), ('doctorate', 4),
])
def test_every_degree_lattice_pair(base: CompiledProfile, required: str, required_rank: int, held: str, held_rank: int) -> None:
    compiled = replace(base, education=(replace(base.education[0], degree_level=held, status='awarded'),))
    atom = evidence(compiled, posting(required + ' required'), '2026-09').spans[0].atoms[0]
    assert atom.awarded_at_or_above == (('edu-01',) if held_rank >= required_rank else ())


@pytest.mark.parametrize('phrase,attainment', [
    ('completed BS', 'awarded'), ('conferred BS', 'awarded'), ('earned BS', 'awarded'),
    ('hold BS', 'awarded'), ('holds BS', 'awarded'), ('BS degree in hand', 'awarded'),
    ('pursuing BS', 'either'), ('enrolled BS', 'either'), ('working toward BS', 'either'),
    ('candidate BS', 'either'), ('current student BS', 'either'), ('BS in progress', 'either'),
])
def test_every_attainment_cue(phrase: str, attainment: str) -> None:
    assert parse_atoms(phrase, 'req-01')[0].attainment == attainment


def test_degree_fields_and_attainment_do_not_cross_atoms() -> None:
    atoms = parse_atoms('BS required and MS in Chemistry preferred', 'req-01')
    assert [a.field_words for a in atoms] == [(), ('chemistry',)]
    atoms = parse_atoms('earned BS or pursuing MS', 'req-01')
    assert [a.attainment for a in atoms] == ['awarded', 'either']
    assert parse_atoms('BS degree in hand', 'req-01')[0].field_words == ()
    atoms = parse_atoms('BS plus 5 years required', 'req-01')
    assert [a.modality_hint for a in atoms] == ['unknown', 'required']
    assert posting('BS; MS or PhD').spans[0].tree.op == 'OR'
    assert posting('BS; MS').spans[0].tree is None


@pytest.mark.parametrize('clause,recency,gpa', [
    ('Graduated within two years', True, False), ('Recent graduate', True, False),
    ('Within the last 3 years', True, False), ('Class of 2026', True, False),
    ('Graduating in 2027', True, False), ('GPA 3.0', False, True),
    ('Grade point average 3.0', False, True), ('Minimum GPA of 3.5', False, True),
    ('A grade point score', False, True), ('Cumulative GPA', False, True),
])
def test_ten_e7_clauses(base: CompiledProfile, clause: str, recency: bool, gpa: bool) -> None:
    ev = evidence(base, posting(clause), '2026-09')
    assert (ev.recency_clause is not None) is recency
    assert (ev.gpa_clause is not None) is gpa


@pytest.mark.parametrize('clause,kind,expected', [
    ('Must start in 2026-08', 'start', 'incompatible'),
    ('Must start in 2026-09', 'start', 'compatible'),
    ('Must start in 2026-10', 'start', 'compatible'),
    ('Must start in 2027-01', 'start', 'incompatible'),
    ('Must start on 2026-09-14', 'start', 'unknown'),
    ('Must graduate by 2027-04', 'graduation', 'incompatible'),
    ('Must graduate by 2027-05', 'graduation', 'compatible'),
    ('Must graduate by 2027-06', 'graduation', 'compatible'),
    ('Must graduate by May 2027', 'graduation', 'compatible'),
    ('Must graduate by 2027-05-14', 'graduation', 'unknown'),
])
def test_ten_e8_comparisons(base: CompiledProfile, clause: str, kind: str, expected: str) -> None:
    compiled = replace(base, availability=replace(base.availability, fact_status='confirmed',
        available_from='2026-09', available_until='2026-12', expected_graduation='2027-05',
        expected_graduation_confirmed=True))
    ev = evidence(compiled, posting(clause), '2026-09')
    values = ev.start_date_clauses if kind == 'start' else ev.graduation_clauses
    assert len(values) == 1 and values[0].comparison == expected


@pytest.mark.parametrize('alias,expected', [
    ('GED', 'high_school'), ('high school diploma', 'high_school'),
    ('AA', 'associate'), ('A.A.S.', 'associate'), ("associate's degree", 'associate'),
    ('BS', 'bachelor'), ('B.Sc.', 'bachelor'), ('BA', 'bachelor'), ('BEng', 'bachelor'),
    ("bachelor's degree", 'bachelor'), ('MS', 'master'), ('M.Sc.', 'master'),
    ('MA', 'master'), ('MBA', 'master'), ('MPH', 'master'), ("master's degree", 'master'),
    ('Ph.D.', 'doctorate'), ('DPhil', 'doctorate'), ('PharmD', 'doctorate'),
    ('MD', 'doctorate'), ('JD', 'doctorate'), ('doctoral degree', 'doctorate'),
])
def test_degree_aliases(alias: str, expected: str) -> None:
    atoms = parse_atoms(alias, 'req-01')
    assert len(atoms) == 1 and atoms[0].required_level == expected
