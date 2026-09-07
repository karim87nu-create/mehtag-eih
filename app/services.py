import os, re, secrets, httpx
from dotenv import load_dotenv

load_dotenv()

GOOGLE_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000")

def understand_request(text: str):
    # parser بسيط للـSpike فقط - قابل للاستبدال لاحقًا بـ LLM
    result = {"item": None, "area": None, "budget": None, "deadline": None}
    result["item"] = text[:80]

    money = re.search(r"(?:حدود|حتى|ميزانية|بـ|ب)\s*([0-9٠-٩,]+)", text)
    if money:
        raw = money.group(1).replace(",", "")
        trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
        try:
            result["budget"] = float(raw.translate(trans))
        except:
            pass

    known_areas = ["مدينة نصر", "التجمع", "مصر الجديدة", "المعادي", "مدينتي", "الرحاب", "الدقي", "المهندسين"]
    for a in known_areas:
        if a in text:
            result["area"] = a
            break

    for marker in ["اليوم", "بكرة", "غدا", "غدًا"]:
        if marker in text:
            result["deadline"] = marker
            break
    return result

async def discover_businesses(query: str):
    if not GOOGLE_KEY:
        return [
            {
                "external_id": "demo-1",
                "name": "Demo Business 1",
                "website": "https://example.com",
                "phone": None,
                "source": "demo"
            },
            {
                "external_id": "demo-2",
                "name": "Demo Business 2",
                "website": None,
                "phone": "+201000000000",
                "source": "demo"
            },
        ]

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_KEY,
        "X-Goog-FieldMask": "places.id,places.displayName,places.websiteUri,places.nationalPhoneNumber"
    }
    payload = {"textQuery": query, "languageCode": "ar", "maxResultCount": 10}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()

    out = []
    for p in data.get("places", []):
        out.append({
            "external_id": p.get("id"),
            "name": (p.get("displayName") or {}).get("text", "Unknown"),
            "website": p.get("websiteUri"),
            "phone": p.get("nationalPhoneNumber"),
            "source": "google_places"
        })
    return out

def build_reachability(business: dict):
    # مهم: الهاتف وحده ليس قناة إرسال مسموحًا بها.
    if business.get("website"):
        return {"reachable": True, "channel": "website_review_required", "endpoint": business["website"]}
    return {"reachable": False, "channel": None, "endpoint": None}

def new_token():
    return secrets.token_urlsafe(24)

async def send_request_to_endpoint(endpoint: str, request_payload: dict):
    # Placeholder intentionally.
    # لا يوجد Cold Outreach آلي حتى يتم توصيل قناة مصرح بها فعليًا.
    return {"sent": False, "reason": "NO_APPROVED_OUTREACH_ADAPTER"}
