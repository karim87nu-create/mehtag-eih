from dataclasses import dataclass
import os


@dataclass(frozen=True)
class LocaleContext:
    locale: str
    language: str
    region: str
    currency: str
    timezone: str


LOCALES = {
    "ar-EG": LocaleContext("ar-EG", "ar", "EG", "EGP", "Africa/Cairo"),
    "en-EG": LocaleContext("en-EG", "en", "EG", "EGP", "Africa/Cairo"),
    "en-US": LocaleContext("en-US", "en", "US", "USD", "America/New_York"),
    "en-GB": LocaleContext("en-GB", "en", "GB", "GBP", "Europe/London"),
}


def resolve_locale(locale: str | None, detected_language: str | None = None) -> LocaleContext:
    requested = (locale or os.getenv("DEFAULT_LOCALE", "ar-EG")).strip()
    base = LOCALES.get(requested, LOCALES["ar-EG"])
    if detected_language == "en" and base.region == "EG":
        return LOCALES["en-EG"]
    return base
