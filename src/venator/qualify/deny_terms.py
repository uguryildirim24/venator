"""Reviewed student-input deny terms. One hit withholds the whole source text."""

from __future__ import annotations

import re

from venator.qualify.schema import Withheld

PRONOUN_TERMS: tuple[str, ...] = (
    "he", "she", "him", "her", "hers", "his", "himself", "herself",
)

PROTECTED_TERMS: tuple[str, ...] = (
    # Religion and worship.
    "religion", "religious", "christian", "christianity", "muslim", "islam",
    "jewish", "judaism", "hindu", "hinduism", "buddhist", "buddhism",
    "church", "mosque", "synagogue", "temple", "prayer", "worship",
    # Ethnicity and national origin.
    "ethnicity", "ethnic", "race", "racial", "nationality", "citizenship",
    "turkish", "danish", "hispanic", "latino", "latina", "asian", "black", "white",
    # Disability and medical condition.
    "disability", "disabled", "diagnosis", "diagnosed", "autism", "adhd",
    "depression", "anxiety", "diabetes", "cancer", "medical condition",
    # Age, birth, and family status.
    "age", "aged", "birth", "birthday", "born", "minor", "elderly",
    "married", "spouse", "husband", "wife", "children", "child", "parent",
    "mother", "father", "pregnant", "pregnancy", "maternity", "paternity",
    # Gender-specific organisations.
    "women in science", "women in tech", "society of women engineers",
    "girls who code", "women's association", "men's association",
)


def denied_class(text: str) -> Withheld:
    """Return the first applicable class using whole word or whole phrase matching."""
    for term in PRONOUN_TERMS:
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.IGNORECASE):
            return "pronoun"
    for term in PROTECTED_TERMS:
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.IGNORECASE):
            return "protected_term"
    return None
