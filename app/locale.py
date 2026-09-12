"""Locale, region and currency policy.

Egypt is the first operational market, but no discovery or execution component
should need to hard-code it. Unknown valid locale tags are preserved and use
neutral ISO/UTC fallbacks rather than silently pretending to be Egyptian.
"""
from dataclasses import dataclass
import os
import re


@dataclass(frozen=True)
class RegionDefaults:
    currency: str
    timezone: str
    search_name: str


@dataclass(frozen=True)
class LocaleContext:
    locale: str
    language: str
    region: str
    currency: str
    timezone: str
    search_country: str

    @property
    def country_code(self) -> str | None:
        return self.region.lower() if re.fullmatch(r"[A-Z]{2}", self.region) else None


REGIONS = {
    "EG": RegionDefaults("EGP", "Africa/Cairo", "مصر"),
    "US": RegionDefaults("USD", "America/New_York", "United States"),
    "GB": RegionDefaults("GBP", "Europe/London", "United Kingdom"),
    "AE": RegionDefaults("AED", "Asia/Dubai", "United Arab Emirates"),
    "SA": RegionDefaults("SAR", "Asia/Riyadh", "Saudi Arabia"),
    "QA": RegionDefaults("QAR", "Asia/Qatar", "Qatar"),
    "KW": RegionDefaults("KWD", "Asia/Kuwait", "Kuwait"),
    "BH": RegionDefaults("BHD", "Asia/Bahrain", "Bahrain"),
    "OM": RegionDefaults("OMR", "Asia/Muscat", "Oman"),
    "JO": RegionDefaults("JOD", "Asia/Amman", "Jordan"),
    "CA": RegionDefaults("CAD", "America/Toronto", "Canada"),
    "AU": RegionDefaults("AUD", "Australia/Sydney", "Australia"),
    "IN": RegionDefaults("INR", "Asia/Kolkata", "India"),
    "FR": RegionDefaults("EUR", "Europe/Paris", "France"),
    "DE": RegionDefaults("EUR", "Europe/Berlin", "Germany"),
    "IT": RegionDefaults("EUR", "Europe/Rome", "Italy"),
    "ES": RegionDefaults("EUR", "Europe/Madrid", "Spain"),
}


def _canonical_locale(value: str) -> tuple[str, str, str]:
    clean = value.strip().replace("_", "-")
    match = re.fullmatch(r"([A-Za-z]{2,3})(?:-([A-Za-z]{2}|[0-9]{3}))?", clean)
    if not match:
        return "ar-EG", "ar", "EG"
    language = match.group(1).lower()
    region = (match.group(2) or "ZZ").upper()
    canonical = language if region == "ZZ" else f"{language}-{region}"
    return canonical, language, region


def resolve_locale(locale: str | None, detected_language: str | None = None) -> LocaleContext:
    requested = locale or os.getenv("DEFAULT_LOCALE", "ar-EG")
    canonical, language, region = _canonical_locale(requested)
    if detected_language in {"ar", "en", "mixed"}:
        language = detected_language
        if language != "mixed":
            canonical = language if region == "ZZ" else f"{language}-{region}"
    defaults = REGIONS.get(region, RegionDefaults("XXX", "UTC", region if region != "ZZ" else ""))
    search_country = defaults.search_name
    if region == "EG" and language == "en":
        search_country = "Egypt"
    return LocaleContext(canonical, language, region, defaults.currency, defaults.timezone, search_country)


LOCALES = {
    tag: resolve_locale(tag)
    for tag in ("ar-EG", "en-EG", "en-US", "en-GB")
}
