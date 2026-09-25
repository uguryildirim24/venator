"""Deterministic evidence assessment for one job Posting.

This is a transparent baseline, not a scorer. It reports exact overlaps and
labelled broader relationships with confirmed ``Profile.resume`` facts, carries
source lifecycle uncertainty forward, and respects a hard-filter decision
supplied by the caller.  It never runs the filters, calls a model, or
fabricates a probability.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

from venator.match.relations import broader_matches, fact_match_is_assertive
from venator.profile import Profile
from venator.profile.facts import confirmed

AssessmentStatus = Literal["suitable", "needs_review", "not_suitable", "unassessed"]

_CHOICES = {
    "listing_status": frozenset({"open", "closed", "unknown"}),
    "description_kind": frozenset({"full", "snippet", "missing"}),
    "opportunity_type": frozenset({"job", "talent_pool", "program", "unknown"}),
}
_UNKNOWN_FACTS = frozenset({
    "unassessed", "missing", "ambiguous", "unknown", "remote_unnamed",
    "unplaced", "degree_unplaced", "degree_timing", "enroll_timing",
    "salary_currency", "salary_period", "noted",
})
_STOPWORDS = frozenset(
    "a an and as at be by for from in of on or the to with degree degrees field fields".split()
)
_DEGREE_WORDS = frozenset({"associate", "bachelor", "master", "phd", "doctor", "doctorate"})
_DEGREE_RE = re.compile(
    r"\b(?:ph\.?d\.?|doctorate|doctor|master(?:'s)?|m\.?s\.?|"
    r"bachelor(?:'s)?|b\.?s\.?|associate(?:'s)?|a\.?s\.?)\b",
    re.IGNORECASE,
)
_REQ_CUE = re.compile(
    r"\b(?:required|must|minimum|preferred|proficien(?:cy|t)|"
    r"knowledge (?:of|in)|"
    r"ability to|you have|coursework|qualifications?)\b",
    re.IGNORECASE,
)
_REQ_HEADING = re.compile(
    r"^(?:(?:basic|required|minimum|essential|preferred|desired)\s+)?(?:requirements?|qualifications?|skills?|education|"
    r"experience|licen[cs]es?|certifications?|what you(?:'ll| will) bring|what we(?:'re| are) looking for|"
    r"who you are|about you|what you(?:'ll| will) need(?: to succeed)?)\s*:??$",
    re.IGNORECASE,
)
_PREFERRED = re.compile(r"\b(?:preferred|desired|nice to have|bonus|not required|optional)\b", re.I)
_MANDATORY = re.compile(r"(?<!not )\brequired\b|\bmust\b", re.I)
_ELIGIBILITY_CUE = re.compile(
    r"\bemployment\b[^.]{0,60}\bcontingent\b|\bonly\b.{0,200}\beligible for (?:this|the) (?:position|role)\b",
    re.I,
)
_SECTION_BREAK = "[section break]"
_WORK_HEADING = re.compile(
    r"^(?:(?:key|main|your|essential)\s+)?(?:responsibilities|duties|"
    r"what you(?:'ll| will) (?:do|be (?:doing|building))|your (?:role|impact))\s*$", re.I,
)
_OTHER_HEADING = re.compile(r"^(?:about (?:the role|us|the company)|position summary|pay (?:and|&) benefits|privacy notice)$", re.I)
_CREDENTIAL_CUE = re.compile(
    r"\b(?:certificat(?:e|ion)|licen[cs]e|security clearance|export.control|"
    r"U\.?S\.? persons?|citizenship|work authori[sz]ation)\b", re.I,
)
_GENERIC_OVERLAP = frozenset({
    "intern", "internship", "assistant", "associate", "entry level",
    "hands-on", "full-time", "part-time", "e.g", "i.e",
})
_SUPPORTING_TOOLS = frozenset({
    "excel", "microsoft excel", "ms excel", "microsoft office", "ms office", "office",
    "office 365", "microsoft 365", "m365", "ms 365", "word", "microsoft word", "ms word",
    "powerpoint", "microsoft powerpoint", "ms powerpoint", "power point", "microsoft power point",
    "google workspace", "workspace", "google docs", "google sheets", "spreadsheets",
})
_TECHNICAL_TERMS = frozenset(
    {
        "airflow",
        "aws",
        "azure",
        "bash",
        "biochemistry",
        "bioinformatics",
        "c#",
        "c++",
        "canva",
        "docker",
        "excel",
        "figma",
        "gc-ms",
        "git",
        "github",
        "java",
        "javascript",
        "kubernetes",
        "matlab",
        "mcp",
        "mongodb",
        "nmr",
        "numpy",
        "pandas",
        "postgresql",
        "python",
        "r",
        "react",
        "rest",
        "rust",
        "scikit-learn",
        "sql",
        "tableau",
        "terraform",
        "typescript",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*")
_EMPLOYER_TECHNICAL_SENTENCE = re.compile(
    r"^(?:our\s+(?:company|team|platform|product|organization|mission)|"
    r"we\b|the\s+(?:company|team|platform|product|organization)\b)\b.*"
    r"\b(?:use|uses|build|builds|built|develop|develops|deploy|deploys|operate|operates|"
    r"run|runs|power|powers|rely|relies|focus|focuses|focused|requires?)\b",
    re.IGNORECASE,
)
_CANDIDATE_REQUIREMENT_CUE = re.compile(
    r"\b(?:candidate|applicant|you|experience|skills?|proficien(?:cy|t)|knowledge|"
    r"ability|qualifications?|familiarity|coursework|certificat(?:e|ion)|license|"
    r"clearance)\b",
    re.IGNORECASE,
)


def _normalize(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split()) if isinstance(value, str) else ""


class _HTMLText(HTMLParser):
    """Small HTML to text reader that retains heading/list boundaries."""

    _BLOCKS = frozenset(
        "br div h1 h2 h3 h4 h5 h6 li ol p section table td th tr ul".split()
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        elif not self.ignored and tag == "hr":
            self.parts.append(f"\n{_SECTION_BREAK}\n")
        elif not self.ignored and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.ignored = max(0, self.ignored - 1)
        elif not self.ignored and tag in self._BLOCKS:
            self.parts.append(":\n" if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} else "\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def _plain(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parser = _HTMLText()
    try:
        parser.feed(html.unescape(value))
        parser.close()
        value = "".join(parser.parts)
    except (TypeError, ValueError):
        value = html.unescape(value)
    return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip()).strip()


def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _is_employer_technical_sentence(value: str) -> bool:
    """Whether a sentence describes the employer's stack rather than the job.

    ATS descriptions occasionally place a company or platform sentence inside
    a qualifications block. Its technology words are context, not candidate
    evidence. Keep sentences that explicitly address candidates or name a
    qualification, even when they use first-person employer wording.
    """
    return bool(_EMPLOYER_TECHNICAL_SENTENCE.search(value)) and not bool(_CANDIDATE_REQUIREMENT_CUE.search(value))


def _field(posting: Mapping[str, object], name: str) -> object:
    value = posting.get(name)
    if value is not None:
        return value
    for container_name in ("source_metadata", "source_meta", "source_info", "verification"):
        container = posting.get(container_name)
        if isinstance(container, Mapping) and name in container:
            return container[name]
    source = posting.get("source")
    return source.get(name) if isinstance(source, Mapping) else None


def _choice(posting: Mapping[str, object], name: str) -> str:
    value = _field(posting, name)
    if isinstance(value, str):
        value = value.strip().casefold().replace("-", "_")
        if value in _CHOICES[name]:
            return value
    return "unknown"


def _description_kind(posting: Mapping[str, object], description: str) -> str:
    value = _choice(posting, "description_kind")
    if value != "unknown":
        return value
    if bool(posting.get("snippet")):
        return "snippet"
    return "full" if description else "missing"


def _source(posting: Mapping[str, object]) -> str:
    value = posting.get("source")
    return value.strip() if isinstance(value, str) and value.strip() else "posting source"


def _source_field(posting: Mapping[str, object], field: str) -> str:
    return f"{_source(posting)} {field}"


@dataclass(frozen=True)
class _Fact:
    value: str
    source: str
    kind: str


def _items(value: object) -> list[str]:
    if not confirmed(value):
        return []
    if isinstance(value, Mapping):
        return _items(value.get("text") or value.get("items") or value.get("name"))
    if isinstance(value, str):
        return [part.strip(" .") for part in re.split(r"[,;\n]|\s+\|\s+", value) if part.strip(" .")]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result: list[str] = []
        for item in value:
            result.extend(_items(item))
        return result
    return []


def _add(facts: list[_Fact], value: object, source: str, kind: str) -> None:
    if not confirmed(value):
        return
    if isinstance(value, Mapping):
        value = value.get("text") or value.get("value")
    value = _text(value).strip(" .")
    if (len(value) >= 2 or value == "R") and len(value) <= 1_000:
        facts.append(_Fact(value, source, kind))


def _labelled(facts: list[_Fact], value: object, source: str, kind: str = "skill") -> None:
    for index, item in enumerate(_items(value)):
        _add(facts, item, f"{source}[{index}]", kind)


def _resume_facts(resume: Mapping[str, object]) -> list[_Fact]:
    facts: list[_Fact] = []
    if not confirmed(resume):
        return facts
    for key in ("technical_proficiencies", "skills", "technical_skills", "tools", "certifications"):
        value = resume.get(key)
        kind = "credential" if key == "certifications" else "skill"
        if not confirmed(value):
            continue
        if isinstance(value, Mapping):
            for label, items in value.items():
                _labelled(facts, items, f"profile.resume.{key}.{label}", kind)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for index, row in enumerate(value):
                source = f"profile.resume.{key}[{index}]"
                if not confirmed(row):
                    continue
                if isinstance(row, Mapping):
                    _labelled(facts, row.get("items") or row.get("skills") or row.get("technologies") or row.get("name"), f"{source}.items", kind)
                else:
                    _labelled(facts, row, source, kind)
        else:
            _labelled(facts, value, f"profile.resume.{key}", kind)

    if resume.get("coursework") is not None:
        _labelled(facts, resume.get("coursework"), "profile.resume.coursework", "coursework")
    education = resume.get("education")
    if isinstance(education, Sequence) and not isinstance(education, (str, bytes, bytearray)):
        for index, row in enumerate(education):
            if not isinstance(row, Mapping) or not confirmed(row):
                continue
            source = f"profile.resume.education[{index}]"
            degree = row.get("degree")
            date = _text(row.get("date") or row.get("dates"))
            if isinstance(degree, str) and re.search(r"expected|in.progress|pursuing", date, re.I):
                degree = f"{degree} ({date})"
            _add(facts, degree, f"{source}.degree", "degree")
            _labelled(facts, row.get("coursework"), f"{source}.coursework", "coursework")

    for key, kind in (("experience", "experience"), ("projects", "project"), ("project", "project"), ("work", "experience")):
        value = resume.get(key)
        if isinstance(value, Mapping):
            rows = list(value.values())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            rows = list(value)
        else:
            rows = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping) or not confirmed(row):
                continue
            source = f"profile.resume.{key}[{index}]"
            fact_kind = "role" if kind == "experience" else "project"
            fact_field = "role" if kind == "experience" else "name"
            _add(facts, row.get("role") or row.get("title") or row.get("name"), f"{source}.{fact_field}", fact_kind)
            for field in ("skills", "tools", "technologies", "technology"):
                if row.get(field) is not None:
                    _labelled(facts, row[field], f"{source}.{field}")
            bullets = row.get("bullets") or row.get("description") or row.get("summary")
            if isinstance(bullets, Sequence) and not isinstance(bullets, (str, bytes, bytearray)):
                for bullet_index, bullet in enumerate(bullets):
                    _add(facts, bullet, f"{source}.bullets[{bullet_index}]", kind)
            elif bullets:
                _add(facts, bullets, f"{source}.description", kind)

    result: list[_Fact] = []
    seen: set[tuple[str, str, str]] = set()
    for fact in facts:
        marker = (fact.value.casefold(), fact.source, fact.kind)
        if marker not in seen:
            seen.add(marker)
            result.append(fact)
    return result


def _variants(fact: _Fact) -> list[str]:
    variants: list[str] = []
    def add(value: str) -> None:
        value = " ".join(value.casefold().split()).strip(" .")
        if (len(value) >= 2 or value == "r") and value not in _STOPWORDS and value not in _GENERIC_OVERLAP and value not in variants:
            variants.append(value)

    add(fact.value)
    words = [word for word in _TOKEN_RE.findall(unicodedata.normalize("NFKC", fact.value).casefold()) if word not in _STOPWORDS]
    if fact.kind in {"skill", "coursework", "degree", "role", "project", "credential"}:
        for width in range(min(4, len(words)), 1, -1):
            for start in range(len(words) - width + 1):
                add(" ".join(words[start : start + width]))
    for word in words:
        # Punctuation does not make a word a technical skill. "high-throughput"
        # inside an observation bullet cannot certify hands-on engineering work.
        if word in _TECHNICAL_TERMS:
            add(word)
    return variants


def _match(text: str, phrase: str, *, start: int = 0) -> re.Match[str] | None:
    words = phrase.casefold().split()
    if not words:
        return None
    body = r"\s+".join(re.escape(word) for word in words)
    pattern = re.compile(rf"(?<![A-Za-z0-9+#]){body}(?![A-Za-z0-9+#])", re.IGNORECASE)
    for found in pattern.finditer(text, start):
        if phrase.casefold() == "excel" and re.search(r"\b(?:you|who|to|can|will|must|we|they)\s+(?:will\s+)?$", text[:found.start()], re.I):
            continue
        if phrase.casefold() == "r" and (found.group(0) != "R" or re.match(r"\s*&\s*D\b", text[found.end():])):
            continue
        if phrase.casefold() == "rest" and re.match(r"\s+of\b", text[found.end():], re.I):
            continue
        return found
    return None


def _assertive_fact_match(fact: _Fact, phrase: str) -> re.Match[str] | None:
    """Find a phrase in a fact only when the local clause asserts it."""
    offset = 0
    while (found := _match(fact.value, phrase, start=offset)) is not None:
        if fact_match_is_assertive(fact.value, found):
            return found
        # Every accepted _match has non-zero width, so this always advances.
        offset = found.end()
    return None


def _contexts(posting: Mapping[str, object], description: str) -> list[tuple[str, str]]:
    contexts: list[tuple[str, str]] = []
    title = _text(posting.get("title"))
    if title:
        contexts.append((title, "posting.title"))
    for field in ("requirements", "qualifications", "required_skills", "skills"):
        value = _field(posting, field)
        for index, item in enumerate(_items(value)):
            contexts.append((item, f"posting.{field}[{index}]"))

    collecting = False
    work = False
    preferred = False
    section_seen = False
    for line in description.splitlines():
        short = line.strip()
        if not short:
            continue
        prefix, separator, remainder = short.partition(":")
        label = prefix if separator and len(prefix) < 100 else short.rstrip(":")
        qualification_label = label
        parenthetical = re.search(r"\(([^()]*)\)\s*$", label)
        if parenthetical and _REQ_HEADING.fullmatch(parenthetical.group(1)):
            qualification_label = parenthetical.group(1)
        parts = re.split(r"\s*(?:/|&|\band\b)\s*", qualification_label, flags=re.I)
        requirement_heading = bool(parts) and all(_REQ_HEADING.fullmatch(part) for part in parts)
        work_heading = bool(_WORK_HEADING.fullmatch(label))
        # Heading tags already carry a colon. ATS editors also commonly use a
        # standalone bold/title-case line, such as "Privacy Notice".
        words = label.split()
        title_heading = (
            0 < len(words) <= 8 and not re.search(r"[.!?]", label)
            and all(word[:1].isupper() or word.casefold() in _STOPWORDS or not any(char.isalpha() for char in word) for word in words)
        )
        heading = requirement_heading or work_heading or bool(_OTHER_HEADING.fullmatch(label)) or (title_heading and not remainder.strip()) or short.endswith(":") and len(short) < 100
        if short == _SECTION_BREAK or heading:
            section_seen = True
            collecting = requirement_heading
            work = work_heading
            preferred = bool(_PREFERRED.search(label))
            # "Bonus (not required)" is a common heading with no Qualifications
            # suffix. Its content can contribute evidence, never a veto.
            if heading and preferred:
                collecting = True
            if collecting and separator and remainder.strip() and not _is_employer_technical_sentence(remainder.strip()):
                optional = preferred and not _MANDATORY.search(remainder)
                source = "posting.description_html.preferred" if optional else "posting.description_html"
                contexts.append((remainder.strip(), source))
            continue
        if work:
            if _is_employer_technical_sentence(short):
                continue
            contexts.append((short, "posting.description_html.responsibilities"))
            continue
        # After a named non-requirement section, a loose word such as
        # "qualifications" in salary/EOE prose cannot reopen requirements.
        # Explicit obligations still surface, including eligibility restrictions
        # that employers place after their standard policy footer.
        cue = _MANDATORY.search(short) or _ELIGIBILITY_CUE.search(short)
        if collecting or cue or not section_seen and _REQ_CUE.search(short):
            if _is_employer_technical_sentence(short):
                continue
            optional = (preferred or bool(_PREFERRED.search(short))) and not _MANDATORY.search(short)
            source = "posting.description_html.preferred" if optional else "posting.description_html"
            contexts.append((short, source))
    return contexts


def _degree_evidence(contexts: list[tuple[str, str]], facts: list[_Fact]) -> list[dict[str, str]]:
    degrees = [fact for fact in facts if fact.kind == "degree"]
    rows: list[dict[str, str]] = []
    levels = {"associate": 1, "bachelor": 2, "master": 3, "phd": 4, "doctor": 4, "doctorate": 4}

    def level(value: str) -> str | None:
        compact = re.sub(r"[^a-z]", "", value.casefold())
        return next((word for word in levels if word in compact), None)

    for text, _source_name in contexts:
        required = _DEGREE_RE.search(text)
        if not required:
            continue
        required_level = level(required.group(0))
        if required_level is None:
            continue
        for fact in degrees:
            candidate_match = _DEGREE_RE.search(fact.value)
            candidate_level = level(candidate_match.group(0)) if candidate_match else None
            if candidate_level is None or levels[candidate_level] < levels[required_level]:
                continue
            tail = _DEGREE_RE.sub(" ", text, count=1)
            fields = [word for word in _TOKEN_RE.findall(tail.casefold()) if word not in _STOPWORDS and word not in _DEGREE_WORDS]
            if fields and not any(_assertive_fact_match(fact, field) for field in fields):
                continue
            rows.append({"requirement": required.group(0), "candidate_evidence": fact.value, "source": fact.source})
            break
    return rows


def _decision_rows(decision: Mapping[str, object] | None) -> tuple[list[dict[str, str]], list[str], list[str], bool]:
    evidence: list[dict[str, str]] = []
    conflicts: list[str] = []
    unknowns: list[str] = []
    if decision is None:
        return evidence, conflicts, ["the current hard-filter decision is unavailable"], False
    if decision.get("verdict") == "kill":
        reason = _text(decision.get("reason")) or "the posting failed a required constraint"
        return evidence, [f"hard filter: {reason} (hard_filter decision)"], [], True
    if decision.get("verdict") != "pass":
        return evidence, conflicts, ["the current hard-filter decision has no recognized pass or kill verdict"], False
    facts = decision.get("facts")
    if facts is None:
        return evidence, conflicts, ["the passing hard-filter decision has no recorded facts (hard_filter decision)"], False
    if not isinstance(facts, Mapping):
        return evidence, conflicts, ["the passing hard-filter facts are malformed (hard_filter decision)"], False
    for name, value in facts.items():
        label, value_text = str(name).replace("_", " "), _text(value)
        if value_text.casefold() in _UNKNOWN_FACTS or value_text.casefold().endswith("_unknown"):
            unknowns.append(f"hard-filter fact {label} was {value_text} (hard_filter decision)")
        elif value_text:
            evidence.append({"requirement": f"hard-filter {label}", "candidate_evidence": value_text, "source": "hard_filter decision"})
    return evidence, conflicts, unknowns, False


def _unmatched_clauses(context: str, facts: list[_Fact]) -> list[str]:
    """Keep independent conjunctions unknown unless each has resume overlap.

    A matched multiword fact can cross a conjunction (for example, "research
    and development"). Its actual span supplies evidence on both sides; a
    match elsewhere in the paragraph cannot satisfy the remaining clauses.
    """
    variants = [(fact, variant) for fact in facts for variant in _variants(fact)]
    spans = [
        match.span()
        for fact, variant in variants
        if _assertive_fact_match(fact, variant) and (match := _match(context, variant))
    ]
    boundaries = list(re.finditer(r"\s+(?:and|as well as)\s+|;\s*|,\s+(?:and\s+)?", context, re.I))
    starts = [0, *(boundary.end() for boundary in boundaries)]
    ends = [*(boundary.start() for boundary in boundaries), len(context)]
    unmatched = []
    for start, end in zip(starts, ends):
        clause = context[start:end].strip()
        if not clause:
            continue
        covered = any(
            _assertive_fact_match(fact, variant) and _match(clause, variant)
            for fact, variant in variants
        ) or any(
            re.search(r"\w", context[max(start, first):min(end, last)])
            for first, last in spans if max(start, first) < min(end, last)
        ) or any(broader_matches(clause, fact.value, fact.kind) for fact in facts)
        if not covered:
            unmatched.append(clause)
    return unmatched


def assess_posting(posting: dict, profile: Profile | None, decision: dict | None = None) -> dict:
    """Return a serializable evidence assessment for ``posting``.

    The five returned keys are always ``status``, ``summary``, ``evidence``,
    ``conflicts`` and ``unknowns``.  Missing source fields remain unknown; a
    snippet or missing description can never become ``suitable``.
    """
    if profile is None:
        return {
            "status": "unassessed",
            "summary": "No candidate Profile is available for assessment.",
            "evidence": [],
            "conflicts": [],
            "unknowns": ["candidate Profile is unavailable"],
        }
    posting = posting if isinstance(posting, Mapping) else {}
    listing_status = _choice(posting, "listing_status")
    evidence, conflicts, unknowns, hard_conflict = _decision_rows(decision if isinstance(decision, Mapping) else None)
    if listing_status == "closed":
        conflicts.insert(0, f"Listing is marked closed ({_source_field(posting, 'listing_status')})")
    if hard_conflict or listing_status == "closed":
        # There is no reason to repeatedly match every resume phrase against a
        # job already excluded by an explicit constraint. The exclusion itself
        # is the evidence the user needs to inspect or correct.
        return {"status": "not_suitable", "summary": conflicts[0].rstrip(".") + ".",
                "evidence": evidence, "conflicts": conflicts, "unknowns": unknowns}
    description = _plain(posting.get("description_html") or posting.get("description") or posting.get("content") or "")
    description_kind = _description_kind(posting, description)
    if not description and description_kind == "full":
        description_kind = "missing"
    opportunity_type = _choice(posting, "opportunity_type")

    contexts, facts = _contexts(posting, description), _resume_facts(profile.resume)
    seen = {(_normalize(row["requirement"]), row["candidate_evidence"], row["source"]) for row in evidence}
    requirement_evidence = False
    for fact in facts:
        for variant in _variants(fact):
            for context, _context_source in contexts:
                found = _match(context, variant)
                if found and _assertive_fact_match(fact, variant):
                    row = {"requirement": " ".join(found.group(0).split()), "candidate_evidence": fact.value, "source": fact.source}
                    marker = (_normalize(row["requirement"]), row["candidate_evidence"], row["source"])
                    if marker not in seen:
                        seen.add(marker)
                        evidence.append(row)
                    credential_subject_only = bool(_CREDENTIAL_CUE.search(context)) and fact.kind != "credential"
                    substantive = (
                        not credential_subject_only
                        and (variant not in _SUPPORTING_TOOLS or bool(_match(_text(posting.get("title")), variant)))
                    )
                    if substantive and _context_source != "posting.title" and not _context_source.endswith(".preferred"):
                        requirement_evidence = True
                        break
        for context, context_source in contexts:
            if context_source == "posting.title":
                continue
            if _CREDENTIAL_CUE.search(context) and fact.kind != "credential":
                continue
            for requirement in broader_matches(context, fact.value, fact.kind):
                marker = (_normalize(requirement), fact.value, fact.source)
                if marker not in seen:
                    seen.add(marker)
                    evidence.append({"requirement": requirement, "candidate_evidence": fact.value,
                                     "source": fact.source, "basis": "related_skill"})
                if not context_source.endswith(".preferred"):
                    requirement_evidence = True
    requirement_contexts = [context for context in contexts if context[1] != "posting.title" and not context[1].endswith(".preferred")]
    for row in _degree_evidence(requirement_contexts, facts):
        # A shared degree level alone says nothing about the work's relevance.
        marker = (_normalize(row["requirement"]), row["candidate_evidence"], row["source"])
        if marker not in seen:
            seen.add(marker)
            evidence.append(row)

    blocking: set[str] = set()
    if _text(_field(posting, "verification_status")).casefold() in {"unknown", "failed"}:
        unknowns.append("The latest source verification was unsuccessful; retained job details need rechecking.")
        blocking.add("source_verification")
    if not requirement_evidence:
        unknowns.append("no established resume evidence for the work or required skills; title overlap alone or matching only optional qualifications does not establish relevance")
        if any(_normalize(row["requirement"]) in _SUPPORTING_TOOLS for row in evidence):
            unknowns.append("Shared office tools are supporting evidence; they do not establish experience in this job's core work.")
        blocking.add("requirement_evidence")
    if posting.get("source") == "workday" and (
        not posting.get("detail_verified_at")
        or posting.get("detail_verification_status") in {"unknown", "failed"}
    ):
        unknowns.append("The employer listing was found, but its full requirements still need a successful detail refresh.")
        blocking.add("description_verification")
    # Positive overlap alone cannot account for the other requirements. Report
    # requirements we cannot connect to the resume instead of silently treating
    # one matching tool (or a passing geography check) as complete suitability.
    for context, context_source in contexts:
        if context_source == "posting.title" or context_source.endswith(".responsibilities"):
            continue
        mentioned = [term for term in sorted(_TECHNICAL_TERMS) if _match(context, term)]
        missing = [term for term in mentioned if not any(_assertive_fact_match(fact, term) for fact in facts)]
        unmatched = _unmatched_clauses(context, facts)
        credential_gap = (
            not context_source.endswith(".preferred") and bool(_CREDENTIAL_CUE.search(context))
            and not any(
                fact.kind == "credential"
                and any(_assertive_fact_match(fact, term) and _match(context, term) for term in _variants(fact))
                for fact in facts
            )
        )
        if credential_gap:
            unknowns.append(f"Credential or eligibility requirement needs review: {context}")
            blocking.add("credential")
        if missing or unmatched:
            detail = f"No confirmed resume evidence for {', '.join(missing)}" if missing else "Qualification not established"
            if context_source.endswith(".preferred"):
                detail = "Optional qualification gap"
            unresolved = "; ".join(unmatched) if unmatched and not missing else context
            unknowns.append(f"{detail}: {unresolved[:300]}")
            # Missing phrases are not proof that the person cannot do the work.
            # Keep ordinary skill gaps visible. Named credentials and legal
            # eligibility remain material questions even alongside useful overlap.
            if not context_source.endswith(".preferred") and any(_CREDENTIAL_CUE.search(clause) for clause in unmatched):
                blocking.add("credential")
    if listing_status == "unknown":
        unknowns.append(f"{_source_field(posting, 'listing_status')} is unknown; the listing was not verified open")
        blocking.add("listing_status")
    if description_kind == "snippet":
        unknowns.append("the description is a snippet, so the full requirements are unassessed")
        blocking.add("description_kind")
    elif description_kind == "missing":
        unknowns.append("the full job description is missing")
        blocking.add("description_kind")
    if opportunity_type == "unknown":
        unknowns.append(f"{_source_field(posting, 'opportunity_type')} is unknown")
        blocking.add("opportunity_type")
    elif opportunity_type in {"program", "talent_pool"}:
        unknowns.append(f"this is a {opportunity_type.replace('_', ' ')}, not a specific vacancy; its next action needs review")
        blocking.add("opportunity_type")
    if not _text(_field(posting, "last_verified_at")):
        unknowns.append(f"{_source_field(posting, 'last_verified_at')} is unavailable")
        blocking.add("last_verified_at")
    if not _field(posting, "locations") and not _text(posting.get("location")):
        unknowns.append("the posting does not state a location")
    if not _text(_field(posting, "workplace_type")):
        unknowns.append(f"{_source_field(posting, 'workplace_type')} is unavailable")
        if profile.search.remote is not None:
            blocking.add("workplace_type")
    if not _text(_field(posting, "application_deadline")):
        unknowns.append(f"{_source_field(posting, 'application_deadline')} is not listed")
    candidate_evidence = [row for row in evidence if row["source"].startswith("profile.resume.")]
    if not candidate_evidence and not conflicts:
        unknowns.append("no established overlap between the posting requirements and confirmed Profile facts")
    if decision is None or any("hard-filter" in item or "hard_filter" in item for item in unknowns):
        blocking.add("decision")

    status: AssessmentStatus
    if decision is None:
        status = "unassessed"
    elif not candidate_evidence:
        status = "needs_review"
    elif description_kind != "full" or blocking:
        status = "needs_review"
    else:
        status = "suitable"
    unknowns, conflicts = list(dict.fromkeys(unknowns)), list(dict.fromkeys(conflicts))
    if status == "suitable":
        terms = [row["requirement"] for row in evidence if not row["requirement"].startswith("hard-filter") and not _DEGREE_RE.fullmatch(row["requirement"])]
        summary_terms = list(dict.fromkeys(terms))[:3]
        summary = f"Potential match: relevant background in {', '.join(summary_terms)}; no known hard conflict. Review the gaps below." if summary_terms else "Potential match: relevant background and no known hard conflict. Review the gaps below."
    elif status == "not_suitable":
        summary = conflicts[0].rstrip(".") + "." if conflicts else "The posting is not suitable under the current constraints."
    elif status == "unassessed":
        summary = "No established evidence from the available posting details."
    elif not candidate_evidence:
        summary = "Needs review: no established resume evidence for the available posting requirements."
    elif evidence:
        summary = "Needs review: confirmed evidence exists, but the listing or requirements are incomplete."
    else:
        summary = "Needs review: the listing or candidate evidence is incomplete."
    return {"status": status, "summary": summary, "evidence": evidence, "conflicts": conflicts, "unknowns": unknowns}
__all__ = ["AssessmentStatus", "assess_posting"]
