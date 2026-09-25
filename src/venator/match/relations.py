"""Narrow, one-way relevance relationships; never infer a specific skill back.

Python is a programming language (python.org/doc/essays/blurb/). Documented
React web application work supports UI development (react.dev/learn). These
relations establish broader relevance, not years, credentials or expertise.
"""
from __future__ import annotations

import re


_FRAMING = frozenset(
    "a an and are be basic demonstrated experience experienced familiarity have in is knowledge language languages of or preferred prior proficiency proficient required skills skill some strong the to with working previous".split()
)
_ACTION = re.compile(r"\b(?:built|build|developed|develop|implemented|implement|wrote|write|maintained|maintain|coded|code)\b", re.I)
_NEGATED = re.compile(
    r"\b(?:no|not(?!\s+only)|never|without|lack\w*|observed|watch\w*|shadow\w*|"
    r"didn['’]?t|doesn['’]?t|don['’]?t|isn['’]?t|aren['’]?t|can(?:not|'t)|unable)\b",
    re.I,
)
_CLAUSE_BREAK = re.compile(r"[.;!?]|\s+(?:and|or|but|as well as)\s+|,\s*", re.I)
_FACT_HARD_BREAK = re.compile(r"[.;!?]|\b(?:but|however|except|rather than)\b", re.I)
_ACTION_AFTER_JOIN = re.compile(
    # A direct action after a list join starts a new candidate-owned clause.
    # Keep the optional subject to first-person ``I``: ``they built`` describes
    # somebody else, and must not clear an observation/negation marker.
    r"(?:\b(?:and|or)\b|,)\s*(?:\b(?:and|or)\b\s*)?(?:i\s+)?"
    r"(?:built|build|developed|develop|implemented|implement|wrote|write|maintained|maintain|coded|code)\b",
    re.I,
)
_TENURE = re.compile(
    r"\b(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve)\+?\s+(?:years?|months?)\b",
    re.I,
)
_CREDENTIAL = re.compile(r"\b(?:certificat(?:e|ion)|licen[cs]e|clearance)\b", re.I)


def _clause(text: str, start: int, end: int) -> str:
    """Return the small clause containing ``text[start:end]``."""
    before = list(_CLAUSE_BREAK.finditer(text, 0, start))
    left = before[-1].end() if before else 0
    after = _CLAUSE_BREAK.search(text, end)
    right = after.start() if after else len(text)
    return text[left:right]


def fact_match_is_assertive(fact: str, match: re.Match[str]) -> bool:
    """Whether a matched fact span is stated as the candidate's own evidence.

    Resume bullets can mention a tool while explicitly describing observation,
    absence, or a different person's work.  The marker is scoped to the local
    clause so a positive clause in ``observed X, but built Y`` remains usable.
    """
    for marker in _NEGATED.finditer(fact):
        if marker.start() < match.start():
            bridge = fact[marker.end() : match.start()]
            # A negation/observation normally carries across a list joined by
            # "and", "or", or a comma. It stops at an explicit contrast or at
            # a new owned action, preserving "observed X, but built Y".
            if _FACT_HARD_BREAK.search(bridge):
                continue
            if _ACTION_AFTER_JOIN.search(bridge):
                continue
            return False
        if marker.start() >= match.end():
            bridge = fact[match.end() : marker.start()]
            # A marker after the term governs it only within the same local
            # phrase; punctuation or a conjunction usually starts the next
            # list item ("Python, not R").
            if not re.search(r"[.,;!?]|\b(?:and|or|but|however|except)\b", bridge):
                return False
    return True


def _positive_match(text: str, pattern: str) -> re.Match[str] | None:
    for match in re.finditer(pattern, text, re.I):
        if fact_match_is_assertive(text, match):
            return match
    return None


def _context_clauses(context: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_BREAK.split(context) if part.strip()]


def _has_inherited_modifier(context: str) -> bool:
    """Reject a broad relation when a split clause sheds its qualifier.

    A relation may inspect an individual generic branch of a conjunction, but
    it must retain a shared tenure, credential, or specialization modifier.
    Refusing the whole relation is safer than claiming that Python establishes
    five years of coding or a specialized programming discipline.
    """
    if _TENURE.search(context) or _CREDENTIAL.search(context):
        return True
    for clause in _context_clauses(context):
        if not re.search(r"\b(?:programming|coding)\b", clause, re.I):
            continue
        remainder = re.sub(r"\b(?:programming|coding)\b", " ", clause, flags=re.I)
        words = set(re.findall(r"[\w+#]+", remainder.casefold()))
        if words - _FRAMING:
            return True
    return False


def broader_matches(context: str, fact: str, kind: str) -> list[str]:
    """Return original requirement spans supported by a broader relationship.

    Only plain, generic qualification clauses qualify. For example, Python
    does not establish CNC programming, or a stated number of years. New
    relationships need their own positive and reverse/negative examples.
    """
    if _has_inherited_modifier(context):
        return []

    python = _positive_match(fact, r"\bPython\b")
    python_clause = _clause(fact, python.start(), python.end()) if python else ""
    applied_python = kind in {"experience", "project"} and bool(_ACTION.search(python_clause))
    patterns: list[str] = []
    if python and (kind in {"skill", "coursework"} or applied_python):
        patterns.append(r"programming|coding")

    react = _positive_match(fact, r"\bReact\b")
    if react:
        react_clause = _clause(fact, react.start(), react.end())
        applied_react = kind in {"experience", "project"} and bool(_ACTION.search(react_clause))
        has_web_apps = bool(re.search(r"\bweb\s+(?:apps?|applications?|interfaces?)\b", react_clause, re.I))
        if applied_react and has_web_apps:
            patterns.append(r"front[ -]?end development")

    matches: list[str] = []
    for pattern in patterns:
        for clause in _context_clauses(context):
            found = re.search(rf"\b(?:{pattern})\b", clause, re.I)
            if found is None:
                continue
            remainder = clause[:found.start()] + clause[found.end():]
            if set(re.findall(r"[\w+#]+", remainder.casefold())) <= _FRAMING:
                value = found.group(0)
                if value not in matches:
                    matches.append(value)
                break
    return matches


__all__ = ["broader_matches", "fact_match_is_assertive"]
