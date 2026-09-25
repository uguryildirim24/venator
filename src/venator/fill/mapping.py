"""Conservative FormSpec-label to Profile-fact mapping."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class FactSources:
    """Where a FillPlan says each value came from — one Profile's real files."""

    resume: str = "resume.yaml"
    constraints: str = "constraints.yaml"


DEFAULT_SOURCES = FactSources()


@dataclass(frozen=True)
class Fact:
    value: str | bool
    source: str


@dataclass(frozen=True)
class FieldMapping:
    value: str | bool | None
    source: str | None
    reason: str | None

    @property
    def mapped(self) -> bool:
        return self.reason is None


@dataclass(frozen=True)
class _OptionResolution:
    value: str | None
    reason: str | None = None


def normalize_label(label: str) -> str:
    """Normalize presentation punctuation, but never infer semantic similarity."""
    normalized = label.casefold().replace("&", " and ")
    normalized = re.sub(r"\(required\)\s*$", "", normalized)
    normalized = re.sub(r"\brequired\s*$", "", normalized)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


_ALIASES = {
    "full_name": (
        "Full Name",
        "Legal Name",
        "Name",
    ),
    "first_name": (
        "First Name",
        "Legal First Name",
        "Given Name",
    ),
    "last_name": (
        "Last Name",
        "Legal Last Name",
        "Family Name",
        "Surname",
    ),
    "email": (
        "Email",
        "Email Address",
    ),
    "phone": (
        "Phone",
        "Phone Number",
        "Telephone",
        "Mobile Phone",
    ),
    "location": (
        "Location",
        "Current Location",
        "City and State",
        "City, State",
    ),
    "linkedin": (
        "LinkedIn",
        "LinkedIn Profile",
        "LinkedIn URL",
        "Website",
        "Personal Website",
    ),
    "school": (
        "School",
        "School Name",
        "College",
        "College or University",
        "University",
    ),
    "degree": (
        "Degree",
        "Degree Name",
        "Degree Type",
    ),
    "graduation_date": (
        "Graduation Date",
        "Expected Graduation Date",
    ),
    "gpa": (
        "GPA",
        "Grade Point Average",
    ),
    "work_authorization_status": (
        "Work Authorization Status",
        "Visa Status",
        "Immigration Status",
    ),
    "binary_work_authorization": (
        "Are you authorized to work in the United States?",
        "Are you legally authorized to work in the United States?",
        "Are you currently authorized to work in the United States?",
    ),
    "sponsorship": (
        "Need employer sponsorship?",
        "Require sponsorship?",
        "Do you require sponsorship?",
        "Do you require sponsorship now or in the future?",
        "Will you require sponsorship?",
        "Will you now or in the future require sponsorship?",
        "Will you now or in the future require sponsorship to work in the United States?",
        "Will you now or in the future require employer sponsorship?",
        "Do you now or in the future require sponsorship?",
        "Will you now or in the future require sponsorship for employment visa status?",
        "Will you now or in the future require visa sponsorship?",
        "Do you now or in the future require visa sponsorship?",
        "Do you now or will you in the future need employer sponsorship to work in the United States?",
        "Do you now or will you in the future need employer sponsorship for a visa to work in the country where the job is open?",
        "Will you now or in the future need employer sponsorship to work in the United States?",
        "Do you need employer sponsorship now or in the future?",
    ),
    "resume_file": (
        "Resume",
        "Resume CV",
        "Resume/CV",
        "Attach Resume",
        "Upload Resume",
    ),
    "salary_expectations": (
        "Salary Expectations",
        "Salary Expectation",
        "Desired Salary",
    ),
    "resides_near_posting": (
        "Do you reside in the location for this job posting or within commutable distance?",
        "Do you live in or within commuting distance of the job location?",
        "Do you reside near the job location?",
    ),
    "prior_employment_at_company": (
        "Have you previously been employed by this company?",
        "Have you ever worked for this company?",
        "Were you previously employed by this company?",
    ),
    "relatives_at_company": (
        "Do you have relatives at this company?",
        "Do you have any relatives currently employed by this company?",
        "Are any of your relatives employed by this company?",
    ),
    "how_heard": (
        "How did you hear about us?",
        "How did you hear about this position?",
        "How did you learn about this opportunity?",
    ),
    "country": (
        "Country",
        "Country or Region",
        "Country/Region",
    ),
    "eeo_gender": (
        "Gender",
        "Gender Identity",
    ),
    "eeo_hispanic_latino": (
        "Are you Hispanic/Latino?",
        "Hispanic or Latino",
        "Hispanic/Latino",
    ),
    "eeo_veteran_status": (
        "Veteran Status",
        "Protected Veteran Status",
    ),
    "eeo_disability_status": (
        "Disability Status",
        "Voluntary Self-Identification of Disability",
    ),
}

LABEL_FACTS = {
    normalize_label(label): fact_name
    for fact_name, labels in _ALIASES.items()
    for label in labels
}

SCREENING_PATHS = {
    "salary_expectations": "screening.salary_expectations",
    "resides_near_posting": "screening.resides_near_posting",
    "prior_employment_at_company": "screening.prior_employment_at_company",
    "relatives_at_company": "screening.relatives_at_company",
    "how_heard": "screening.how_heard",
    "country": "screening.country",
    "eeo_gender": "screening.eeo.gender",
    "eeo_hispanic_latino": "screening.eeo.hispanic_latino",
    "eeo_veteran_status": "screening.eeo.veteran_status",
    "eeo_disability_status": "screening.eeo.disability_status",
}


def _option_key(value: object) -> str:
    """Read one alias key. YAML reads a bare Yes/No as a boolean, not a word."""
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return str(value)


def canonical_option_aliases(
    constraints: Mapping[str, object],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Read the Profile's explicit, reviewable option spellings.

    Deliberately small by design: an entry requires knowing both the Profile
    value and the ATS's canonical label, so it is the Owner who writes one
    (constraints.yaml: option_aliases). Everything else must match through the
    deterministic canonical normalization below or the field goes to
    `unmapped`.

    A malformed entry is skipped rather than raised on — an unusable alias must
    not stop a whole FillPlan — but it says so on stderr, because an alias that
    is silently ignored looks exactly like an alias that did not match.
    """
    configured = constraints.get("option_aliases")
    if configured is None:
        return {}
    if not isinstance(configured, Mapping):
        print(
            f"WARNING: constraints.yaml: option_aliases must be a mapping of fact name to "
            f"spellings, not a {type(configured).__name__} — every alias is ignored",
            file=sys.stderr,
        )
        return {}
    aliases: dict[str, dict[str, tuple[str, ...]]] = {}
    for fact_name, spellings in configured.items():
        if not isinstance(spellings, Mapping):
            print(
                f"WARNING: constraints.yaml: option_aliases.{fact_name} must be a mapping of "
                f"Profile value to ATS spellings, not a {type(spellings).__name__} — ignored",
                file=sys.stderr,
            )
            continue
        aliases[str(fact_name)] = {
            normalize_label(_option_key(value)): tuple(
                str(alias) for alias in (alternatives or ()) if str(alias).strip()
            )
            for value, alternatives in spellings.items()
        }
    return aliases


#: Sponsorship routing uses direct Owner-focused phrases. The wider stem is
#: reserved for guards that reject combined authorization/sponsorship fields and
#: sponsorship-bearing authorization options.
_SPONSORSHIP_WORD = "sponsor"
_SPONSORSHIP_QUESTION = re.compile(
    r"^(?:(?:are|can|do|will|would) you|(?:does|will|would) the applicant)"
    r"(?:\s+(?:any|at|be|currently|ever|future|in|later|now|or|the|time|will|would|you))*"
    r"\s+(?:need|needs|require|requires|required|requiring)\b"
    r"(?:\s+(?:employer|employment|immigration|visa|work))*"
    r"\s+sponsorship\b"
)


def fact_name_for_label(label: str) -> str | None:
    normalized = normalize_label(label)
    exact = LABEL_FACTS.get(normalized)
    if exact is not None:
        if exact == "binary_work_authorization" and _SPONSORSHIP_WORD in normalized:
            # A combined question is not an authorization question. Answering it
            # from `authorized_to_work` would state something about sponsorship
            # the Profile never said — which is the one thing the planner is
            # structurally unable to do, and stays unable to do.
            return None
        return exact
    if _SPONSORSHIP_QUESTION.search(normalized) is not None:
        words = normalized.split()
        if "authoriz" in normalized or "without" in words:
            return None
        return "sponsorship"
    if normalized.startswith("have you ever been employed by or performed services for "):
        return "prior_employment_at_company"
    if (
        normalized.startswith("do you have any relatives")
        and "employed by" in normalized
        and not any(
            phrase in normalized
            for phrase in ("federal government", "department of", "military")
        )
    ):
        return "relatives_at_company"
    return None


_DEGREE_ABBREVIATIONS = {
    "aa": ("associate", "arts"),
    "as": ("associate", "science"),
    "ba": ("bachelor", "arts"),
    "bba": ("bachelor", "business", "administration"),
    "beng": ("bachelor", "engineering"),
    "bfa": ("bachelor", "fine", "arts"),
    "bs": ("bachelor", "science"),
    "bsc": ("bachelor", "science"),
    "ma": ("master", "arts"),
    "mba": ("master", "business", "administration"),
    "meng": ("master", "engineering"),
    "mfa": ("master", "fine", "arts"),
    "ms": ("master", "science"),
    "msc": ("master", "science"),
    "edd": ("doctor", "education"),
    "phd": ("doctor", "philosophy"),
}


def _school_word_order_variant(value: str) -> str | None:
    """Return a reordered spelling for diagnostics, never for resolution."""
    words = normalize_label(value).split()
    if words[:2] == ["university", "of"] and len(words) > 2:
        return " ".join((*words[2:], "university"))
    if len(words) > 1 and words[-1] == "university":
        return " ".join(("university", "of", *words[:-1]))
    return None


def _degree_option_key(value: str) -> str:
    words = normalize_label(value).split()
    for size in range(min(3, len(words)), 0, -1):
        expanded = _DEGREE_ABBREVIATIONS.get("".join(words[:size]))
        if expanded is not None:
            words = [*expanded, *words[size:]]
            break
    if words[:1] == ["associates"]:
        words[0] = "associate"
    elif words[:1] == ["bachelors"]:
        words[0] = "bachelor"
    elif words[:1] == ["masters"]:
        words[0] = "master"
    if len(words) > 1 and words[0] in {"associate", "bachelor", "master", "doctor"}:
        if words[1] == "s":
            words.pop(1)
        if words[1:2] == ["of"]:
            words.pop(1)
    if words[-1:] == ["degree"] and len(words) > 1:
        words.pop()
    return " ".join(words)


def _canonical_option_key(value: str, fact_name: str) -> str:
    if fact_name == "degree":
        return _degree_option_key(value)
    return normalize_label(value)


def _canonical_matches(candidate: str, options: list[str], fact_name: str) -> list[str]:
    key = _canonical_option_key(candidate, fact_name)
    return list(
        dict.fromkeys(
            option for option in options if _canonical_option_key(option, fact_name) == key
        )
    )


def _ambiguous_reason(value: str, matches: list[str]) -> str:
    choices = ", ".join(repr(match) for match in matches)
    return (
        f"Profile value {value!r} matches multiple canonical options after normalization: "
        f"{choices}"
    )


def _unsafe_school_word_order_reason(value: str, options: list[str]) -> str | None:
    variant = _school_word_order_variant(value)
    if variant is None:
        return None
    matches = list(
        dict.fromkeys(option for option in options if normalize_label(option) == variant)
    )
    if not matches:
        return None
    choices = ", ".join(repr(match) for match in matches)
    return (
        f"Profile value {value!r} only matches after unsafe school word reordering: "
        f"{choices}; add an explicit school option alias"
    )


def _resolve_canonical_option(
    value: str,
    options: list[str],
    *,
    fact_name: str,
    aliases: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
) -> _OptionResolution:
    if value in options:
        return _OptionResolution(value)

    matches = _canonical_matches(value, options, fact_name)
    if len(matches) == 1:
        return _OptionResolution(matches[0])

    spellings = (aliases or {}).get(fact_name, {}).get(normalize_label(value), ())
    alias_matches: list[str] = []
    for alias in spellings:
        if alias in options:
            alias_matches.append(alias)
        else:
            alias_matches.extend(_canonical_matches(alias, options, fact_name))
    alias_matches = list(dict.fromkeys(alias_matches))
    if len(alias_matches) == 1:
        return _OptionResolution(alias_matches[0])
    if len(alias_matches) > 1:
        return _OptionResolution(None, _ambiguous_reason(value, alias_matches))
    if len(matches) > 1:
        return _OptionResolution(None, _ambiguous_reason(value, matches))
    if fact_name == "school":
        reason = _unsafe_school_word_order_reason(value, options)
        if reason is not None:
            return _OptionResolution(None, reason)
    return _OptionResolution(None)


def _option_is_allowed(resolved: str, fact_name: str) -> bool:
    if fact_name == "sponsorship" and not normalize_label(resolved).startswith("yes"):
        return False
    if (
        fact_name == "binary_work_authorization"
        and _SPONSORSHIP_WORD in normalize_label(resolved)
    ):
        # The option, not just the label, can smuggle the other question in:
        # "Yes, and I will not require sponsorship" is an ATS's way of asking
        # both at once. The authorization fact says nothing about sponsorship,
        # so it cannot pick that option — not even through an Owner-written
        # alias, which is the one route by which it could otherwise arrive.
        return False
    return True


def _nested_value(profile: Mapping[str, object], path: str) -> object | None:
    value: object = profile
    for component in path.split("."):
        if isinstance(value, Mapping):
            value = value.get(component)
        elif isinstance(value, list) and component.isdigit():
            index = int(component)
            if index >= len(value):
                return None
            value = value[index]
        else:
            return None
    return value


def _string_fact(
    profile: Mapping[str, object],
    path: str,
    source_file: str,
) -> Fact | None:
    value = _nested_value(profile, path)
    if not isinstance(value, str) or not value.strip():
        return None
    return Fact(value.strip(), f"{source_file}:{path}")


def _screening_fact(
    constraints: Mapping[str, object],
    path: str,
    sources: FactSources,
) -> Fact | None:
    value = _nested_value(constraints, path)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    elif not isinstance(value, bool):
        return None
    return Fact(value, f"{sources.constraints}:{path}")


def _name_part(resume: Mapping[str, object], sources: FactSources, *, first: bool) -> Fact | None:
    name = _string_fact(resume, "name", sources.resume)
    if name is None:
        return None
    parts = name.value.split()
    if not parts:
        return None
    value = parts[0] if first else parts[-1]
    return Fact(value.strip('"\''), name.source)


AUTHORIZATION_PATH = "work_authorization.authorized_to_work"


def _authorization_fact(constraints: Mapping[str, object], sources: FactSources) -> Fact | None:
    """Answer "are you authorized to work here?" only from an explicit statement.

    The Profile must say ``work_authorization.authorized_to_work`` outright, as
    a boolean the Owner wrote themselves. It is optional and it has three
    states, all of which have to stay distinguishable all the way to the form:

    * ``true`` — the Owner has said they are authorized; the field is answered
      "Yes" and the source recorded.
    * ``false`` — the Owner has said they are not; the field is answered "No".
      Their statement, not the planner's inference.
    * absent, ``null``, or anything that is not a boolean — **not configured**,
      which is not an answer. The field goes to ``unmapped`` for the Owner to
      fill in by hand, and nothing anywhere renders it as a "No".

    Hence ``is True`` / ``is False`` rather than a truthiness test: a null that
    collapses into a "No" on an attestation-adjacent question is a false
    statement on an application, and the collapse is exactly what a plain
    falsy check would do. Nothing here reads ``status``, either — an
    immigration status is not an eligibility answer, and inferring one from it
    would be the planner guessing on the Owner's behalf.

    This fact is deliberately not the sponsorship fact and cannot reach it.
    "Authorized to work" and "requires sponsorship" are different questions with
    different fields; ``_sponsorship_fact`` still answers only from
    ``requires_sponsorship: true`` and still cannot say "No".
    """
    value = _nested_value(constraints, AUTHORIZATION_PATH)
    if value is not True and value is not False:
        return None
    return Fact(value, f"{sources.constraints}:{AUTHORIZATION_PATH}")


def _sponsorship_fact(constraints: Mapping[str, object], sources: FactSources) -> Fact | None:
    """Answer a sponsorship question only from an explicit Profile statement.

    The Profile must say `work_authorization.requires_sponsorship: true`. A
    status string is not enough: inferring "will require sponsorship" from an
    immigration status would be the planner guessing on the Owner's behalf, and
    a wrong answer here is a false statement on an application.
    """
    path = "work_authorization.requires_sponsorship"
    if _nested_value(constraints, path) is not True:
        return None
    return Fact(True, f"{sources.constraints}:{path}")


def _primary_education_index(resume: Mapping[str, object]) -> int:
    """The education entry an application form should be answered from."""
    entries = resume.get("education")
    if not isinstance(entries, list):
        return 0
    for index, entry in enumerate(entries):
        if isinstance(entry, Mapping) and entry.get("primary") is True:
            return index
    return 0


def resolve_fact(
    fact_name: str,
    resume: Mapping[str, object],
    constraints: Mapping[str, object],
    resume_file: Path | None,
    sources: FactSources = DEFAULT_SOURCES,
) -> Fact | None:
    if fact_name == "full_name":
        return _string_fact(resume, "name", sources.resume)
    if fact_name == "first_name":
        return _name_part(resume, sources, first=True)
    if fact_name == "last_name":
        return _name_part(resume, sources, first=False)
    education = _primary_education_index(resume)
    resume_paths = {
        "email": "contact.email",
        "phone": "contact.phone",
        "location": "contact.location",
        "linkedin": "contact.linkedin",
        "school": f"education.{education}.org",
        "degree": f"education.{education}.degree",
        "graduation_date": f"education.{education}.date",
        "gpa": f"education.{education}.gpa",
    }
    if fact_name in resume_paths:
        return _string_fact(resume, resume_paths[fact_name], sources.resume)
    if fact_name == "work_authorization_status":
        return _string_fact(
            constraints,
            "work_authorization.status",
            sources.constraints,
        )
    if fact_name == "binary_work_authorization":
        # Answered only from the Owner's own statement. A work-authorization
        # status alone does not establish a truthful yes/no response, and
        # eligibility is never inferred from one.
        return _authorization_fact(constraints, sources)
    if fact_name == "sponsorship":
        return _sponsorship_fact(constraints, sources)
    if fact_name in SCREENING_PATHS:
        return _screening_fact(constraints, SCREENING_PATHS[fact_name], sources)
    if fact_name == "resume_file" and resume_file is not None and resume_file.is_file():
        return Fact(str(resume_file), f"{resume_file.as_posix()}:$file")
    return None


def map_field(
    field: Mapping[str, object],
    resume: Mapping[str, object],
    constraints: Mapping[str, object],
    *,
    resume_file: Path | None = None,
    sources: FactSources = DEFAULT_SOURCES,
) -> FieldMapping:
    label = field["label"]
    kind = field["kind"]
    if not isinstance(label, str) or not isinstance(kind, str):
        raise ValueError("validated FormSpec fields must have string label and kind")

    fact_name = fact_name_for_label(label)
    if fact_name is None:
        return FieldMapping(None, None, "no Profile fact covers this")
    fact = resolve_fact(fact_name, resume, constraints, resume_file, sources)
    if fact is None:
        if fact_name == "binary_work_authorization":
            reason = (
                f"owner has not stated {sources.constraints}:{AUTHORIZATION_PATH}, so there is "
                "no binary authorization answer to give — it is left for the Owner, never "
                "answered 'No'"
            )
        elif fact_name == "resume_file":
            reason = "no prepared resume artifact exists for this Posting"
        elif fact_name in SCREENING_PATHS:
            reason = f"owner has not provided {sources.constraints}:{SCREENING_PATHS[fact_name]}"
        else:
            reason = "the mapped Profile fact has no value"
        return FieldMapping(None, None, reason)

    if kind == "file":
        if fact_name != "resume_file":
            return FieldMapping(None, None, "this file field has no Profile-backed artifact")
        return FieldMapping(fact.value, fact.source, None)
    if fact_name == "resume_file":
        return FieldMapping(None, None, "the resume artifact only maps to a file field")

    if kind == "checkbox":
        if not isinstance(fact.value, bool):
            return FieldMapping(None, None, "the Profile fact is not a checkbox answer")
        return FieldMapping(fact.value, fact.source, None)

    value = "Yes" if fact.value is True else "No" if fact.value is False else fact.value
    if kind in {"select", "radio"}:
        options = field["options"]
        if not isinstance(options, list):
            raise ValueError("validated select/radio fields must provide options")
        resolution = _resolve_canonical_option(
            value,
            options,
            fact_name=fact_name,
            aliases=canonical_option_aliases(constraints),
        )
        if resolution.value is None:
            return FieldMapping(
                None,
                None,
                resolution.reason
                or f"Profile value {value!r} is not an exact or canonical option",
            )
        if not _option_is_allowed(resolution.value, fact_name):
            return FieldMapping(
                None,
                None,
                f"Profile value {value!r} is not an exact or canonical option",
            )
        value = resolution.value
    return FieldMapping(value, fact.source, None)
