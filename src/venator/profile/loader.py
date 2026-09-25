"""Load and validate a Profile directory into the schema dataclasses.

Validation is explicit rather than schema-library-driven: every rejection names
the file and the dotted field it came from, because the person editing these
files is the Owner, not a developer with a stack trace.
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence, overload

import yaml

from venator.profile.schema import (
    DEGREE_LEVELS,
    JEV_DOMAIN_TOKENS,
    JEV_EXCLUDE_OR_REVIEW,
    JEV_POLICY_VERSION,
    JEV_RETAIN_OR_REVIEW,
    REMOTE_PREFERENCES,
    SHIFT_PREFERENCES,
    BoardRegistry,
    EducationFitPolicy,
    FilterPolicy,
    JevPolicy,
    Matcher,
    Profile,
    RoleLevel,
    RoleTargetPolicy,
    SearchTargeting,
    TimingPolicy,
    WorkAuthorizationPolicy,
)

PROFILE_FILES = ("resume.yaml", "constraints.yaml", "targeting.yaml")
KNOWN_RULES = ("work_authorization", "education_fit", "role_target", "eligibility")


class ProfileError(ValueError):
    """A Profile file is unreadable, malformed, or contradicts the schema."""


class _Field:
    """A dotted field position inside one Profile file, for error messages."""

    def __init__(self, source: Path | str, path: str = "") -> None:
        self.source = source
        self.path = path

    def child(self, name: str) -> _Field:
        return _Field(self.source, f"{self.path}.{name}" if self.path else name)

    def reject(self, expectation: str, value: object = None) -> ProfileError:
        where = f"{self.source}: {self.path}" if self.path else str(self.source)
        seen = "" if value is None else f" (got {value!r})"
        return ProfileError(f"{where} {expectation}{seen}")


def _read_yaml(path: Path) -> dict:
    """Read one Profile file. A missing or empty file is an empty mapping."""
    if not path.exists():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ProfileError(f"{path} is not valid YAML: {error}") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProfileError(f"{path} must contain a YAML mapping, not a {type(value).__name__}")
    return value


def _mapping(value: object, at: _Field) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise at.reject("must be a mapping", value)
    return value


def _strings(value: object, at: _Field) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise at.reject("must be a list of strings", value)
    items = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise at.child(str(index)).reject("must be a non-empty string", item)
        items.append(item.strip())
    return tuple(items)


def _text(value: object, at: _Field, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise at.reject("must be a string", value)
    return value


def _date(value: object, at: _Field) -> date | None:
    """Read an ISO date from YAML's string or native date representation."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw:
            try:
                # ``fromisoformat`` accepts date-times too; a profile only
                # needs the calendar date for eligibility comparisons.
                return date.fromisoformat(raw[:10])
            except ValueError:
                pass
    raise at.reject("must be an ISO date (YYYY-MM-DD)", value)


def _identifier(value: object, at: _Field) -> str:
    """An opaque Profile identifier, which YAML may hand back as a number.

    ``id: 4815162342`` is a string to the person who wrote it and an ``int`` to
    the parser, and a hex identifier that happens to draw no letters parses the
    same way. Both are the value they look like, so they are accepted and
    stringified rather than rejected over how YAML happened to type them.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _text(value, at)


def _flag(value: object, at: _Field, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise at.reject("must be true or false", value)
    return value


@overload
def _whole_number(value: object, at: _Field, default: int) -> int: ...


@overload
def _whole_number(value: object, at: _Field, default: None = None) -> int | None: ...


def _whole_number(value: object, at: _Field, default: int | None = None) -> int | None:
    """A whole number, or ``default`` — which is only ``None`` when it is asked for."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise at.reject("must be a whole number of 0 or more", value)
    return value


def _alternation(terms: Sequence[str]) -> str:
    """Join plain words into one alternation, each matched literally."""
    return "|".join(re.escape(term) for term in terms)


def _compile(alternatives: Sequence[str], at: _Field, *, word_bounded: bool) -> re.Pattern[str] | None:
    """Compile one alternation.

    ``terms`` are documented as plain words, so they are escaped and matched
    literally between word boundaries: a term of ``.*`` matches the two
    characters, not everything, and a place name holding ``(`` or ``[`` is a
    place name rather than a syntax error. Only the fields whose names end in
    ``_patterns`` are compiled verbatim — those are documented as regular
    expressions, and the Owner writes them knowing it.
    """
    if not alternatives:
        return None
    if word_bounded:
        expression = rf"\b(?:{_alternation(alternatives)})\b"
    else:
        expression = "(?:{})".format("|".join(alternatives))
    try:
        return re.compile(expression, re.IGNORECASE)
    except re.error as error:
        raise at.reject(f"is not a valid regular expression: {error}") from error


def _combine(label: str, *compiled: re.Pattern[str] | None) -> Matcher:
    """One Matcher over every group that was configured, or none at all."""
    parts = [str(part.pattern) for part in compiled if part is not None]
    if not parts:
        return Matcher(label)
    return Matcher(label, re.compile("|".join(parts), re.IGNORECASE))


def _words(value: object, at: _Field, key: str) -> re.Pattern[str] | None:
    """Compile a list of plain words, matched literally between word boundaries."""
    return _compile(_strings(value, at.child(key)), at.child(key), word_bounded=True)


def _expressions(value: object, at: _Field, key: str) -> re.Pattern[str] | None:
    """Compile a list of regular expressions, verbatim."""
    return _compile(_strings(value, at.child(key)), at.child(key), word_bounded=False)


def _matcher(value: object, at: _Field, *, default_label: str) -> Matcher:
    """Build a Matcher from ``terms`` (word-bounded) and/or ``patterns`` (verbatim)."""
    config = _mapping(value, at)
    label = _text(config.get("label"), at.child("label"), default_label)
    return _combine(
        label,
        _words(config.get("terms"), at, "terms"),
        _expressions(config.get("patterns"), at, "patterns"),
    )


def _pattern_list(value: object, at: _Field, key: str) -> tuple[re.Pattern[str], ...]:
    """Compile a list of regular expressions one at a time, naming the bad one."""
    compiled = []
    for index, pattern in enumerate(_strings(value, at.child(key))):
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as error:
            raise at.child(key).child(str(index)).reject(
                f"is not a valid regular expression: {error}"
            ) from error
    return tuple(compiled)


def _work_authorization_policy(value: object, at: _Field) -> WorkAuthorizationPolicy:
    config = _mapping(value, at)
    return WorkAuthorizationPolicy(
        restrictions=_pattern_list(config.get("restriction_patterns"), at, "restriction_patterns"),
        noted=_pattern_list(config.get("noted_patterns"), at, "noted_patterns"),
        noted_label=_text(
            config.get("noted_label"),
            at.child("noted_label"),
            "wording the Profile passes deliberately",
        ),
        pass_reason=_text(
            config.get("pass_reason"),
            at.child("pass_reason"),
            "no explicit work-authorization restriction",
        ),
    )


def _degree_level(value: object, at: _Field) -> str | None:
    """One rung of ``DEGREE_LEVELS``, or ``None`` for a Profile that says nothing.

    Named rungs rather than free text, because the filter compares them: a value
    it cannot place on the ladder is a value it cannot decide with, and silently
    treating it as "nothing configured" would be a Profile whose education rule
    quietly stopped running.
    """
    if value is None:
        return None
    if not isinstance(value, str) or value.strip().lower() not in DEGREE_LEVELS:
        raise at.reject(f"must be one of {', '.join(DEGREE_LEVELS)}", value)
    return value.strip().lower()


def _education_fit_policy(value: object, at: _Field) -> EducationFitPolicy:
    config = _mapping(value, at)
    kill_years = _whole_number(config.get("experience_years_kill"), at.child("experience_years_kill"))
    implausible = _whole_number(
        config.get("experience_years_implausible"), at.child("experience_years_implausible"), 15
    )
    if kill_years is not None and implausible is not None and kill_years > implausible:
        raise at.child("experience_years_kill").reject(
            f"must not exceed experience_years_implausible ({implausible})", kill_years
        )
    kill_completed = _flag(config.get("kill_completed_degree"), at.child("kill_completed_degree"))
    holds = _degree_level(config.get("holds"), at.child("holds"))
    in_progress = _degree_level(config.get("in_progress"), at.child("in_progress"))
    # Two models that say incompatible things about the same Owner, so the
    # Profile picks one rather than this code guessing which it meant.
    # ``kill_completed_degree: true`` says the Owner holds no completed degree;
    # ``holds:`` says which one they hold. ``false`` is the default and states
    # nothing, so it sits beside the widened model without conflict.
    if kill_completed and (holds is not None or in_progress is not None):
        raise at.child("kill_completed_degree").reject(
            "cannot be true beside holds or in_progress — kill_completed_degree says the Owner "
            "has completed no degree, and holds/in_progress say which qualification they have; "
            "set one model or the other",
            True,
        )
    if holds is not None and in_progress is not None:
        if DEGREE_LEVELS.index(in_progress) < DEGREE_LEVELS.index(holds):
            raise at.child("in_progress").reject(
                f"must not be below holds ({holds}) — a qualification in progress is one the "
                "Owner does not have yet",
                in_progress,
            )
    return EducationFitPolicy(
        kill_completed_degree=kill_completed,
        holds=holds,
        in_progress=in_progress,
        experience_years_kill=kill_years,
        experience_years_implausible=implausible,
    )


def _role_target_policy(value: object, at: _Field) -> RoleTargetPolicy:
    """The seniority ladder, the rungs the Profile accepts, and excluded functions.

    One contradiction is refused here, and the line it is drawn on matters more
    than the check. Absent means "not configured" everywhere else in this loader,
    and a stage must never refuse to run because a Profile left a field out — so
    a Profile with no ``role_target`` block at all, or one that names excluded
    functions and no ladder, loads and kills nothing on seniority, exactly as
    before. What is refused is narrower: a Profile that *writes* ``levels:`` and
    then accepts no rung of it. Writing a ladder is a positive statement, and
    "here is my ladder, I will take nothing on it" is not an omission — it is two
    statements that contradict each other, and almost always a list that was
    lost in an edit.

    The refusal keys on ``levels`` and ``accept`` alone, never on ``exclude``.
    ``exclude`` names functions outside the search at any rung and is read
    whether or not a ladder exists, so letting it decide whether the same broken
    ladder is legal would make the verdict on a Profile turn on an unrelated
    field. The contradiction is between the two fields that describe the ladder.
    """
    config = _mapping(value, at)
    levels_at = at.child("levels")
    raw = config.get("levels")
    if raw is not None and (isinstance(raw, str) or not isinstance(raw, Sequence)):
        raise levels_at.reject("must be a list of rungs, lowest first", raw)
    levels: list[RoleLevel] = []
    for index, entry in enumerate(raw or ()):
        rung_at = levels_at.child(str(index))
        rung = _mapping(entry, rung_at)
        name = _text(rung.get("name"), rung_at.child("name")).strip()
        if not name:
            raise rung_at.child("name").reject("must name this rung of the ladder", rung.get("name"))
        if any(level.name == name for level in levels):
            raise rung_at.child("name").reject("is already a rung on this ladder", name)
        levels.append(
            RoleLevel(
                name=name,
                titles=_combine(
                    name,
                    _words(rung.get("title_terms"), rung_at, "title_terms"),
                    _expressions(rung.get("title_patterns"), rung_at, "title_patterns"),
                ),
            )
        )
    accept = _strings(config.get("accept"), at.child("accept"))
    known = [level.name for level in levels]
    for index, name in enumerate(accept):
        if name not in known:
            raise at.child("accept").child(str(index)).reject(
                f"must name a rung of levels — one of {known}", name
            )
    if levels and not accept:
        raise at.child("accept").reject(
            f"must name at least one rung of levels — one of {known}. A ladder the "
            "Profile accepts no rung of contradicts itself: it would place every "
            "Posting outside the band it never declared. To target no seniority at "
            "all, omit levels as well as accept",
            config.get("accept"),
        )
    return RoleTargetPolicy(
        label=_text(config.get("label"), at.child("label"), "role target"),
        levels=tuple(levels),
        accept=accept,
        exclude=_matcher(config.get("exclude"), at.child("exclude"), default_label="an excluded function"),
    )


_JEV_FIELDS = (
    "policy_version",
    "restricted_roles",
    "temporary_student_authorization_exclusion",
    "no_sponsorship_student",
    "no_sponsorship_nonstudent",
    "unmet_completed_degree",
    "domains",
)


def _closed(value: object, at: _Field, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise at.reject(f"must be one of {list(allowed)}", value)
    return str(value)


def _jev_policy(value: object, at: _Field) -> JevPolicy | None:
    if value is None:
        return None
    config = _mapping(value, at)
    for key in config:
        if key not in _JEV_FIELDS:
            raise at.child(key).reject("is not a Jev policy field", config[key])
    for name in _JEV_FIELDS:
        if name not in config:
            raise at.child(name).reject("is required when filters.jev is present")
    version = _text(config.get("policy_version"), at.child("policy_version"))
    if not JEV_POLICY_VERSION.fullmatch(version):
        raise at.child("policy_version").reject("must match [A-Za-z0-9._-]{1,64}", version)
    raw_domains = config.get("domains")
    if not isinstance(raw_domains, list) or not raw_domains:
        raise at.child("domains").reject("must be a nonempty list of domain tokens", raw_domains)
    tokens: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_domains):
        token_at = at.child("domains").child(str(index))
        if not isinstance(item, str):
            raise token_at.reject("must be a domain token", item)
        if item not in JEV_DOMAIN_TOKENS:
            raise token_at.reject(f"must be one of {list(JEV_DOMAIN_TOKENS)}", item)
        if item in seen:
            raise token_at.reject("must not repeat a domain token", item)
        seen.add(item)
        tokens.append(item)
    return JevPolicy(
        policy_version=version,
        restricted_roles=_closed(
            config.get("restricted_roles"), at.child("restricted_roles"), JEV_EXCLUDE_OR_REVIEW,
        ),
        temporary_student_authorization_exclusion=_closed(
            config.get("temporary_student_authorization_exclusion"),
            at.child("temporary_student_authorization_exclusion"), JEV_EXCLUDE_OR_REVIEW,
        ),
        no_sponsorship_student=_closed(
            config.get("no_sponsorship_student"), at.child("no_sponsorship_student"),
            JEV_RETAIN_OR_REVIEW,
        ),
        no_sponsorship_nonstudent=_closed(
            config.get("no_sponsorship_nonstudent"), at.child("no_sponsorship_nonstudent"),
            JEV_EXCLUDE_OR_REVIEW,
        ),
        unmet_completed_degree=_closed(
            config.get("unmet_completed_degree"), at.child("unmet_completed_degree"),
            JEV_EXCLUDE_OR_REVIEW,
        ),
        domains=tuple(tokens),
    )


def _filter_policy(value: object, at: _Field, *, search: SearchTargeting | None = None) -> FilterPolicy:
    config = _mapping(value, at)
    qualification_mode = config.get("qualification_mode", "deterministic")
    if qualification_mode not in ("deterministic", "jev"):
        raise at.child("qualification_mode").reject(
            "must be deterministic or jev", qualification_mode
        )
    enabled = _strings(config.get("enabled"), at.child("enabled"))
    for index, rule in enumerate(enabled):
        if rule not in KNOWN_RULES:
            raise at.child("enabled").child(str(index)).reject(
                f"must name a Hard Filter — one of {list(KNOWN_RULES)}", rule
            )
    work_authorization = _work_authorization_policy(
        config.get("work_authorization"), at.child("work_authorization")
    )
    education_fit = _education_fit_policy(config.get("education_fit"), at.child("education_fit"))
    role_target = _role_target_policy(config.get("role_target"), at.child("role_target"))
    jev = _jev_policy(config.get("jev"), at.child("jev"))
    if qualification_mode == "jev" and jev is None:
        raise at.child("jev").reject("is required when qualification_mode is jev")
    shift_preference = config.get("shift_preference", "no_preference")
    if shift_preference not in SHIFT_PREFERENCES:
        raise at.child("shift_preference").reject(
            f"must be one of {list(SHIFT_PREFERENCES)}", shift_preference
        )
    # Writing a policy and forgetting to enable it is the one failure that fails
    # open: every Hard Filter stays off and every Posting is reported as passing.
    configured = [rule for rule in KNOWN_RULES if config.get(rule) is not None]
    if configured and not enabled:
        raise at.child("enabled").reject(
            "is required once a filter policy is written — this Profile configures "
            f"{', '.join(configured)} but enables no Hard Filter, so every Posting would "
            "pass unfiltered; list the Hard Filters to run, in the order they run"
        )
    return FilterPolicy(
        enabled=enabled,
        qualification_mode=qualification_mode,
        work_authorization=work_authorization,
        education_fit=education_fit,
        role_target=role_target,
        search=search or SearchTargeting(),
        jev=jev,
        shift_preference=shift_preference,
    )


_SEASONS = ("spring", "summer", "fall", "autumn", "winter")
_ENROLLMENT_STATES = ("current", "future", "either", "unknown")


def _timing_policy(value: object, at: _Field) -> TimingPolicy:
    """Load explicit availability and education timing without inventing dates."""
    config = _mapping(value, at)
    months_value = config.get("allowed_months")
    months = _whole_number_list(months_value, at, "allowed_months")
    for index, month in enumerate(months):
        if not 1 <= month <= 12:
            raise at.child("allowed_months").child(str(index)).reject(
                "must be a month number from 1 through 12", month
            )

    years = _whole_number_list(config.get("allowed_years"), at, "allowed_years")
    for index, year in enumerate(years):
        if not 1 <= year <= 9999:
            raise at.child("allowed_years").child(str(index)).reject(
                "must be a calendar year from 1 through 9999", year
            )

    seasons = tuple(
        season.casefold()
        for season in _strings(
            config.get("allowed_seasons"), at.child("allowed_seasons")
        )
    )
    for index, season in enumerate(seasons):
        if season not in _SEASONS:
            raise at.child("allowed_seasons").child(str(index)).reject(
                f"must be one of {list(_SEASONS)}", season
            )

    enrollment = config.get("enrollment")
    if enrollment is not None:
        enrollment = _text(enrollment, at.child("enrollment")).strip().casefold()
        if enrollment not in _ENROLLMENT_STATES:
            raise at.child("enrollment").reject(
                f"must be one of {list(_ENROLLMENT_STATES)}", enrollment
            )

    confirmed = _flag(
        config.get("expected_graduation_confirmed"),
        at.child("expected_graduation_confirmed"),
    )
    expected_value = config.get("expected_graduation")
    precision = "day"
    if isinstance(expected_value, str) and re.fullmatch(r"\d{4}-\d{2}", expected_value.strip()):
        year, month = (int(part) for part in expected_value.strip().split("-"))
        if not 1 <= month <= 12:
            raise at.child("expected_graduation").reject("must use a month from 01 through 12", expected_value)
        try:
            expected = date(year, month, 1)
        except ValueError as error:
            raise at.child("expected_graduation").reject("must use a valid year and month", expected_value) from error
        precision = "month"
    else:
        expected = _date(expected_value, at.child("expected_graduation"))
    requested_precision = config.get("expected_graduation_precision")
    if requested_precision is not None:
        requested_precision = _text(
            requested_precision, at.child("expected_graduation_precision")
        ).strip().casefold()
        if requested_precision not in {"day", "month"}:
            raise at.child("expected_graduation_precision").reject(
                "must be day or month", requested_precision
            )
        precision = requested_precision
    if confirmed and expected is None:
        raise at.child("expected_graduation").reject(
            "is required when expected_graduation_confirmed is true"
        )

    return TimingPolicy(
        available_from=_date(config.get("available_from"), at.child("available_from")),
        available_until=_date(config.get("available_until"), at.child("available_until")),
        allowed_months=months,
        allowed_seasons=seasons,
        allowed_years=years,
        expected_graduation=expected,
        expected_graduation_confirmed=confirmed,
        expected_graduation_precision=precision,
        enrollment=enrollment,
        reference_date=_date(config.get("reference_date"), at.child("reference_date")),
    )


def _whole_number_list(value: object, at: _Field, key: str) -> tuple[int, ...]:
    """A list of non-negative integers, with errors located at each item."""
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise at.child(key).reject("must be a list of whole numbers", value)
    values = []
    for index, item in enumerate(value):
        number = _whole_number(item, at.child(key).child(str(index)))
        if number is None:
            raise at.child(key).child(str(index)).reject("must be a whole number", item)
        values.append(number)
    return tuple(values)


def _salary_floor(value: object, at: _Field) -> tuple[int | None, str | None, str | None]:
    """Read a numeric floor while retaining optional currency and pay period."""
    if isinstance(value, Mapping):
        config = _mapping(value, at)
        amount = _whole_number(config.get("amount"), at.child("amount"))
        currency = _text(config.get("currency"), at.child("currency")).strip().upper() or None
        period = _text(config.get("period"), at.child("period")).strip().casefold() or None
        return amount, currency, period
    return _whole_number(value, at), None, None


def _search_targeting(value: object, at: _Field) -> SearchTargeting:
    config = _mapping(value, at)
    remote = config.get("remote")
    if remote is not None and remote not in REMOTE_PREFERENCES:
        raise at.child("remote").reject(f"must be one of {list(REMOTE_PREFERENCES)}", remote)
    timing = _timing_policy(config.get("timing"), at.child("timing"))
    salary_floor, salary_currency, salary_period = _salary_floor(
        config.get("salary_floor"), at.child("salary_floor")
    )
    # Scalar floors retain the existing shape; mapping floors can also provide
    # explicit top-level values, which are useful when onboarding writes the
    # fields separately.
    salary_currency = (
        _text(config.get("salary_currency"), at.child("salary_currency")).strip().upper()
        if config.get("salary_currency") is not None
        else salary_currency
    ) or None
    salary_period = (
        _text(config.get("salary_period"), at.child("salary_period")).strip().casefold()
        if config.get("salary_period") is not None
        else salary_period
    ) or None
    return SearchTargeting(
        queries=_strings(config.get("queries"), at.child("queries")),
        locations=_strings(config.get("locations"), at.child("locations")),
        keywords=_strings(config.get("keywords"), at.child("keywords")),
        seniority=_strings(config.get("seniority"), at.child("seniority")),
        industries=_strings(config.get("industries"), at.child("industries")),
        remote=remote,
        salary_floor=salary_floor,
        salary_currency=salary_currency,
        salary_period=salary_period,
        timing=timing,
    )


def _board_registry(value: object, at: _Field) -> BoardRegistry:
    config = _mapping(value, at)
    names_at = at.child("names")
    names = {}
    for token, display in _mapping(config.get("names"), names_at).items():
        if not isinstance(display, str) or not display.strip():
            raise names_at.child(str(token)).reject("must be an employer display name", display)
        names[str(token)] = display.strip()
    boards_at = at.child("boards")
    boards = {
        str(source): _strings(tokens, boards_at.child(str(source)))
        for source, tokens in _mapping(config.get("boards"), boards_at).items()
    }
    # A half-written registry is the one failure here that fails silently, in
    # the direction that costs a real opening. Dedup resolves an ATS Posting's
    # employer through `names` alone, so a board with no entry has no employer
    # and its Postings join no duplicate group at all — and because location
    # compatibility is deliberately non-transitive, a member dropping out of the
    # greedy pass changes which of the *remaining* Postings merge. Randomized
    # replay of the pass puts that at ~4% of corpora containing an unnamed
    # board: total kills never rise, but a Posting a complete registry would
    # have kept is killed as a duplicate instead of the one that really was one.
    # The Owner is shown a queue that is the right length and missing a real Posting.
    #
    # An empty `names` is left alone: a Profile that registers boards and names
    # none of them has not configured Dedup's employer resolution, and absent
    # means "not configured", not "invalid". This is the same shape as the
    # `filters.enabled` rule above — writing half of a block is the error, and
    # writing none of it is a legitimate state.
    registered = {token for tokens in boards.values() for token in tokens}
    if names:
        unnamed = sorted(registered - set(names))
        if unnamed:
            raise names_at.reject(
                "must name every board this Profile registers — "
                f"{unnamed} appear under sources.boards with no entry here, and Dedup "
                "resolves an ATS Posting's employer through this map alone, so their "
                "Postings would join no duplicate group and silently change which "
                "other Postings are killed as duplicates; give each one its employer "
                "display name"
            )
    elif registered:
        # The legitimate-but-degraded state, said out loud. The error above only
        # catches a registry someone started and fell behind on; a registry with
        # no `names` at all is the shape the measurement actually used, and it
        # carries exactly the same harm — every ATS Posting drops out of every
        # duplicate group, and because location compatibility is non-transitive,
        # a member falling out changes which of the *remaining* Postings merge.
        # It stays a warning because absent means "not configured", never
        # "invalid", and refusing to run over a missing field is the one thing
        # this loader must not do. stderr, so a stage writing JSON to stdout
        # keeps writing JSON.
        boards_word = "board" if len(registered) == 1 else "boards"
        print(
            f"warning: {at.source} registers {len(registered)} {boards_word} under "
            f"sources.boards and names none of them under sources.names, so Dedup "
            f"cannot resolve the employer of any Posting from "
            f"{'it' if len(registered) == 1 else 'them'}. Those Postings join no "
            "duplicate group, and because a member falling out changes which of the "
            "rest merge, that can kill a different Posting than it should. Give each "
            "board its employer display name under sources.names.",
            file=sys.stderr,
            flush=True,
        )
    return BoardRegistry(names=names, boards=boards)


def _refuse_retired_judge_keys(targeting: Mapping[str, object], at: _Field) -> None:
    """Decision 17 retired the judge; leftover scoring keys are refused, not ignored."""
    if "scoring" in targeting:
        raise at.child("scoring").reject(
            "is retired (decision 17); delete the scoring: block"
        )
    match = targeting.get("match")
    if isinstance(match, Mapping) and "threshold" in match:
        raise at.child("match").child("threshold").reject(
            "is retired (decision 17); delete match.threshold"
        )
    if "match" in targeting:
        raise at.child("match").reject(
            "is retired (decision 17); delete the match: block"
        )


def load_profile(directory: Path) -> Profile:
    """Load one Profile directory. Missing files load as empty, not as errors."""
    directory = Path(directory)
    if directory.exists() and not directory.is_dir():
        raise ProfileError(f"{directory} is not a Profile directory")
    if not directory.exists():
        raise ProfileError(
            f"no Profile at {directory} — expected a directory holding {', '.join(PROFILE_FILES)}"
        )

    resume = _read_yaml(directory / "resume.yaml")
    constraints = _read_yaml(directory / "constraints.yaml")
    targeting_path = directory / "targeting.yaml"
    targeting = _read_yaml(targeting_path)
    at = _Field(targeting_path)
    _refuse_retired_judge_keys(targeting, at)

    identity = _mapping(targeting.get("profile"), at.child("profile"))
    search = _search_targeting(targeting.get("search"), at.child("search"))
    filters = _filter_policy(targeting.get("filters"), at.child("filters"), search=search)
    education_facts = constraints.get("education_fit")
    if isinstance(education_facts, Mapping):
        stated = str(education_facts.get("is") or "").strip().casefold()
        if re.match(r"^undergraduate\b", stated):
            filters = replace(filters, education_fit=replace(filters.education_fit, current_student_level="undergraduate"))
    authorization = constraints.get("work_authorization")
    if isinstance(authorization, Mapping):
        exclusions = authorization.get("hard_kill", ())
        if isinstance(exclusions, list) and any(
            isinstance(value, str) and re.search(r"export[- ]control", value, re.I) for value in exclusions
        ):
            # Read an explicit policy already supplied by the owner, rather than
            # guessing immigration eligibility from a name or nationality.
            us_person = re.compile(
                r"\bonly\s+individuals\b.{0,120}\bu\.?s\.?\s+persons\b.{0,120}\beligible\b",
                re.I,
            )
            filters = replace(filters, work_authorization=replace(
                filters.work_authorization, restrictions=(*filters.work_authorization.restrictions, us_person)
            ))
    # Timing and pay are explicit search preferences rather than a new Profile
    # file.  Make them part of the deterministic filter pass even when an older
    # targeting file listed only the original four rules.
    if search.eligibility_configured and "eligibility" not in filters.enabled:
        filters = replace(filters, enabled=(*filters.enabled, "eligibility"))
    return Profile(
        name=_text(identity.get("name"), at.child("profile").child("name"), directory.name),
        directory=directory,
        id=_identifier(identity.get("id"), at.child("profile").child("id")),
        scaffold=_flag(identity.get("scaffold"), at.child("profile").child("scaffold")),
        resume=resume,
        constraints=constraints,
        targeting=targeting,
        search=search,
        sources=_board_registry(targeting.get("sources"), at.child("sources")),
        filters=filters,
    )
