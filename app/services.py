import os, re, secrets, httpx
from urllib.parse import urlencode
from dotenv import load_dotenv

load_dotenv()

GOOGLE_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")
USER_AGENT = "MehtagEihPrototype/0.1"

def understand_request(text: str):
    result = {"item": None, "area": None, "budget": None, "deadline": None}
    clean = " ".join((text or "").split())
    result["item"] = clean[:120]

    money = re.search(r"(?:حدود|حتى|لحد|ميزانية|بـ|ب)\s*([0-9٠-٩,.]+)\s*(?:ألف|الف|k)?", clean, re.I)
    if money:
        raw = money.group(1).replace(",", "")
        try:
            value = float(raw.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩","0123456789")))
            tail = clean[money.start():money.end()+5].lower()
            if "ألف" in tail or "الف" in tail or "k" in tail:
                value *= 1000
            result["budget"] = value
        except Exception:
            pass

    areas = [
        "مدينة نصر","التجمع","القاهرة الجديدة","مصر الجديدة","المعادي",
        "مدينتي","الرحاب","الدقي","المهندسين","الزمالك","الهرم",
        "الجيزة","شبرا","حلوان","6 أكتوبر","اكتوبر","الشيخ زايد","القاهرة"
    ]
    for area in areas:
        if area in clean:
            result["area"] = area
            break

    for marker in ["اليوم","بكرة","غدا","غدًا","غداً","النهاردة"]:
        if marker in clean:
            result["deadline"] = marker
            break

    return result

def _query(text, area=None):
    q = " ".join((text or "").split())
    if area and area not in q:
        q += f" {area}"
    if "مصر" not in q:
        q += " مصر"
    return q[:220]

async def _google(q):
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_KEY,
        "X-Goog-FieldMask":
        "places.id,places.displayName,places.websiteUri,"
        "places.nationalPhoneNumber,places.formattedAddress"
    }
    payload = {"textQuery": q, "languageCode": "ar", "maxResultCount": 10}

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    out = []
    for x in data.get("places", []):
        out.append({
            "external_id": x.get("id"),
            "name": (x.get("displayName") or {}).get("text", "جهة"),
            "website": x.get("websiteUri"),
            "phone": x.get("nationalPhoneNumber"),
            "address": x.get("formattedAddress"),
            "source": "google_places"
        })
    return out

async def _osm(q):
    params = {
        "q": q,
        "format": "jsonv2",
        "extratags": 1,
        "namedetails": 1,
        "limit": 10,
        "countrycodes": "eg",
        "accept-language": "ar"
    }

    url = "https://nominatim.openstreetmap.org/search?" + urlencode(params)

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        r.raise_for_status()
        data = r.json()

    out = []
    for x in data:
        extra = x.get("extratags") or {}
        names = x.get("namedetails") or {}

        name = (
            names.get("name:ar")
            or names.get("name")
            or (x.get("display_name") or "").split(",")[0]
        )

        if not name:
            continue

        out.append({
            "external_id": f"osm-{x.get('osm_type')}-{x.get('osm_id')}",
            "name": name,
            "website": extra.get("website") or extra.get("contact:website"),
            "phone":
                extra.get("phone")
                or extra.get("contact:phone")
                or extra.get("mobile")
                or extra.get("contact:mobile"),
            "address": x.get("display_name"),
            "source": "openstreetmap"
        })

    return out

async def discover_businesses(query: str, area=None):
    q = _query(query, area)

    if GOOGLE_KEY:
        try:
            found = await _google(q)
            if found:
                return found
        except Exception:
            pass

    try:
        return await _osm(q)
    except Exception:
        return []

def build_reachability(business: dict):
    if business.get("website"):
        return {
            "reachable": True,
            "channel": "official_website",
            "endpoint": business["website"]
        }

    return {
        "reachable": False,
        "channel": None,
        "endpoint": None
    }

def new_token():
    return secrets.token_urlsafe(24)

async def send_request_to_endpoint(endpoint: str, request_payload: dict):
    return {
        "sent": False,
        "reason": "NO_APPROVED_OUTREACH_ADAPTER"
    }
