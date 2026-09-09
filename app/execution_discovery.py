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

_lock = asyncio.Lock()
_last_call = 0.0
CATEGORIES = [
    (('موتوسيكل','موتوسكل','سكوتر','motorcycle','scooter'), 'shop', 'motorcycle'),
    (('سباك','plumber'), 'craft', 'plumber'),
    (('كهربائي','electrician'), 'craft', 'electrician'),
    (('نجار','carpenter'), 'craft', 'carpenter'),
    (('صيدلي','pharmacy'), 'amenity', 'pharmacy'),
    (('مطعم','عشا','restaurant'), 'amenity', 'restaurant'),
    (('فندق','hotel'), 'tourism', 'hotel'),
    (('موبايل','mobile phone'), 'shop', 'mobile_phone'),
]

def category_for(text):
    return next(((key, value) for words, key, value in CATEGORIES if any(w in text.lower() for w in words)), None)

def cached(key):
    with SessionLocal() as db:
        row = db.get(DiscoveryCache, key)
        if row and row.created_at > datetime.utcnow() - timedelta(hours=6):
            return json.loads(row.payload)

def save(key, payload):
    with SessionLocal() as db:
        row = db.get(DiscoveryCache, key)
        if row:
            row.payload = json.dumps(payload, ensure_ascii=False); row.created_at = datetime.utcnow()
        else:
            db.add(DiscoveryCache(key=key, payload=json.dumps(payload, ensure_ascii=False)))
        db.commit()

async def discover_businesses(text, area=None):
    global _last_call
    category = category_for(text)
    query = ' '.join(text.split())
    key = hashlib.sha256(json.dumps([category or query, area], ensure_ascii=False).encode()).hexdigest()
    async with _lock:
        hit = cached(key)
        if hit is not None:
            return hit
        # Serialize the public geocoder, including fallback searches; cache reused results.
        await asyncio.sleep(max(0, 1.1 - (time.monotonic() - _last_call)))
        _last_call = time.monotonic()
        if not category or not area:
            result = await services.discover_businesses(query, area)
            if result:
                save(key, result)
            return result
        if services.GOOGLE_KEY:
            try:
                result = await services._google(services._query(query, area))
                if result:
                    save(key, result); return result
            except (httpx.HTTPError, ValueError):
                pass
        headers = {'User-Agent': 'Maak/1.0 (https://github.com/karim87nu-create/mehtag-eih)'}
        async with httpx.AsyncClient(timeout=12, headers=headers, follow_redirects=False) as client:
            response = await client.get('https://nominatim.openstreetmap.org/search', params={
                'q': f'{area}, مصر', 'format': 'jsonv2', 'limit': 1, 'countrycodes': 'eg'})
            response.raise_for_status()
            locations = response.json()
            if not locations:
                return []
            lat, lon = float(locations[0]['lat']), float(locations[0]['lon'])
            tag, value = category
            # Fixed catalog tags and numeric coordinates: user text cannot inject QL.
            q = f'[out:json][timeout:10];nwr["{tag}"="{value}"](around:5000,{lat},{lon});out center tags 15;'
            response = await client.post('https://overpass-api.de/api/interpreter', data={'data': q})
            response.raise_for_status()
            result = []
            for element in response.json().get('elements', []):
                tags = element.get('tags', {})
                name = tags.get('name:ar') or tags.get('name')
                if not name:
                    continue
                kind, oid = element['type'], element['id']
                result.append({'external_id': f'osm-{kind}-{oid}', 'name': name,
                    'website': tags.get('contact:website') or tags.get('website'),
                    'phone': tags.get('contact:phone') or tags.get('phone'),
                    'source': f'https://www.openstreetmap.org/{kind}/{oid}',
                    'address': tags.get('addr:full'), 'search_radius_m': 5000})
            save(key, result)
            return result
