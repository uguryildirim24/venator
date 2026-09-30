"""Conservative New England location reading for a Profile's Hard Filter.

A city with several readings keeps its New England reading. Unreadable sites
are outside only when the employer's current Postings have no readable local site.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from venator.countries import country_code
from venator.discover.common import STATE_CODES, normalize_locations, source_country
from venator.place_lookup import readings

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
    "Newport Hospital": "new_england",
    "Rhode Island Hospital": "new_england",
    "The Miriam Hospital": "new_england",
    "Bradley Hospital": "new_england",
    "Hasbro Children's Hospital": "new_england",
    "Emma Pendleton Bradley Hospital": "new_england",
}
_LOCAL_FACILITIES = {name.casefold(): scope for name, scope in _FACILITIES.items()}


def _site(name: str, *, region: str = "", country: str = "") -> Scope:
    name = name.strip()
    if (code := country_code(country)) and code != "US":
        return "outside"
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
        remote_parts = [without_remote.strip(" ,-/"), *[part.strip(" ,-/") for part in re.split(r"[,;]|\s+[-–—]\s+", without_remote)]]
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
    # A remote site tied to a state is scoped to that state. Only a remote
    # without a specific region is US-wide; a foreign country was handled above.
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
    if re.search(r"\bremote\b", name, re.I):
        return "remote"
    if re.search(r"\b(?:Puerto Rico|Guam|American Samoa|Northern Mariana Islands|US Virgin Islands)\b", name, re.I):
        return "outside"
    if local := _LOCAL_FACILITIES.get(name.casefold()):
        return local
    if _REMOTE.fullmatch(name):
        return "remote"
    # County readings outrank an unknown neighborhood or employer site label.
    for county in re.findall(r"\b[\w ]+?\s+County\b", name, flags=re.I):
        places = readings("county:" + county.strip())
        if places:
            if "MA" in places:
                return "ma"
            if NEW_ENGLAND.intersection(places):
                return "new_england"
            return "outside"
    # US is a prefix, not a city: "US, Lenexa KCIB (PRA)" reads Lenexa.
    city = re.sub(r"^(?:US|USA|United States)\s*[,\-]\s*", "", name, flags=re.I)
    city = re.sub(r"^City of ", "", city, flags=re.I)
    city = re.sub(r"\s*\([^)]*\)$|\s+(?:office|site|campus)$", "", city, flags=re.I).strip()
    city = re.sub(r"\s+(?:KCIB|Affiliate|Job Posting Location|Posting Location|Job Location)\b.*$", "", city, flags=re.I).strip()
    for candidate in (city, city.split(",")[0].strip()):
        places = readings(candidate)
        if places:
            if "MA" in places:
                return "ma"
            if NEW_ENGLAND.intersection(places):
                return "new_england"
            return "outside"
    if code == "US":
        return "remote" if not city else "unreadable"
    return "unreadable"


# A title hint must name a city followed by an explicit region/country; bare
# city names in titles cannot disambiguate a shared place name.
_TITLE_PLACE = re.compile(
    r"(?:\s/\s|\s[-–—]\s|\()\s*([A-Za-z][A-Za-z .'-]+?,\s*(?:[A-Z]{2}|[A-Za-z][A-Za-z ]+?))(?=\s*(?:\)|\(|[-–—/]|$))"
)


def _ambiguous_site(name: str) -> bool:
    city = re.sub(r"\s*\([^)]*\)$", "", name).strip()
    if "," in city or re.search(r"\b(?:" + "|".join(re.escape(n) for n in STATE_NAMES) + r")\b", city, re.I):
        return False
    if re.search(r"\b[A-Z]{2}\b", city):
        return False
    return len(readings(city)) > 1


def location_scope(
    posting: Mapping[str, object], *, employer_has_local: bool | None = None,
    physical_only: bool = False,
) -> Scope:
    """Keep any New England site; only physical sites can vouch for an employer."""
    # Source countries survive in source_facts even when an older observation
    # left locations.country empty. Bind unspecified sites to that country;
    # an explicitly US secondary site still keeps a multi-site Posting local.
    facts = posting.get("source_facts")
    fields = [posting]
    if isinstance(facts, Mapping):
        fields.append(facts)
        detail = facts.get("jobPostingInfo")
        if isinstance(detail, Mapping):
            fields.append(detail)
    foreign_country = next(
        (code for value in fields if (code := source_country(value)) and code != "US"),
        "",
    )
    sites: list[Scope] = ["outside"] if foreign_country else []
    ambiguous = False
    known_local = False

    def add(name: str, *, region: str = "", country: str = "") -> None:
        nonlocal ambiguous, known_local
        country = country or foreign_country
        if physical_only and re.search(r"\bremote\b", name, re.I) and not (
            (code := country_code(country)) and code != "US"
        ):
            return
        scope = _site(name, region=region, country=country)
        sites.append(scope)
        shared_city = not region and (not country or country_code(country) == "US") and _ambiguous_site(name)
        if shared_city:
            ambiguous = True
        elif scope in {"ma", "new_england", "remote"}:
            known_local = True
    raw = posting.get("locations")
    structured_sites = False
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, Mapping):
                name = str(entry.get("name") or "")
                if _COUNT.fullmatch(name.strip()):
                    if (code := country_code(entry.get("country"))) and code != "US":
                        sites.append("outside")
                    continue
                structured_sites = True
                for part in re.split(r"\s*[;|]\s*", name):
                    add(part, region=str(entry.get("region") or ""), country=str(entry.get("country") or ""))
            elif isinstance(entry, str) and not _COUNT.fullmatch(entry.strip()):
                structured_sites = True
                for part in re.split(r"\s*[;|]\s*", entry):
                    add(part)
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
                        for part in re.split(r"\s*[;|]\s*", name):
                            add(part, region=entry["region"], country=entry["country"])
    if isinstance(display, str) and not (structured_sites and display_is_count):
        for part in re.split(r"\s*[;|]\s*", display):
            # The display repeats structured sites without their country. Do
            # not read that duplicate as a second, potentially local US site.
            if isinstance(raw, list) and any(
                isinstance(entry, Mapping)
                and (code := country_code(entry.get("country"))) and code != "US"
                and part.strip() in re.split(r"\s*[;|]\s*", str(entry.get("name") or "").strip())
                for entry in raw
            ):
                continue
            add(part)
    # A title can clarify an ambiguous site, but cannot erase a separate,
    # explicitly local (or US-remote) site on a multi-site Posting.
    if not physical_only and (ambiguous or "unreadable" in sites or not sites) and not known_local:
        title = posting.get("title")
        if isinstance(title, str):
            for match in _TITLE_PLACE.finditer(title):
                hint = match[1]
                region = hint.rsplit(",", 1)[-1].strip()
                if region.upper() not in STATE_CODES.values() and region.casefold() not in STATE_NAMES and not country_code(region):
                    continue
                scope = _site(hint)
                if scope == "outside":
                    return "outside"
                # A local title does not rescue a known outside site or an
                # unreadable employer that revision 35 already excluded.
                if scope in {"ma", "new_england"} and "outside" not in sites and employer_has_local is not False:
                    return scope
    for value in ("ma", "new_england", "remote", "unreadable", "outside"):
        if value in sites:
            if value == "unreadable" and employer_has_local is False and not (display_is_count and not structured_sites):
                return "outside"
            return value  # type: ignore[return-value]
    # A count does not name a site. Do not apply the employer rule to it.
    if display_is_count and not structured_sites:
        return "unreadable"
    return "outside" if employer_has_local is False else "unreadable"
