"""Shared normalization helpers for discovery adapters.

The public boards use slightly different names for the same facts.  Keeping
the small amount of shape handling here gives every adapter the same output
contract while still retaining the source record alongside the normalized
fields.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from typing import Any


from venator.countries import country_code


# State/province abbreviations are useful as regions when a source only gives
# a human-readable location.  Full names are left as-is, which is better than
# pretending they are a code we did not receive.
STATE_CODES = {
    "al": "AL",
    "ak": "AK",
    "az": "AZ",
    "ar": "AR",
    "ca": "CA",
    "co": "CO",
    "ct": "CT",
    "de": "DE",
    "fl": "FL",
    "ga": "GA",
    "hi": "HI",
    "id": "ID",
    "il": "IL",
    "in": "IN",
    "ia": "IA",
    "ks": "KS",
    "ky": "KY",
    "la": "LA",
    "me": "ME",
    "md": "MD",
    "ma": "MA",
    "mi": "MI",
    "mn": "MN",
    "ms": "MS",
    "mo": "MO",
    "mt": "MT",
    "ne": "NE",
    "nv": "NV",
    "nh": "NH",
    "nj": "NJ",
    "nm": "NM",
    "ny": "NY",
    "nc": "NC",
    "nd": "ND",
    "oh": "OH",
    "ok": "OK",
    "or": "OR",
    "pa": "PA",
    "ri": "RI",
    "sc": "SC",
    "sd": "SD",
    "tn": "TN",
    "tx": "TX",
    "ut": "UT",
    "vt": "VT",
    "va": "VA",
    "wa": "WA",
    "wv": "WV",
    "wi": "WI",
    "wy": "WY",
    "dc": "DC",
}


def text(value: object) -> str:
    """Return a trimmed string, including a useful value from a small mapping."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def first_text(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("descriptor") or value.get("code")
        result = text(value)
        if result:
            return result
    return ""


def country_iso2(value: object) -> str:
    """Normalize an explicit source country through the shared ISO registry."""
    return country_code(value) or ""


def _location_name(value: Mapping[str, Any]) -> str:
    return first_text(
        value,
        "name",
        "display_name",
        "displayName",
        "location",
        "locationName",
        "text",
        "addressLocality",
    )


def _country_component(value: str, explicit_country: str) -> str:
    code = country_iso2(value)
    # Georgia and several ISO codes also name US states. Display wording
    # alone cannot settle that ambiguity; an explicit country field can.
    ambiguous = value.casefold() in STATE_CODES or value.casefold() == "georgia"
    return code if not ambiguous or explicit_country == code else ""


def _location_parts(name: str, country: str, region: str, city: str) -> dict[str, str]:
    """Fill missing city/region/country pieces from a conventional name."""

    clean_name = text(name)
    clean_country = country_iso2(country)
    clean_region = text(region)
    clean_city = text(city)

    # Workday commonly writes ``US - Cambridge, MA`` and a few ATS sources
    # write ``Remote, US``.  Split the prefix only when it is a recognized
    # country; otherwise punctuation is handled below as city/region wording.
    country_prefix = re.match(r"^\s*([A-Za-z .-]{2,30})\s*[-–—:]\s*(.+)$", clean_name)
    if country_prefix:
        possible = _country_component(country_prefix.group(1).strip(), clean_country)
        if possible:
            clean_country = clean_country or possible
            clean_name = country_prefix.group(2).strip()

    if not clean_city and clean_name:
        pieces = [part.strip() for part in clean_name.split(",") if part.strip()]
        if len(pieces) >= 2:
            # Some Workday tenants publish country first: Malaysia, Petaling
            # Jaya. Recognize the explicit country, never infer one from a city.
            leading_country = _country_component(pieces[0], clean_country)
            if leading_country and (not clean_country or leading_country == clean_country):
                clean_country = leading_country
                pieces = pieces[1:]
            clean_city = pieces[0]
            if not clean_region and len(pieces) >= 2:
                clean_region = pieces[1]
            if not clean_country and len(pieces) >= 3:
                clean_country = country_iso2(pieces[-1])
        elif len(pieces) == 1:
            # A one-word location can be a city or a country.  Prefer country
            # when it is unambiguous; otherwise retain it as the city.
            maybe_country = _country_component(pieces[0], clean_country)
            if maybe_country:
                clean_country = clean_country or maybe_country
            else:
                clean_city = pieces[0]

    # Do not turn arbitrary two-letter city names into regions.  A region code
    # is useful when it is one of the well-known US state/DC abbreviations.
    if clean_region.casefold() in STATE_CODES:
        clean_region = STATE_CODES[clean_region.casefold()]
    return {
        "name": text(name),
        "country": clean_country,
        "region": clean_region,
        "city": clean_city,
    }


def normalize_location(value: object, *, default_country: object = "") -> dict[str, str] | None:
    """Normalize one location value to the shared four-string shape."""

    if isinstance(value, Mapping):
        nested_country = value.get("country") or value.get("countryCode") or value.get("country_code")
        nested_region = (
            value.get("region")
            or value.get("state")
            or value.get("stateCode")
            or value.get("administrativeArea")
        )
        nested_city = value.get("city") or value.get("locality") or value.get("addressLocality")
        area = value.get("area")
        if isinstance(area, list):
            levels = [text(level) for level in area if text(level)]
            if levels:
                nested_country = nested_country or levels[0]
                nested_city = nested_city or levels[-1]
                # Adzuna's hierarchy is country, state/region, county, city.
                # The second level is the useful region; the county remains in
                # the source display name and raw source facts.
                if len(levels) >= 3:
                    nested_region = nested_region or levels[1]
        name = _location_name(value)
        country = country_iso2(nested_country or default_country)
        if not country and isinstance(nested_country, Mapping):
            country = country_iso2(nested_country.get("iso2") or nested_country.get("isoCode"))
        location = _location_parts(name, country, text(nested_region), text(nested_city))
        return location if any(location.values()) else None

    raw = text(value)
    if not raw:
        return None
    location = _location_parts(raw, country_iso2(default_country), "", "")
    return location if any(location.values()) else None


def _iter_location_values(value: object) -> list[object]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    if isinstance(value, str):
        # A few APIs join multiple locations with semicolons.  Avoid splitting
        # commas because commas normally separate city and region.
        return [item.strip() for item in value.split(";") if item.strip()]
    return []


def normalize_locations(
    primary: object = None,
    additional: object = None,
    *,
    country: object = "",
) -> list[dict[str, str]]:
    """Normalize and de-duplicate one or more source location values."""

    values = _iter_location_values(primary) + _iter_location_values(additional)
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for value in values:
        item = normalize_location(value, default_country=country)
        if item is None:
            continue
        identity = tuple(item[field] for field in ("name", "country", "region", "city"))
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def location_text(locations: Sequence[Mapping[str, str]], fallback: object = "") -> str:
    names = [text(location.get("name")) for location in locations if text(location.get("name"))]
    if names:
        return "; ".join(dict.fromkeys(names))
    return text(fallback)


def canonical_workplace_type(value: object) -> str:
    raw = text(value).casefold().replace("_", "-")
    if not raw:
        return "unknown"
    if any(token in raw for token in ("hybrid", "flexible", "partially remote")):
        return "hybrid"
    if any(token in raw for token in ("remote", "work from home", "distributed", "virtual")):
        return "remote"
    if any(token in raw for token in ("on-site", "onsite", "on site", "office", "in-person", "in person")):
        return "onsite"
    return "unknown"


def canonical_opportunity_type(value: object, *, title: object = "", description: object = "") -> str:
    raw = text(value).casefold().replace("-", "_").replace(" ", "_")
    if raw in {"job", "jobs", "employment", "position", "role", "full_time", "part_time", "internship"}:
        return "job"
    if any(token in raw for token in ("talent_pool", "talentpool", "general_application", "evergreen", "future_opportunity")):
        return "talent_pool"
    if any(token in raw for token in ("program", "fellowship", "apprenticeship", "rotational")):
        return "program"
    if raw and raw in {"unknown", "other", "none"}:
        return "unknown"
    # Explicit wording is safer than guessing from every job title.  These
    # phrases are the source's own usual labels for non-vacancy opportunities.
    combined = f"{text(title)} {text(description)}".casefold()
    if re.search(r"\b(talent\s+pool|general\s+application|future\s+opportunit(?:y|ies))\b", combined):
        return "talent_pool"
    if re.search(r"\b(fellowship|apprenticeship|rotational\s+program|internship\s+program)\b", combined):
        return "program"
    return "job" if not raw else "unknown"


def actual_publication(value: Mapping[str, Any]) -> str | None:
    """Return an explicitly named publication timestamp, never an update time."""

    for key in (
        "published_at",
        "publishedAt",
        "first_published",
        "firstPublished",
        "firstPublishedAt",
        "publicationDate",
        "publication_date",
    ):
        result = text(value.get(key))
        if result:
            return result
    return None


def source_updated(value: Mapping[str, Any]) -> str | None:
    for key in (
        "source_updated_at",
        "sourceUpdatedAt",
        "updated_at",
        "updatedAt",
        "lastUpdated",
        "lastUpdatedAt",
        "lastUpdatedDate",
        "modified_at",
        "modifiedAt",
    ):
        result = text(value.get(key))
        if result:
            return result
    return None


def deadline(value: Mapping[str, Any]) -> str | None:
    for key in (
        "application_deadline",
        "applicationDeadline",
        "deadline",
        "deadlineDate",
        "endDate",
        "end_date",
    ):
        result = text(value.get(key))
        if result:
            return result
    return None


def description_kind(description: object, *, snippet: bool = False) -> str:
    if snippet:
        return "snippet"
    return "full" if text(description) else "missing"


def html_text(value: object) -> str:
    """Render a scalar list-section value as safe, minimal HTML text."""

    raw = text(value)
    if not raw:
        return ""
    # Values containing markup are source HTML; plain values are escaped so
    # rendering a structured Lever list cannot accidentally create markup.
    return raw if re.search(r"<\s*\w+[^>]*>", raw) else html.escape(raw)
