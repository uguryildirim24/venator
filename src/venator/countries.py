"""Shared ISO country identity; unknown labels stay unknown."""
from collections.abc import Mapping
from functools import lru_cache

import pycountry

# Colloquial source labels that ISO's official names do not cover.
_ALIASES = {
    "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "northern ireland": "GB", "south korea": "KR", "north korea": "KP",
    "russia": "RU", "vietnam": "VN", "taiwan": "TW", "turkey": "TR",
    "czech republic": "CZ", "ivory coast": "CI", "u.s.": "US", "u.s.a.": "US",
}
COUNTRY_NAMES = {
    str(name).casefold(): country.alpha_2
    for country in pycountry.countries
    for name in (getattr(country, field, None) for field in ("name", "official_name", "common_name"))
    if name
} | _ALIASES


@lru_cache(maxsize=512)
def _code(value: str) -> str | None:
    text = " ".join(value.strip().casefold().split())
    if not text:
        return None
    if text in COUNTRY_NAMES:
        return COUNTRY_NAMES[text]
    if len(text) in {2, 3}:
        country = pycountry.countries.get(**{"alpha_2" if len(text) == 2 else "alpha_3": text.upper()})
        return country.alpha_2 if country else None
    return None


def country_code(value: object) -> str | None:
    """Read exact names/codes or a source object; never infer from a city."""
    if isinstance(value, str):
        return _code(value)
    if isinstance(value, Mapping):
        for field in ("alpha2Code", "alpha3Code", "countryCode", "code", "descriptor", "name"):
            if result := country_code(value.get(field)):
                return result
    return None
