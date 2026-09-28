"""Conservative New England location reading for a Profile's Hard Filter.

A site with no recognizable jurisdiction is not evidence of an outside site.
Never infer a state from a shared town name (Cambridge, Concord, Burlington).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from venator.countries import country_code
from venator.discover.common import STATE_CODES, normalize_locations

Scope = Literal["ma", "new_england", "remote", "unreadable", "outside"]
NEW_ENGLAND = frozenset({"MA", "RI", "NH", "CT", "VT", "ME"})
# Full state names are read only as entire words; abbreviations are read only
# after a city, delimiter, or at the end, never out of "Cambridge MA"'s city.
_STATE_NAMES = """Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia""".split("|")
_CODES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
STATE_NAMES = {name.casefold(): code for name, code in zip(_STATE_NAMES, _CODES, strict=True)}
_REGION = re.compile(r"(?:^|[,;|\-/]\s*|\s+)(" + "|".join(re.escape(n) for n in sorted(STATE_NAMES, key=len, reverse=True)) + r")(?=\s*(?:[,;|\-/]|$|\bUSA\b|\bUnited States\b))", re.I)
_ABBREVIATION = re.compile(r"(?:^|[\s,;|\-/])([A-Z]{2})(?=$|[\s,;|\-/])")
_REMOTE = re.compile(r"\bremote\b|\b(?:US|USA|United States)(?:[ -]?wide| nationwide)?\b", re.I)
_COUNT = re.compile(r"^\d+\s+locations?$", re.I)
_US = {"us", "usa", "united states", "united states of america"}

# Exact, unambiguous site labels seen in the View, plus major world cities.
# Do not infer from a shared city name (e.g. Cambridge, London, Paris, Dublin).
_FOREIGN_CITIES = frozenset(name.casefold() for name in (
    "Tokyo", "Tokyo (NPKK Sales)", "Seoul", "Basel", "Basel (City)",
    "Beijing", "Frankfurt am Main", "Ljubljana", "Montevideo",
    "Grenzach", "Kaiseraugst", "Vitry-sur-Seine", "City of Singapore",
    "Shanghai", "Hyderabad", "Hyderabad (Office)", "Budapest",
    "Petaling Jaya", "Schaftenau", "Penzberg", "Warsaw", "Taipei",
    "Mengeš", "Sant Cugat del Vallès", "Barcelona", "Mississauga",
    "Mannheim", "Bengaluru India", "Prague", "Sao Paulo", "Rotkreuz",
    "Moscow (City)", "Guangzhou", "Qingdao Site",
    "Bogota", "Riga", "Taguig City", "Toranomon (NPKK Head Office)",
    "Beijing Yizhuang", "Singapore Manufacturing - Tuas", "Central Singapore",
    "Kundl", "Welwyn", "Shanghai China", "Shanghai Jing'An Office",
    "Otemachi-JP", "Warsaw-WeWork-PL", "Barcelona Gran Vía",
    "Gangnam-gu, Korea, Republic of", "Seoul, Korea, Republic of",
    "Seoul, Republic of Korea", "London (The Westworks)", "Dublin (NOCC)",
    "Mexico City", "Munich", "Zurich",
    "Osaka", "Singapore", "Hong Kong", "Mumbai", "Jakarta",
    "Auckland", "Buenos Aires", "Tianjin",
    "Wulumuqi", "Wroclaw", "Le Trait", "Brasilia", "Chengdu",
))
# Known New England facilities without a town in the source location. The
# Other explicit site labels can name a facility rather than a city
# (Maddock Alumni Center is at Brown in Providence).
_FACILITIES = {
    "Beth Israel Deaconess Medical Center": "ma",
    "Tufts Medical Center": "ma",
    "New England Baptist Hospital": "ma",
    "Lemuel Shattuck Hospital": "ma",
    "Mount Auburn Hospital": "ma",
    "Lahey Clinic": "ma",
    "Winchester Hospital": "ma",
    "Beth Israel Deaconess Hospital Needham": "ma",
    "Beth Israel Deaconess Hospital Milton": "ma",
    "MelroseWakefield Hospital": "ma",
    "Lawrence Memorial Hospital": "ma",
    "Beth Israel Deaconess Hospital Plymouth": "ma",
    "Anna Jaques Hospital": "ma",
    "Beverly Hospital": "ma",
    "Lowell General Hospital": "ma",
    "Addison Gilbert Hospital": "ma",
    "Exeter Hospital": "new_england",
    "Maddock Alumni Center": "new_england",
}
_LOCAL_FACILITIES = {name.casefold(): scope for name, scope in _FACILITIES.items()}


def _site(name: str, *, region: str = "", country: str = "") -> Scope:
    name = name.strip()
    if _COUNT.fullmatch(name) or not (name or region or country):
        return "unreadable"
    # A structured country is authoritative; a trailing or leading written
    # country is used only if it is an unambiguous country token.
    code = country_code(country) if country else None
    if not code:
        parts = [part.strip() for part in re.split(r"[,;]|\s+[-–—]\s+", name)]
        prefix = re.match(r"^([A-Za-z]{3,30})-", name)
        # Country-labelled remote locations are outside, not US-wide remote.
        # Remove only the remote marker, then read exact country tokens.
        without_remote = re.sub(r"\bremote\b(?:\s+location)?", "", name, flags=re.I)
        remote_parts = [part.strip(" ,-/") for part in re.split(r"[,;]|\s+[-–—]\s+", without_remote)]
        # A city can share a country name (Lebanon, NH). A leading country
        # only counts when the following component is not a US region.
        trailing = parts[-1].casefold()
        trailing_state = trailing in STATE_NAMES or parts[-1].upper() in STATE_CODES.values()
        candidates = (parts[-1],) if trailing_state else (
            parts[-1], parts[0], prefix[1] if prefix else "", *remote_parts,
        )
        for candidate in candidates:
            candidate = re.sub(r"\s*\([^)]*\)$", "", candidate).strip()
            if candidate.casefold() in _US:
                code = "US"
                break
            if candidate.casefold() not in STATE_NAMES and candidate.upper() not in STATE_CODES.values():
                code = country_code(candidate)
                if code:
                    break
    if code and code != "US":
        return "outside"
    if re.search(r"\bremote\b", name, re.I):
        return "remote"
    # Structured region first, then full names, then uppercase postal codes.
    state = STATE_NAMES.get(region.casefold()) or (region.upper() if region.upper() in STATE_CODES.values() else None)
    if not state:
        matches = list(_REGION.finditer(name))
        if matches:
            state = STATE_NAMES[matches[-1][1].casefold()]
    if not state:
        for match in _ABBREVIATION.finditer(name):
            if match[1] in STATE_CODES.values() and match[1] not in {"US"}:
                state = match[1]
                break
    if state:
        return "ma" if state == "MA" else "new_england" if state in NEW_ENGLAND else "outside"
    if re.search(r"\b(?:Puerto Rico|Guam|American Samoa|Northern Mariana Islands|US Virgin Islands)\b", name, re.I):
        return "outside"
    if code == "US" or _REMOTE.fullmatch(name):
        return "remote"
    if name.casefold() in _FOREIGN_CITIES:
        return "outside"
    if local := _LOCAL_FACILITIES.get(name.casefold()):
        return local
    return "unreadable"


def location_scope(posting: Mapping[str, object]) -> Scope:
    """Keep any New England site; do not kill a wholly unreadable site."""
    sites: list[Scope] = []
    raw = posting.get("locations")
    structured_sites = False
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, Mapping):
                name = str(entry.get("name") or "")
                if _COUNT.fullmatch(name.strip()):
                    continue
                structured_sites = True
                sites.extend(_site(part, region=str(entry.get("region") or ""), country=str(entry.get("country") or "")) for part in re.split(r"\s*[;|]\s*", name))
            elif isinstance(entry, str) and not _COUNT.fullmatch(entry.strip()):
                structured_sites = True
                sites.extend(_site(part) for part in re.split(r"\s*[;|]\s*", entry))
    display = posting.get("location")
    display_is_count = isinstance(display, str) and bool(_COUNT.fullmatch(display.strip()))
    if not structured_sites or display_is_count:
        # Workday's top-level site is often only "2 Locations". Historical
        # Postings can also lack a structured site list despite carrying
        # additional sites in source_facts. Read the same explicit fields View
        # recovers before deciding whether every site is outside New England.
        facts = posting.get("source_facts")
        if isinstance(facts, Mapping):
            for fields in (facts, *(value for value in facts.values() if isinstance(value, Mapping))):
                recovered = normalize_locations(
                    fields.get("locationsText") or fields.get("location") or fields.get("locations"),
                    fields.get("additionalLocations") or fields.get("secondaryLocations"),
                )
                for entry in recovered:
                    name = entry.get("name")
                    if isinstance(name, str) and not _COUNT.fullmatch(name.strip()):
                        structured_sites = True
                        sites.extend(_site(part) for part in re.split(r"\s*[;|]\s*", name))
    if isinstance(display, str) and not (structured_sites and display_is_count):
        sites.extend(_site(part) for part in re.split(r"\s*[;|]\s*", display))
    for value in ("ma", "new_england", "remote", "unreadable", "outside"):
        if value in sites:
            return value  # type: ignore[return-value]
    return "unreadable"
