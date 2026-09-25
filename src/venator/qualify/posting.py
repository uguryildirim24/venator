"""Canonical Posting text and deliberately conservative requirement structure."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Literal, Mapping

from venator.discover.store import posting_revision
from venator.match.assessment import (
    _CREDENTIAL_CUE, _ELIGIBILITY_CUE, _HTMLText, _REQ_HEADING,
    _SECTION_BREAK, _WORK_HEADING, _OTHER_HEADING, _is_employer_technical_sentence,
)

TEXT_VERSION = "1"
POSTING_CHAR_CAP = 48_000
_MANDATORY = re.compile(r"\b(?:required|must|minimum|need(?:s)?|at least)\b", re.I)
_NEGATED = re.compile(r"\b(?:not required|no\b.{0,40}?\brequired|need not)\b", re.I)
_PREFERRED = re.compile(r"\b(?:preferred|desired|nice to have|bonus|plus|optional)\b", re.I)
_YEARS = re.compile(
    r"\b(?P<low>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
    r"(?:\s*(?:-|–|—|to)\s*(?P<high>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty))?"
    r"\s*\+?\s*years?\b", re.I,
)
_NUMBER = {word: index for index, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split()
)}
_DEGREE = re.compile(
    r"(?<!\w)(?P<degree>high school(?: diploma)?|GED|associate(?:'s)?(?: degree)?|A\.?A\.?S?\.?|"
    r"bachelor(?:'s)?(?: degree)?|B\.?S\.?c?\.?|B\.?A\.?|BEng|"
    r"master(?:'s)?(?: degree)?|M\.?S\.?c?\.?|M\.?A\.?|MBA|MPH|"
    r"doctorate|doctoral(?: degree)?|Ph\.?D\.?|DPhil|PharmD|MD|JD)(?!\w)", re.I,
)
_FIELD = re.compile(r"\b(?:in|of)\s+([A-Za-z][A-Za-z /&-]{1,90})", re.I)
_FIELD_STOP = re.compile(r"\b(?:required|preferred|or|and|with|plus|experience|degree|years?|certification|license)\b", re.I)
_ATTAIN_AWARDED = re.compile(r"\b(?:completed|conferred|earned|hold|holds|degree in hand)\b", re.I)
_ATTAIN_EITHER = re.compile(r"\b(?:pursuing|enrolled|working toward|candidate|current student|in progress)\b", re.I)
_DEGREE_BOUNDARY = re.compile(r"[;()]|\b(?:and|plus)\b|\bor\b(?!\s+equivalent\b)", re.I)
_CONNECTOR = re.compile(r"\b(?:and|or|with|plus|either)\b|[/+(),;]", re.I)
_WORD = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class Atom:
    atom_id: str
    kind: Literal["years", "degree", "credential"]
    start: int
    end: int
    quote: str
    years_min: int | None = None
    years_max: int | None = None
    scope_text: str = ""
    high_years: bool = False
    required_level: str | None = None
    attainment: Literal["awarded", "either", "unknown"] = "unknown"
    field_words: tuple[str, ...] = ()
    or_equivalent: bool = False
    awarded_at_or_above: tuple[str, ...] = ()
    in_progress_at_or_above: tuple[str, ...] = ()
    field_overlap: tuple[tuple[str, str], ...] = ()
    modality_hint: Literal["required", "preferred", "unknown"] = "unknown"


@dataclass(frozen=True)
class Expr:
    op: Literal["AND", "OR"]
    children: tuple[Expr | str, ...]


@dataclass(frozen=True)
class Span:
    id: str
    text: str
    start: int
    end: int
    heading: str | None
    modality_hint: Literal["required", "preferred", "unknown"]
    cues: tuple[str, ...]
    atoms: tuple[Atom, ...]
    tree: Expr | None


@dataclass(frozen=True)
class CanonicalPosting:
    posting_key: str
    posting_group: str
    posting_revision: str
    title: str
    description_text: str
    spans: tuple[Span, ...]
    too_long: bool
    too_many_spans: bool
    description_kind: str
    listing_status: str
    opportunity_type: str
    workplace_type: str | None
    location: str | None


def _number(value: str) -> int:
    return int(value) if value.isdigit() else _NUMBER[value.casefold()]


def _modality(text: str, heading: str | None) -> tuple[str, tuple[str, ...]]:
    negated = tuple(match.group(0) for match in _NEGATED.finditer(text))
    clean = _NEGATED.sub(" ", text)
    mandatory = tuple(match.group(0) for match in _MANDATORY.finditer(clean))
    preferred = tuple(match.group(0) for match in _PREFERRED.finditer(text))
    heading_required = bool(heading and re.search(r"\b(?:basic|required|minimum|essential)\b", heading, re.I))
    heading_preferred = bool(heading and _PREFERRED.search(heading))
    if mandatory:
        hint = "required"
    elif preferred or negated or heading_preferred:
        hint = "preferred"
    elif heading_required:
        hint = "required"
    else:
        hint = "unknown"
    return hint, (*mandatory, *preferred, *negated)


def parse_atoms(text: str, prefix: str, *, base: int = 0) -> tuple[Atom, ...]:
    """Extract numerical, degree and credential atoms with exact offsets."""
    found: list[Atom] = []
    for match in _YEARS.finditer(text):
        low = _number(match.group("low"))
        high = _number(match.group("high")) if match.group("high") else None
        tail = text[match.end():]
        scope = re.match(r"\s*(?:of|in|with)\s+([^.;,()]{1,90})", tail, re.I)
        scope_text = scope.group(0).strip() if scope else ""
        found.append(Atom("", "years", base + match.start(), base + match.end(), match.group(0),
                          years_min=low, years_max=high, scope_text=scope_text, high_years=low >= 15))
    for match in _DEGREE.finditer(text):
        raw = re.sub(r"[^a-z]", "", match.group("degree").casefold())
        if raw.startswith(("highschool", "ged")):
            level = "high_school"
        elif raw.startswith(("associate", "aa")):
            level = "associate"
        elif raw.startswith(("bachelor", "bs", "ba", "beng")):
            level = "bachelor"
        elif raw.startswith(("master", "ms", "ma", "mba", "mph")):
            level = "master"
        else:
            level = "doctorate"
        before = list(_DEGREE_BOUNDARY.finditer(text, 0, match.start()))
        after = _DEGREE_BOUNDARY.search(text, match.end())
        begin = before[-1].end() if before else 0
        end = after.start() if after else len(text)
        context = text[begin:end]
        attainment = "awarded" if _ATTAIN_AWARDED.search(context) else "either" if _ATTAIN_EITHER.search(context) else "unknown"
        field_match = _FIELD.match(text[match.end():end].lstrip())
        field_text = _FIELD_STOP.split(field_match.group(1), maxsplit=1)[0] if field_match else ""
        fields = tuple(word.casefold() for word in _WORD.findall(field_text) if word.casefold() not in {"a", "an", "the", "related"})
        found.append(Atom("", "degree", base + match.start(), base + match.end(), match.group(0),
                          required_level=level, attainment=attainment, field_words=fields,
                          or_equivalent=bool(re.search(r"\bor equivalent\b", context, re.I))))
    for match in _CREDENTIAL_CUE.finditer(text):
        if re.search(r"citizenship|persons?|authori[sz]ation|export.control", match.group(0), re.I):
            continue
        found.append(Atom("", "credential", base + match.start(), base + match.end(), match.group(0)))
    found.sort(key=lambda atom: (atom.start, atom.end, atom.kind))
    unique: list[Atom] = []
    for atom in found:
        if any(atom.start < prior.end and prior.start < atom.end for prior in unique):
            continue
        local_start = atom.end - base
        next_start = next((other.start - base for other in found if other.start >= atom.end), len(text))
        local = text[local_start:next_start]
        if re.fullmatch(r"\s*plus\s*", local, re.I) and next_start < len(text):
            local = ""
        hint, _ = _modality(local, None)
        unique.append(replace(atom, atom_id=f"{prefix}.a{len(unique) + 1}", modality_hint=hint))
    return tuple(unique)


def _tree(text: str, atoms: tuple[Atom, ...], base: int) -> Expr | None:
    if len(atoms) < 2:
        return None
    # Explicitly different modality cues on either side of a connector make
    # one root unsafe. The downstream judge must split the obligations.
    cue_positions = [(m.start(), "required") for m in _MANDATORY.finditer(_NEGATED.sub(" ", text))]
    # Between atoms, plus is an AND connector, not an optionality cue.
    cue_positions += [(m.start(), "preferred") for m in _PREFERRED.finditer(text)
                      if m.group(0).casefold() != "plus" or not any(
                          left.end - base <= m.start() < m.end() <= right.start - base
                          for left, right in zip(atoms, atoms[1:]))]
    if len({cue for _, cue in cue_positions}) > 1:
        return None
    tokens: list[str] = []
    cursor = 0
    for atom in atoms:
        start = atom.start - base
        gap = text[cursor:start]
        tokens.extend(m.group(0).upper() for m in _CONNECTOR.finditer(gap))
        tokens.append(atom.atom_id)
        cursor = atom.end - base
    tokens.extend(m.group(0).upper() for m in _CONNECTOR.finditer(text[cursor:]))
    explicit = {token for token in tokens if token in {"AND", "OR"}}
    if any(token in {",", ";"} for token in tokens):
        if len(explicit) != 1:
            return None
        tokens = [next(iter(explicit)) if token in {",", ";"} else token for token in tokens]
    tokens = ["AND" if token in {"WITH", "PLUS", "+"} else "OR" if token == "/" else token
              for token in tokens if token != "EITHER"]
    # A small recursive descent grammar gives AND precedence over OR.
    position = 0

    def primary() -> Expr | str | None:
        nonlocal position
        if position >= len(tokens):
            return None
        token = tokens[position]
        position += 1
        if token == "(":
            node = disjunction()
            if position >= len(tokens) or tokens[position] != ")":
                return None
            position += 1
            return node
        return token if token.startswith(("req-", "free.")) else None

    def conjunction() -> Expr | str | None:
        nonlocal position
        left = primary()
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            right = primary()
            if left is None or right is None:
                return None
            left = Expr("AND", (*left.children, right)) if isinstance(left, Expr) and left.op == "AND" else Expr("AND", (left, right))
        return left

    def disjunction() -> Expr | str | None:
        nonlocal position
        left = conjunction()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            right = conjunction()
            if left is None or right is None:
                return None
            left = Expr("OR", (*left.children, right)) if isinstance(left, Expr) and left.op == "OR" else Expr("OR", (left, right))
        return left

    node = disjunction()
    return node if position == len(tokens) and isinstance(node, Expr) else None


def _spans(description: str, html_headings: frozenset[str] = frozenset()) -> tuple[Span, ...]:
    spans: list[Span] = []
    heading: str | None = None
    in_requirements = False
    in_work = False
    offset = 0
    for raw in description.splitlines(keepends=True):
        line = raw.rstrip("\n")
        stripped = line.strip()
        label = stripped.rstrip(":")
        pieces = re.split(r"\s*(?:/|&|\band\b)\s*", label, flags=re.I)
        is_req = bool(pieces) and all(_REQ_HEADING.fullmatch(piece) for piece in pieces)
        is_work = bool(_WORK_HEADING.fullmatch(label) or re.fullmatch(
            r"(?:primary|core|job|position|additional)\s+(?:responsibilities|duties)", label, re.I))
        is_heading = (stripped == _SECTION_BREAK or is_req or is_work or
                      bool(_OTHER_HEADING.fullmatch(label)) or stripped in html_headings)
        if is_heading:
            heading = label if stripped != _SECTION_BREAK else None
            in_requirements = is_req or bool(_PREFERRED.search(label)) and stripped.endswith(":")
            in_work = is_work
            offset += len(raw)
            continue
        clean = _NEGATED.sub(" ", stripped)
        cue = _MANDATORY.search(clean) or _ELIGIBILITY_CUE.search(stripped)
        if stripped and not in_work and (in_requirements or cue) and not _is_employer_technical_sentence(stripped):
            starts = [(0, len(line))]
            if len(line) > 400:
                starts = []
                begin = 0
                for match in re.finditer(r"(?<=[.!?])\s+", line):
                    if match.end() - begin >= 250:
                        starts.append((begin, match.start()))
                        begin = match.end()
                starts.append((begin, len(line)))
            for begin, end in starts:
                while begin < end and line[begin].isspace():
                    begin += 1
                while end > begin and line[end - 1].isspace():
                    end -= 1
                if begin == end:
                    continue
                part = line[begin:end]
                span_start = offset + begin
                atoms = parse_atoms(part, f"req-{len(spans) + 1:02d}", base=span_start)
                hint, cues = _modality(part, heading)
                spans.append(Span(f"req-{len(spans) + 1:02d}", part, span_start, offset + end,
                                  heading, hint, cues, atoms, _tree(part, atoms, span_start)))
        offset += len(raw)
    return tuple(spans)


class _PostingText(_HTMLText):
    """Keep the shared reader's bytes and retain actual HTML heading labels."""

    def __init__(self) -> None:
        super().__init__()
        self.headings: set[str] = set()
        self.heading_start: int | None = None
        self.emphasis_starts: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and not self.ignored:
            self.heading_start = len(self.parts)
        if tag in {"b", "strong"} and not self.ignored:
            self.emphasis_starts.append(len(self.parts))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"b", "strong"} and self.emphasis_starts:
            begin = self.emphasis_starts.pop()
            label = " ".join(unicodedata.normalize("NFKC", "".join(self.parts[begin:])).split())
            # ATS section labels commonly use a bold paragraph, not h2.
            # Exact full-line matching below excludes inline emphasis.
            if (0 < len(label) <= 85 and label.endswith(":") and
                    not re.fullmatch(r"(?:note|notes|important|example|examples):", label, re.I) and
                    not parse_atoms(label, "heading")):
                self.headings.add(label)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading_start is not None:
            label = " ".join(unicodedata.normalize("NFKC", "".join(self.parts[self.heading_start:])).split())
            self.headings.add(label + ":")
            self.heading_start = None
        super().handle_endtag(tag)


def _group(key: str, group_index: Mapping[str, object]) -> str:
    for field in ("posting_to_group", "by_posting", "posting_groups", "index"):
        child = group_index.get(field)
        if isinstance(child, Mapping) and key in child:
            return str(child[key])
    value = group_index.get(key)
    if isinstance(value, str):
        return value
    groups = group_index.get("groups")
    if isinstance(groups, Mapping):
        for group, members in groups.items():
            if isinstance(members, (list, tuple, set)) and key in members:
                return str(group)
    return key


def canonical_posting(posting: Mapping[str, object], group_index: Mapping[str, object]) -> CanonicalPosting:
    """Normalize one Posting; an empty frozen index makes singleton groups."""
    if not isinstance(group_index, Mapping):
        raise TypeError("group_index must be a mapping")
    key = str(posting.get("key") or "")
    if not key:
        raise ValueError("Posting key is required")
    raw = posting.get("description_html")
    parser = _PostingText()
    if isinstance(raw, str):
        parser.feed(html.unescape(raw))
        parser.close()
    normalized = unicodedata.normalize("NFKC", "".join(parser.parts))
    description = "\n".join(" ".join(line.split()) for line in normalized.splitlines() if line.strip())
    spans = _spans(description, frozenset(parser.headings))
    kind = str(posting.get("description_kind") or ("full" if description else "missing"))
    return CanonicalPosting(key, _group(key, group_index), posting_revision(posting),
                            str(posting.get("title") or ""), description, spans,
                            len(description) > POSTING_CHAR_CAP,
                            len(spans) > 40 or sum(len(span.atoms) for span in spans) > 100,
                            kind, str(posting.get("listing_status") or "unknown"),
                            str(posting.get("opportunity_type") or "unknown"),
                            str(posting.get("workplace_type")) if posting.get("workplace_type") is not None else None,
                            str(posting.get("location")) if posting.get("location") is not None else None)


def free_atoms(quote: str) -> tuple[Atom, ...]:
    digest = hashlib.sha256(quote.encode("utf-8")).hexdigest()[:8]
    return parse_atoms(quote, f"free.{digest}")
