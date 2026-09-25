"""Conservative duplicate recognition for stored Postings.

Identity is a source fact, not a guess from presentation fields.  Two records
are duplicates only when they share an exact canonical job destination or an
explicit requisition identifier.  A company and normalized title by
themselves are intentionally insufficient: one employer can advertise the
same title in several cities and under several requisitions.

The destination normalizer removes only well-known analytics parameters.  A
query parameter that may select a requisition, locale, or application route is
kept.  This lets an aggregator record and an employer record collapse when
they point at the same destination while preserving meaningful URL identity.

``duplicate_groups`` and ``duplicate_decisions`` keep the interface used by
``match.run``.  Their input is still the set of Postings that already passed
the fit filters; callers therefore cannot suppress an eligible copy merely
because a different location or requisition was ranked first.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DUPLICATE_RULE = "duplicate"
"""The ``rule`` a duplicate's Filter Decision is stamped with."""

LEGAL_SUFFIXES = frozenset({"inc", "llc", "ltd", "corp", "plc", "s.a"})
REMOTE_TERM = "remote"

# Punctuation trimmed from the ends of a title or a place component.  Interior
# punctuation remains meaningful: ``Scientist I`` and ``Scientist II`` are
# different titles, as are ``Engineer (Bioprocess)`` and ``Engineer``.
_EDGE_PUNCTUATION = " \t\r\n -–—,.;:/\\|·•*&_+()[]{}'\"“”‘’"
_COUNTY_SUFFIX = " county"

# Only parameters whose usual meaning is analytics/campaign attribution are
# removed.  In particular, ``ref``, ``source``, ``id``, ``job``, ``gh_jid`` and
# ``lever-origin`` remain because employers and aggregators may use them to
# select a requisition or application route.
_TRACKING_QUERY_EXACT = frozenset(
    {
        "_ga",
        "_gl",
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "li_fat_id",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "twclid",
        "vero_conv",
        "vero_id",
        "wbraid",
        "yclid",
    }
)

# ``external_id`` is a requisition identity on an employer ATS, but an
# aggregator's own row id when it comes from a feed.  Feed ids may differ even
# when the feed and employer destination are identical.  Explicit
# ``requisition_id`` fields are always authoritative.
_AGGREGATOR_SOURCES = frozenset(
    {"adzuna", "indeed", "linkedin", "ziprecruiter", "glassdoor", "monster"}
)


def _normalize(value: object) -> str:
    """NFKC, casefold, collapse whitespace."""
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_title(title: object) -> str:
    """Normalize only case, spacing, and edge punctuation in a title."""
    return _normalize(title).strip(_EDGE_PUNCTUATION)


def normalize_employer(name: object) -> str:
    """Normalize an employer and remove one unambiguous legal suffix."""
    normalized = _normalize(name).strip(_EDGE_PUNCTUATION)
    if not normalized:
        return ""
    head, _, last = normalized.rpartition(" ")
    if head and last in LEGAL_SUFFIXES:
        return head.strip(_EDGE_PUNCTUATION)
    return normalized


def employer_display(posting: Mapping[str, object], names: Mapping[str, str]) -> str | None:
    """Resolve a human employer name without treating a board slug as one."""
    company = posting.get("company")
    if isinstance(company, str) and company.strip():
        return company.strip()
    board = posting.get("board")
    if isinstance(board, str):
        display = names.get(board)
        if isinstance(display, str) and display.strip():
            return display.strip()
    return None


def _components(location: object) -> list[str]:
    return [
        stripped
        for component in _normalize(location).split(",")
        if (stripped := component.strip(_EDGE_PUNCTUATION))
    ]


def locality(location: object) -> str:
    """The leading locality of a comma-separated location."""
    parts = _components(location)
    return parts[0] if parts else ""


def enclosure(location: object) -> tuple[str, ...]:
    """The normalized region/country components around a locality."""
    return tuple(_components(location)[1:])


def is_remote(location: object) -> bool:
    """Whether the location is exactly a bare ``Remote`` locality."""
    return locality(location) == REMOTE_TERM


def _enclosures_agree(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    if not first or not second or first == second:
        return True
    return all(part.endswith(_COUNTY_SUFFIX) for part in first) or all(
        part.endswith(_COUNTY_SUFFIX) for part in second
    )


@dataclass(frozen=True)
class Place:
    """A normalized location kept for callers that use the old helper API."""

    stated: bool
    remote: bool
    locality: str
    enclosure: tuple[str, ...]

    def agrees_with(self, other: Place) -> bool:
        if not self.stated or not other.stated:
            return True
        if self.remote or other.remote:
            return True
        return self.locality == other.locality and _enclosures_agree(
            self.enclosure, other.enclosure
        )


def place(location: object) -> Place:
    parts = _components(location)
    if not parts:
        return Place(stated=False, remote=False, locality="", enclosure=())
    return Place(
        stated=True,
        remote=parts[0] == REMOTE_TERM,
        locality=parts[0],
        enclosure=tuple(parts[1:]),
    )


def compatible_locations(first: object, second: object) -> bool:
    """Whether two locations can describe one destination.

    This helper remains available for callers that need a conservative place
    comparison.  Duplicate identity itself does not use a title/company
    fallback or a location-only match; exact destination/requisition identity
    has priority over display formatting and multi-location presentation.
    """
    return place(first).agrees_with(place(second))


def _is_tracking_parameter(name: str) -> bool:
    lowered = name.strip().casefold()
    return lowered.startswith("utm_") or lowered in _TRACKING_QUERY_EXACT


def canonical_destination(value: object) -> str:
    """Canonicalize a job destination while preserving meaningful query data.

    Scheme and host case, default ports, fragments, and a trailing path slash
    do not identify a different HTTP destination.  Analytics parameters are
    removed by an explicit allowlist; every other query pair remains in its
    original order and value.

    Non-URL values are returned with only surrounding whitespace and Unicode
    normalization applied.  A caller can still use an opaque canonical
    destination supplied by a source worker without this function inventing a
    URL shape.
    """
    if not isinstance(value, str):
        return ""
    raw = unicodedata.normalize("NFKC", value).strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if not parsed.netloc and raw.startswith("//"):
        parsed = urlsplit(f"https:{raw}")
    if not parsed.netloc:
        return raw.rstrip("/") if raw != "/" else raw

    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname or ""
    host = hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        # An invalid port is still a meaningful opaque destination.  Returning
        # the normalized input is safer than dropping the port or raising from
        # a read-time dedup pass.
        return raw
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"
    if parsed.username is not None:
        # Credentials are not expected on job links, but preserve their exact
        # presence rather than silently changing an opaque destination.
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo += f":{parsed.password}"
        netloc = f"{userinfo}@{netloc}"

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query_pairs = [
        (name, item)
        for name, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_parameter(name)
    ]
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))



def _posting_field(posting: Mapping[str, object], name: str) -> object:
    value = posting.get(name)
    if value is not None:
        return value
    for container_name in ("source_metadata", "source_meta", "source_info", "identity"):
        container = posting.get(container_name)
        if isinstance(container, Mapping) and name in container:
            return container[name]
    return None


def _first_destination(posting: Mapping[str, object]) -> str:
    # Source-worker fields are preferred over the legacy ``url`` field.  All
    # aliases are accepted so a worker can expose the same identity without
    # forcing a store migration.
    for name in (
        "canonical_destination",
        "canonical_url",
        "application_url",
        "apply_url",
        "job_url",
        "url",
    ):
        value = _posting_field(posting, name)
        normalized = canonical_destination(value)
        if normalized:
            return normalized
    return ""


def _requisition(posting: Mapping[str, object]) -> tuple[str, str]:
    for name in (
        "requisition_id",
        "requisition",
        "req_id",
        "job_requisition_id",
        "external_id",
    ):
        value = _posting_field(posting, name)
        if isinstance(value, str) and value.strip():
            return _normalize(value).strip(_EDGE_PUNCTUATION), name
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value), name
    return "", ""


@dataclass(frozen=True)
class GroupMember:
    """One Posting with its exact identity and deterministic display rank."""

    key: str
    employer: str
    employer_key: str
    title: str
    title_key: str
    location: str
    place: Place
    source: str
    truncated: bool
    discovered: float | None
    destination: str = ""
    requisition: str = ""
    requisition_source: str = ""
    requisition_strong: bool = False
    board: str = ""
    listing_status: str = "unknown"

    @property
    def rank(self) -> tuple[int, int, int, float, str]:
        stamped = 0 if self.discovered is not None else 1
        availability = {"open": 0, "unknown": 1, "closed": 2}.get(self.listing_status, 1)
        return (availability, 1 if self.truncated else 0, stamped, self.discovered or 0.0, self.key)



@dataclass(frozen=True)
class DuplicateGroup:
    """One exact identity and its members, with the winner first."""

    winner: GroupMember
    losers: tuple[GroupMember, ...]


def _discovered_instant(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _member(posting: Mapping[str, object], names: Mapping[str, str]) -> GroupMember | None:
    key = posting.get("key")
    if not isinstance(key, str) or not key:
        return None
    destination = _first_destination(posting)
    requisition, requisition_source = _requisition(posting)
    # There is no safe fallback from an absent identity.  Company/title/location
    # are display facts, and using them to identify a job is the defect this
    # module is designed to prevent.
    if not destination and not requisition:
        return None
    employer = employer_display(posting, names) or ""
    employer_key = normalize_employer(employer)
    title = str(posting.get("title") or "").strip()
    location = str(posting.get("location") or "").strip()
    description_kind = posting.get("description_kind")
    truncated = bool(posting.get("snippet")) or (
        isinstance(description_kind, str) and description_kind.casefold() in {"snippet", "missing"}
    )
    source = posting.get("source")
    source_name = source.casefold() if isinstance(source, str) else ""
    return GroupMember(
        key=key,
        employer=employer,
        employer_key=employer_key,
        title=title,
        title_key=normalize_title(title),
        location=location,
        place=place(location),
        source=source if isinstance(source, str) else "",
        board=str(posting.get("board") or ""),
        listing_status=str(posting.get("listing_status") or "unknown"),
        truncated=truncated,
        discovered=_discovered_instant(posting.get("discovered_at")),
        destination=destination,
        requisition=requisition,
        requisition_source=requisition_source,
        requisition_strong=bool(requisition)
        and (
            requisition_source != "external_id"
            or source_name not in _AGGREGATOR_SOURCES
        ),
    )


def _same_identity(first: GroupMember, second: GroupMember) -> bool:
    """Whether two members share one exact identity without conflicting facts."""
    destination_same = bool(
        first.destination and second.destination and first.destination == second.destination
    )
    requisition_same = bool(
        first.requisition
        and second.requisition
        and first.requisition == second.requisition
        and (
            # Native ATS ids belong to that source and employer board. A bare
            # numeric id shared by unrelated boards is not a shared job.
            (first.requisition_source == second.requisition_source == "external_id"
             and first.source == second.source and first.board and first.board == second.board)
            or (first.employer_key and first.employer_key == second.employer_key
                and (first.requisition_source != "external_id" or second.requisition_source != "external_id"))
        )
    )

    # Two available strong identifiers that disagree are separate records even
    # if the other one happens to match.  This protects a generic application
    # URL from collapsing two explicitly different requisitions.
    if first.destination and second.destination and first.destination != second.destination:
        return False
    if (
        first.requisition
        and second.requisition
        and first.requisition != second.requisition
        and first.requisition_strong
        and second.requisition_strong
    ):
        return False
    return destination_same or requisition_same


def duplicate_groups(
    postings: Iterable[Mapping[str, object]], names: Mapping[str, str]
) -> list[DuplicateGroup]:
    """Find duplicate groups using exact destination/requisition identity.

    The input is expected to contain fit-filter survivors.  Members are sorted
    by information quality and discovery time, then a member joins a group
    only if it agrees with every existing member.  That prevents a chain such
    as ``destination A -> requisition X -> destination B`` from making one
    group out of two conflicting identities.
    """
    members = [
        member
        for posting in postings
        if (member := _member(posting, names)) is not None
    ]
    groups: list[list[GroupMember]] = []
    for member in sorted(members, key=lambda value: value.rank):
        for group in groups:
            if all(_same_identity(member, other) for other in group):
                group.append(member)
                break
        else:
            groups.append([member])
    return [
        DuplicateGroup(winner=group[0], losers=tuple(group[1:]))
        for group in groups
        if len(group) > 1
    ]


# Source names are kept in the Python layer because reasons are rendered here;
# unknown source identifiers are omitted rather than leaking board slugs.
SOURCE_NAMES = {
    "adzuna": "Adzuna",
    "ashby": "Ashby",
    "avature": "Avature",
    "greenhouse": "Greenhouse",
    "icims": "iCIMS",
    "lever": "Lever",
    "peopleclick": "PeopleClick",
    "smartrecruiters": "SmartRecruiters",
    "talentbrew": "TalentBrew",
    "workable": "Workable",
    "workday": "Workday",
}


def _source_phrase(source: str) -> str:
    display = SOURCE_NAMES.get(source)
    return f" from {display}" if display else ""


def duplicate_reason(winner: GroupMember) -> str:
    """Describe the winner without exposing a source key or board slug."""
    if winner.employer:
        return (
            f"the same role as {winner.title!r} at {winner.employer}, "
            f"already discovered{_source_phrase(winner.source)}"
        )
    return (
        f"the same job destination as {winner.title!r}, "
        f"already discovered{_source_phrase(winner.source)}"
    )


def duplicate_decisions(
    postings: Iterable[Mapping[str, object]], names: Mapping[str, str]
) -> dict[str, str]:
    """Return loser key -> readable reason for exact duplicate groups."""
    return {
        loser.key: duplicate_reason(group.winner)
        for group in duplicate_groups(postings, names)
        for loser in group.losers
    }


__all__ = [
    "DUPLICATE_RULE",
    "DuplicateGroup",
    "GroupMember",
    "Place",
    "SOURCE_NAMES",
    "canonical_destination",
    "compatible_locations",
    "duplicate_decisions",
    "duplicate_groups",
    "duplicate_reason",
    "employer_display",
    "enclosure",
    "is_remote",
    "locality",
    "normalize_employer",
    "normalize_title",
    "place",
]
