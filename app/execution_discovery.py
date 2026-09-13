"""Real category/area search. OSM results are leads, never offers or consent."""
import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta

import httpx

from . import services
from .db import SessionLocal
from .execution_models import DiscoveryCache
from .locale import LocaleContext, resolve_locale

_lock = asyncio.Lock()
_last_call = 0.0
DISCOVERY_CACHE_VERSION = "radius-v3"
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

CATEGORIES = [
    (("موتوسيكل", "موتوسكل", "سكوتر", "motorcycle", "scooter"), "shop", "motorcycle"),
    (("سباك", "plumber"), "craft", "plumber"),
    (("كهربائي", "electrician"), "craft", "electrician"),
    (("نجار", "carpenter"), "craft", "carpenter"),
    (("صيدلي", "pharmacy"), "amenity", "pharmacy"),
    (("مطعم", "عشا", "restaurant"), "amenity", "restaurant"),
    (("فندق", "hotel"), "tourism", "hotel"),
    (("موبايل", "mobile phone"), "shop", "mobile_phone"),
]


def category_for(text):
    low = text.lower()
    return next(((key, value) for words, key, value in CATEGORIES if any(w in low for w in words)), None)


def cached(key):
    with SessionLocal() as db:
        row = db.get(DiscoveryCache, key)
        if row and row.created_at > datetime.utcnow() - timedelta(hours=6):
            return json.loads(row.payload)


def save(key, payload):
    with SessionLocal() as db:
        row = db.get(DiscoveryCache, key)
        if row:
            row.payload = json.dumps(payload, ensure_ascii=False)
            row.created_at = datetime.utcnow()
        else:
            db.add(DiscoveryCache(key=key, payload=json.dumps(payload, ensure_ascii=False)))
        db.commit()


async def _overpass(client: httpx.AsyncClient, query: str) -> dict:
    """Run one Overpass query with bounded retry/failover.

    Public Overpass instances can transiently throttle or time out. A temporary
    outage is not evidence that no supplier exists, so only return data after a
    successful response and raise if every endpoint is unavailable.
    """
    last_error: Exception | None = None
    for endpoint_index, endpoint in enumerate(OVERPASS_ENDPOINTS):
        for attempt in range(2):
            if endpoint_index or attempt:
                await asyncio.sleep(1.5 * (attempt + 1))
            try:
                response = await client.post(endpoint, data={"data": query}, timeout=28.0)
                if response.status_code in RETRYABLE_STATUS:
                    last_error = httpx.HTTPStatusError(
                        f"Overpass temporary status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                    continue
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
                last_error = ValueError("Overpass returned a non-object payload")
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError) as exc:
                last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("Overpass unavailable")


async def discover_businesses(text, area=None, locale_context: LocaleContext | str | None = None):
    global _last_call
    context = locale_context if isinstance(locale_context, LocaleContext) else resolve_locale(locale_context)
    category = category_for(text)
    query = " ".join(text.split())
    key = hashlib.sha256(json.dumps(
        [DISCOVERY_CACHE_VERSION, category or query, area, context.locale, context.region],
        ensure_ascii=False,
    ).encode()).hexdigest()

    async with _lock:
        hit = cached(key)
        if hit is not None:
            return hit

        await asyncio.sleep(max(0, 1.1 - (time.monotonic() - _last_call)))
        _last_call = time.monotonic()

        if not category or not area:
            result = await services.discover_businesses(query, area, context)
            if result:
                save(key, result)
            return result

        if services.GOOGLE_KEY:
            try:
                result = await services._google(services._query(query, area, context), context)
                if result:
                    save(key, result)
                    return result
            except (httpx.HTTPError, ValueError):
                pass

        headers = {"User-Agent": "Maak/1.0 supplier-discovery"}
        async with httpx.AsyncClient(timeout=28, headers=headers, follow_redirects=False) as client:
            geocode_params = {
                "q": ", ".join(value for value in (area, context.search_country) if value),
                "format": "jsonv2",
                "limit": 1,
                "accept-language": context.language if context.language != "mixed" else "ar",
            }
            if context.country_code:
                geocode_params["countrycodes"] = context.country_code

            response = await client.get("https://nominatim.openstreetmap.org/search", params=geocode_params)
            response.raise_for_status()
            locations = response.json()
            if not locations:
                return []

            lat, lon = float(locations[0]["lat"]), float(locations[0]["lon"])
            tag, value = category
            result = []

            # Prefer the requested neighborhood, then widen once only when no
            # named local lead exists. Back off before the wider query so public
            # Overpass infrastructure is not hit in a burst.
            for index, radius in enumerate((5000, 15000)):
                if index:
                    await asyncio.sleep(1.5)
                q = f'[out:json][timeout:20];nwr["{tag}"="{value}"](around:{radius},{lat},{lon});out center tags 15;'
                payload = await _overpass(client, q)
                result = []
                for element in payload.get("elements", []):
                    tags = element.get("tags", {})
                    localized_name = tags.get(f"name:{context.language}") if context.language != "mixed" else None
                    language_fallback = tags.get("name:ar") if context.language == "ar" else tags.get("name:en")
                    name = localized_name or tags.get("name") or language_fallback
                    if not name:
                        continue
                    kind, oid = element["type"], element["id"]
                    result.append({
                        "external_id": f"osm-{kind}-{oid}",
                        "name": name,
                        "website": tags.get("contact:website") or tags.get("website"),
                        "phone": tags.get("contact:phone") or tags.get("phone"),
                        "source": f"https://www.openstreetmap.org/{kind}/{oid}",
                        "address": tags.get("addr:full"),
                        "search_radius_m": radius,
                    })
                if result:
                    break

            save(key, result)
            return result
