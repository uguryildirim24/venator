"""Deterministic compilation of loaded Profile facts and student-safe projection."""

from __future__ import annotations

import hashlib
import json
import re
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass, replace
from datetime import date
from types import MappingProxyType
from typing import Any

from venator.profile.facts import confirmed
from venator.profile.schema import Profile
from venator.qualify.deny_terms import denied_class
from venator.qualify.schema import (
    Authorization, Availability, Bullet, CompiledProfile, Coursework, Credential,
    E7, Education, EducationPolicy, Evidence, Experience, Fact, FactStatus,
    Interval, NameMap, Skill, StudentBullet, StudentEducation, StudentExperience,
    StudentNamedFact, StudentProjection, StudentAuthorization, StudentAvailability,
    StudentEducationPolicy, Visibility, Withheld, WithheldCounts,
)

COMPILER_VERSION = "4"
_MONTHS = {name.casefold(): index for index, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1
)}
_DATE = re.compile(r"\b(\d{4})-(\d{1,2})(?:-\d{1,2})?\b|\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{4})\b", re.IGNORECASE)
_EXPECTED = re.compile(r"expected|in[ -]?progress|pursuing|candidate", re.IGNORECASE)
_COURSE_CODE = re.compile(r"\b[A-Z]{2,4}\s?\d{3}[A-Z]?\b")
_SPLIT = re.compile(r"[,;\n]|\s+\|\s+")
_ALIAS = (
    ("high_school", ("high school", "HS diploma", "GED", "secondary")),
    ("associate", ("associate", "AA", "AS", "AAS")),
    ("bachelor", ("bachelor", "BS", "BSc", "BA", "BEng", "BE", "BBA", "BFA", "B.Tech")),
    ("master", ("master", "MS", "MSc", "MA", "MEng", "MBA", "MPH", "M.Tech")),
    ("doctorate", ("PhD", "doctorate", "doctoral", "DPhil", "MD", "JD", "PharmD", "DDS", "DVM", "EdD")),
)
_RANK = {"high_school": 0, "associate": 1, "bachelor": 2, "master": 3, "doctorate": 4}


class ProfileInconsistent(ValueError):
    """Loaded Profile facts contradict its declared education policy."""


def _warning(reason: str) -> None:
    warnings.warn(reason, UserWarning, stacklevel=2)


def _confirmed_fact(node: object) -> bool:
    """Treat an explicit confirmation as independent of resume-page inclusion."""
    if isinstance(node, Mapping) and node.get("confirmed") is True:
        node = {key: value for key, value in node.items() if key != "include"}
    return confirmed(node)


def _status(*nodes: object) -> FactStatus:
    return "confirmed" if all(_confirmed_fact(node) for node in nodes) else "draft"


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "name", "value"):
            if isinstance(value.get(key), str):
                return str(value[key]).strip()
    return ""


def _month(value: object) -> str | None:
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    if not isinstance(value, str):
        return None
    match = _DATE.search(value)
    if match is None:
        return None
    if match.group(1):
        year, month = int(match.group(1)), int(match.group(2))
    else:
        year, month = int(match.group(4)), _MONTHS[match.group(3).casefold()]
    return f"{year:04d}-{month:02d}" if 1 <= month <= 12 else None


def _ordinal(month: str) -> int:
    year, number = (int(part) for part in month.split("-"))
    return year * 12 + number - 1


def _interval(value: object) -> tuple[Interval | None, str | None]:
    text = _text(value)
    parts = re.split(r"\s+(?:-|–|—|to)\s+", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None, "unsupported interval shape"
    month_name = "|".join(_MONTHS)
    month_pattern = rf"(?:{month_name})\s+\d{{4}}"
    if not re.fullmatch(month_pattern, parts[0].strip(), re.IGNORECASE):
        return None, "unparseable interval month"
    start = _month(parts[0])
    current = parts[1].strip().casefold() in {"present", "current", "ongoing"}
    end = None if current else _month(parts[1]) if re.fullmatch(month_pattern, parts[1].strip(), re.IGNORECASE) else None
    if start is None or (not current and end is None):
        return None, "unparseable interval month"
    if end is not None and end < start:
        return None, "interval ends before it starts"
    return Interval(start, end, current), None


def _months(interval: Interval | None, cutoff: str) -> tuple[int | None, int | None]:
    if interval is None:
        return None, None
    start = _ordinal(interval.start)
    limit = _ordinal(cutoff)
    end = min(_ordinal(interval.end), limit) if interval.end else limit
    if start > limit:
        return 0, 0
    return max(0, end - start - 1), max(0, end - start + 1)


def _degree(text: str) -> str:
    matches: list[tuple[int, str]] = []
    for level, aliases in _ALIAS:
        for alias in aliases:
            if alias == "HS diploma":
                pattern = r"(?<!\w)H\.?S\.?\s+diploma(?!\w)"
            elif " " not in alias and (alias.isupper() or any(char.isupper() for char in alias[1:])):
                letters = alias.replace(".", "")
                middle = r"\.?".join(re.escape(char) for char in letters)
                pattern = r"(?<!\w)" + middle + r"\.?(?!\w)"
            else:
                pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
            found = re.search(pattern, text, re.IGNORECASE)
            if found:
                matches.append((found.start(), level))
    return min(matches, key=lambda pair: pair[0])[1] if matches else "unknown"


def _field(text: str) -> str | None:
    match = re.search(r"\s+in\s+", text, re.IGNORECASE)
    if match is None:
        match = re.search(r"\s+of\s+", text, re.IGNORECASE)
    if match is None:
        return None
    field = re.split(r"\s+at\s+|[,(]", text[match.end():], maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if field.casefold() in {"science", "arts", "engineering", "philosophy", "fine arts", "laws", "medicine", "business"}:
        return None
    return field or None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _indexed(value: object) -> list[tuple[int, object]]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(enumerate(value))
    if isinstance(value, Mapping):
        for key in ("entries", "items"):
            child = value.get(key)
            if isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
                return list(enumerate(child))
        return [(0, value)]
    # Keep a malformed section as one unknown entry instead of erasing it.
    return [(0, value)] if value is not None else []


def _letter(index: int) -> str:
    result = ""
    while True:
        result = chr(65 + index % 26) + result
        index = index // 26 - 1
        if index < 0:
            return result


def _names(resume: Mapping[str, object]) -> tuple[NameMap, Mapping[str, str], Mapping[str, str]]:
    institutions: dict[str, str] = {}
    organisations: dict[str, str] = {}
    for _, row in _indexed(resume.get("education")):
        name = _text(_mapping(row).get("institution") or _mapping(row).get("org"))
        if name and name.casefold() not in institutions:
            institutions[name.casefold()] = f"Institution {_letter(len(institutions))}"
    for section in ("experience", "campus_involvement", "projects"):
        for _, row in _indexed(resume.get(section)):
            name = _text(_mapping(row).get("organization") or _mapping(row).get("org"))
            if name and name.casefold() not in organisations:
                organisations[name.casefold()] = f"Organisation {_letter(len(organisations))}"
    combined = dict(organisations)
    combined.update(institutions)
    return MappingProxyType(combined), institutions, organisations


def scrub_text(text: str, names: NameMap) -> tuple[str, Withheld]:
    """Replace whole institution/organisation phrases, then withhold any denied text."""
    # One pass prevents a source name such as "Institution" from rewriting
    # a placeholder inserted for another name earlier in the same text.
    replacements = {name.casefold(): placeholder for name, placeholder in names.items() if name}
    alternatives = sorted(replacements, key=lambda name: (-len(name), name))
    groups = {f"name{index}": replacements[name] for index, name in enumerate(alternatives)}
    pattern = r"(?<!\w)(?:" + "|".join(
        f"(?P<name{index}>" + re.escape(name) + ")" for index, name in enumerate(alternatives)
    ) + r")(?!\w)"
    rendered = re.sub(pattern, lambda match: groups[match.lastgroup], text,
                      flags=re.IGNORECASE) if alternatives else text
    denied = denied_class(rendered)
    return ("[withheld]", denied) if denied else (rendered, None)


def _items(value: object, path: str, ancestors: tuple[object, ...]) -> list[tuple[str, str, FactStatus]]:
    if isinstance(value, Mapping):
        for key in ("items", "skills", "technologies", "text", "name", "value"):
            if value.get(key) is not None:
                return _items(value[key], path + "." + key, (*ancestors, value))
        _warning(f"{path}: unsupported item shape")
        return []
    if isinstance(value, str):
        return [(part.strip(" ."), f"{path}[{index}]", _status(*ancestors)) for index, part in enumerate(_SPLIT.split(value)) if part.strip(" .")]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result: list[tuple[str, str, FactStatus]] = []
        for index, item in enumerate(value):
            result.extend(_items(item, f"{path}[{index}]", ancestors))
        return result
    if value is not None:
        _warning(f"{path}: unsupported item shape")
    return []


def _gpa(value: object, names: NameMap) -> str | None:
    # YAML commonly loads an unquoted GPA as a number. E7 permits the GPA,
    # never arbitrary annotations or disclosures smuggled into that field.
    text = str(value) if type(value) in (int, float) else _text(value)
    if not text:
        return None
    scrubbed, withheld = scrub_text(text, names)
    if withheld or not re.fullmatch(r"\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?", scrubbed):
        _warning("education.gpa: unsupported GPA shape withheld")
        return None
    return scrubbed


def _jsonable(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (frozenset, set)):
        return sorted(_jsonable(item) for item in value)
    return value


def canonical_json(value: object) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _facts(education: tuple[Education, ...], experience: tuple[Experience, ...],
           skills: tuple[Skill, ...], coursework: tuple[Coursework, ...],
           credentials: tuple[Credential, ...], authorization: Authorization,
           availability: Availability, policy: EducationPolicy) -> Mapping[str, Fact]:
    facts: dict[str, Fact] = {}

    def add(id: str, kind: str, path: str, status: FactStatus, withheld: Withheld, text: str) -> None:
        if id in facts:
            raise ValueError(f"duplicate fact id: {id}")
        facts[id] = Fact(id, kind, path, status, withheld, text)  # type: ignore[arg-type]

    for row in education:
        text = f"{row.degree_level} in {row.field_of_study or 'unspecified field'}, {row.status}"
        if row.expected_completion:
            text += f", expected {row.expected_completion}"
        add(row.id, "education", row.source_path, row.fact_status, row.withheld, "[withheld]" if row.withheld else text)
    for row in experience:
        month_text = "unknown" if row.months is None else f"{row.guaranteed_months}/{row.months} months"
        text = f"{row.role or '[withheld]'} ({row.kind}), {month_text}"
        if row.current:
            text += ", current"
        add(row.id, "experience", row.source_path, row.fact_status, row.role_withheld, "[withheld]" if row.role_withheld else text)
        for bullet in row.bullets:
            add(bullet.id, "bullet", bullet.source_path, bullet.fact_status, bullet.withheld, bullet.text)
    for row in skills:
        add(row.id, "skill", row.source_path, row.fact_status, row.withheld, f"{row.name} ({row.evidence_level})")
    for row in coursework:
        add(row.id, "coursework", row.source_path, row.fact_status, row.withheld, row.name)
    for row in credentials:
        add(row.id, "credential", row.source_path, row.fact_status, row.withheld, row.name)
    add("auth", "auth", authorization.source_path, authorization.fact_status, None,
        f"requires_sponsorship={authorization.requires_sponsorship}; authorized_to_work={authorization.authorized_to_work}")
    confirmation = "confirmed" if availability.expected_graduation_confirmed else "unconfirmed"
    add("avail", "avail", availability.source_path, availability.fact_status, None,
        f"available_from={availability.available_from}; available_until={availability.available_until}; expected_graduation={availability.expected_graduation} ({confirmation}); enrollment={availability.enrollment}")
    add("edupol", "edupol", policy.source_path, policy.fact_status, None,
        f"holds={policy.holds}; in_progress={policy.in_progress}; kill_completed_degree={policy.kill_completed_degree}")
    return MappingProxyType(facts)


def compile_profile(profile: Profile, *, as_of_month: str) -> CompiledProfile:
    """Compile loaded facts at month precision; never inspect YAML source text.

    SPEC §13 item 48 removes resume.completeness. Its presence, even null,
    raises ProfileInconsistent; absence is valid and needs no configuration.
    """
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", as_of_month):
        raise ValueError("as_of_month must be YYYY-MM")
    if profile.scaffold:
        raise ProfileInconsistent("scaffold Profiles cannot be compiled")
    resume, constraints, targeting = profile.resume, profile.constraints, profile.targeting
    if "completeness" in resume:
        raise ProfileInconsistent("resume.completeness is removed by SPEC §13 item 48")
    synthetic = _mapping(targeting.get("profile")).get("synthetic")
    if synthetic is not None and not isinstance(synthetic, bool):
        raise ProfileInconsistent("targeting.profile.synthetic must be true or false")
    names, institutions, organisations = _names(resume)
    timing = profile.search.timing
    search_node = _mapping(targeting.get("search"))
    timing_node = search_node.get("timing")
    timing_status = _status(targeting, search_node, timing_node)
    cutoff = min(as_of_month, _month(timing.reference_date) or as_of_month)

    education: list[Education] = []
    education_node = resume.get("education")
    for index, raw in _indexed(education_node):
        row = _mapping(raw)
        path = f"resume.education[{index}]"
        degree_text = _text(row.get("degree"))
        level = _degree(degree_text)
        field_raw = _field(degree_text)
        field, withheld = scrub_text(field_raw, names) if field_raw else (None, None)
        date_value = row.get("date") or row.get("dates")
        date_text = _text(date_value)
        parsed = _month(date_value) if isinstance(date_value, date) else _month(date_text)
        marked = bool(_EXPECTED.search(date_text))
        status = "in_progress" if marked else "awarded" if parsed and parsed <= as_of_month else "unknown"
        reason = None
        if not isinstance(raw, Mapping):
            reason = "unsupported education entry shape"
        elif level == "unknown":
            reason = "unrecognized degree alias"
        elif date_text and parsed is None:
            reason = "unparseable education date"
        if reason:
            _warning(f"{path}: {reason}")
        institution = _text(row.get("institution") or row.get("org")) or None
        education.append(Education(
            id=f"edu-{index + 1:02d}", source_path=path,
            fact_status=_status(resume, education_node, raw, row.get("degree"), date_value,
                                row.get("gpa")), degree_level=level, field_of_study=field,
            status=status, expected_completion=parsed if marked else None,
            awarded_on=parsed if status == "awarded" else None, institution=institution,
            institution_placeholder=institutions.get(institution.casefold(), "Institution ?") if institution else "Institution ?",
            gpa=_gpa(row.get("gpa"), names), withheld=withheld, unknown_reason=reason,
        ))
    # An explicit current enrollment resolves one otherwise undated entry, never several.
    incomplete = [i for i, row in enumerate(education) if row.status != "awarded"]
    if timing.enrollment == "current" and len(incomplete) == 1:
        i = incomplete[0]
        education[i] = replace(education[i], status="in_progress",
                               fact_status="draft" if timing_status == "draft" else education[i].fact_status)
    progressing = [i for i, row in enumerate(education) if row.status == "in_progress"]
    if timing.expected_graduation_confirmed and timing.expected_graduation:
        if len(progressing) == 1:
            i = progressing[0]
            education[i] = replace(education[i], expected_completion=_month(timing.expected_graduation),
                                   fact_status="draft" if timing_status == "draft" else education[i].fact_status)
        else:
            _warning("timing.expected_graduation: requires exactly one in-progress education entry")

    experience: list[Experience] = []
    for section in ("experience", "campus_involvement", "projects"):
        node = resume.get(section)
        for index, raw in _indexed(node):
            row = _mapping(raw)
            path = f"resume.{section}[{index}]"
            role_raw = _text(row.get("role") or row.get("title") or row.get("name"))
            role, role_withheld = scrub_text(role_raw, names)
            organization = _text(row.get("organization") or row.get("org")) or None
            kind = "campus" if section == "campus_involvement" else "project" if section == "projects" else "employment"
            if re.search(r"\bintern(?:ship)?\b", role_raw, re.IGNORECASE):
                kind = "internship"
            elif organization and organization.casefold() in institutions and _COURSE_CODE.search(role_raw):
                kind = "academic_lab"
            interval, reason = _interval(row.get("dates") or row.get("date"))
            if not isinstance(raw, Mapping):
                reason = "unsupported experience entry shape"
                kind = "unknown"
            if reason:
                _warning(f"{path}: {reason}")
            guaranteed, occupied = _months(interval, cutoff)
            bullet_node = row.get("bullets")
            bullets: list[Bullet] = []
            for bullet_index, bullet_raw in _indexed(bullet_node):
                bullet_path = f"{path}.bullets[{bullet_index}]"
                bullet_text, bullet_withheld = scrub_text(_text(bullet_raw), names)
                bullets.append(Bullet(
                    f"exp-{len(experience) + 1:02d}-b{bullet_index + 1}", bullet_path,
                    _status(resume, node, raw, bullet_node, bullet_raw), bullet_text, bullet_withheld,
                ))
            experience.append(Experience(
                id=f"exp-{len(experience) + 1:02d}", source_path=path,
                fact_status=_status(resume, node, raw, row.get("role"), row.get("title"),
                                    row.get("name"), row.get("dates"), row.get("date")), kind=kind, role=role,
                role_withheld=role_withheld, organization=organization,
                organization_placeholder=organisations.get(organization.casefold(), "Organisation ?") if organization else "Organisation ?",
                interval=interval, months=occupied, guaranteed_months=guaranteed,
                current=bool(interval and interval.is_current), bullets=tuple(bullets), unknown_reason=reason,
            ))

    coursework: list[Coursework] = []
    for index, raw in _indexed(education_node):
        row = _mapping(raw)
        for name, path, status in _items(row.get("coursework"), f"resume.education[{index}].coursework", (resume, education_node, raw)):
            scrubbed, withheld = scrub_text(name, names)
            coursework.append(Coursework(f"cw-{len(coursework) + 1:03d}", path, status, scrubbed, f"edu-{index + 1:02d}", withheld))
    for name, path, status in _items(resume.get("coursework"), "resume.coursework", (resume,)):
        scrubbed, withheld = scrub_text(name, names)
        coursework.append(Coursework(f"cw-{len(coursework) + 1:03d}", path, status, scrubbed, None, withheld))

    credentials: list[Credential] = []
    for name, path, status in _items(resume.get("certifications"), "resume.certifications", (resume,)):
        scrubbed, withheld = scrub_text(name, names)
        credentials.append(Credential(f"cred-{len(credentials) + 1:02d}", path, status, scrubbed, withheld))

    skills: list[Skill] = []
    for key in ("technical_proficiencies", "skills", "tools", "technical_skills"):
        node = resume.get(key)
        for name, path, status in _items(node, f"resume.{key}", (resume, node)):
            scrubbed, withheld = scrub_text(name, names)
            supporting = tuple(b.id for exp in experience for b in exp.bullets
                               if b.fact_status == "confirmed" and b.withheld is None
                               and re.search(r"(?<!\w)" + re.escape(scrubbed) + r"(?!\w)", b.text, re.IGNORECASE)) if withheld is None else ()
            course_match = any(c.fact_status == "confirmed" and c.withheld is None and
                               re.search(r"(?<!\w)" + re.escape(scrubbed) + r"(?!\w)", c.name, re.IGNORECASE) for c in coursework) if withheld is None else False
            level = "bullet" if supporting else "coursework" if course_match else "listed"
            skills.append(Skill(f"skill-{len(skills) + 1:03d}", path, status, scrubbed, level, supporting, withheld))

    auth_node = constraints.get("work_authorization")
    auth_row = _mapping(auth_node)
    authorization = Authorization("auth", "constraints.work_authorization", _status(constraints, auth_node),
                                  auth_row.get("requires_sponsorship") if isinstance(auth_row.get("requires_sponsorship"), bool) else None,
                                  auth_row.get("authorized_to_work") if isinstance(auth_row.get("authorized_to_work"), bool) else None)
    availability = Availability("avail", "targeting.search.timing", timing_status,
                                _month(timing.available_from), _month(timing.available_until),
                                _month(timing.expected_graduation), timing.expected_graduation_confirmed,
                                timing.enrollment, _month(timing.reference_date))
    policy_node = _mapping(targeting.get("filters")).get("education_fit")
    policy = profile.filters.education_fit
    education_policy = EducationPolicy("edupol", "targeting.filters.education_fit",
                                       _status(targeting, targeting.get("filters"), policy_node),
                                       policy.holds, policy.in_progress, policy.kill_completed_degree)
    if policy.kill_completed_degree and any(row.status == "awarded" for row in education):
        raise ProfileInconsistent("kill_completed_degree contradicts an awarded education entry")
    if policy.holds in _RANK and not any(row.status == "awarded" and _RANK.get(row.degree_level, -1) >= _RANK[policy.holds] for row in education):
        raise ProfileInconsistent("holds has no awarded education entry at or above its level")
    facts = _facts(tuple(education), tuple(experience), tuple(skills), tuple(coursework),
                   tuple(credentials), authorization, availability, education_policy)
    compiled = CompiledProfile("qualify-profile-3", COMPILER_VERSION, "", synthetic is True,
                               tuple(education), tuple(experience), tuple(skills), tuple(coursework),
                               tuple(credentials), authorization, availability, education_policy,
                               names, facts)
    digest = hashlib.sha256(canonical_json(compiled).encode("utf-8")).hexdigest()[:12]
    return replace(compiled, profile_hash=digest)


def project(compiled: CompiledProfile, clauses: E7) -> tuple[StudentProjection, Mapping[str, Visibility]]:
    """Return only student-allowed fields and visibility for prefix/Judge A."""
    visibility: dict[str, Visibility] = {key: "withheld" if fact.withheld else "visible"
                                         for key, fact in compiled.facts_by_id.items()}
    education = tuple(StudentEducation(row.id, row.fact_status, row.degree_level,
                                        row.field_of_study, row.status, row.expected_completion,
                                        row.institution_placeholder,
                                        row.awarded_on[:4] if clauses.recency_clause and row.awarded_on and row.degree_level in {"bachelor", "master", "doctorate"} else None,
                                        row.gpa if clauses.gpa_clause else None)
                      for row in compiled.education)
    experience = tuple(StudentExperience(row.id, row.fact_status, row.role, row.kind,
                                         row.months, row.current, row.organization_placeholder,
                                         tuple(StudentBullet(b.id, b.fact_status, b.text) for b in row.bullets))
                       for row in compiled.experience)
    skills = tuple(StudentNamedFact(row.id, row.fact_status, row.name, row.evidence_level, row.supporting_ids)
                   for row in compiled.skills)
    coursework = tuple(StudentNamedFact(row.id, row.fact_status, row.name, education_id=row.education_id)
                       for row in compiled.coursework)
    credentials = tuple(StudentNamedFact(row.id, row.fact_status, row.name) for row in compiled.credentials)
    counts = WithheldCounts(
        bullets=sum(b.withheld is not None for row in compiled.experience for b in row.bullets),
        names=sum(row.withheld is not None for row in (*compiled.skills, *compiled.coursework, *compiled.credentials)),
        other=sum(row.withheld is not None for row in compiled.education) +
              sum(row.role_withheld is not None for row in compiled.experience),
    )
    projection = StudentProjection(
        "qualify-profile-3", education, experience, skills, coursework, credentials,
        StudentAuthorization("auth", compiled.authorization.fact_status,
                             compiled.authorization.requires_sponsorship, compiled.authorization.authorized_to_work),
        StudentAvailability("avail", compiled.availability.fact_status,
                            compiled.availability.available_from, compiled.availability.available_until,
                            compiled.availability.expected_graduation,
                            compiled.availability.expected_graduation_confirmed,
                            compiled.availability.enrollment),
        StudentEducationPolicy("edupol", compiled.education_policy.fact_status,
                               compiled.education_policy.holds, compiled.education_policy.in_progress,
                               compiled.education_policy.kill_completed_degree),
        counts,
    )
    return projection, MappingProxyType(visibility)
