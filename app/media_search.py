from __future__ import annotations

import re
from typing import Any

import httpx


_IMAGE_CUE_RE = re.compile(
    r"(?:وريني|ورني|اعرض(?:لي)?|فرجني|صور|صوره|صورة|show\s+me|photos?|pictures?|images?)",
    re.IGNORECASE,
)
_REQUEST_FILLER_RE = re.compile(
    r"(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه|هاتلي|هات|جيبلي|جيب|من\s+فضلك)",
    re.IGNORECASE,
)


def clean_image_query(text: str) -> str:
    value = " ".join((text or "").strip().split())
    value = _IMAGE_CUE_RE.sub(" ", value)
    value = _REQUEST_FILLER_RE.sub(" ", value)
    value = re.sub(r"\b(?:ال|لل|لي)\b", " ", value)
    value = " ".join(value.split()).strip("-–—:،,. ")
    low = value.casefold().replace("-", "").replace("_", "").replace(" ", "")
    if not low:
        return ""
    if "rkv250" in low or "rve250" in low:
        return "Keeway RKV 250"
    return value[:120]


def _query_candidates(query: str) -> list[str]:
    low = query.casefold().replace("-", "").replace("_", "").replace(" ", "")
    if "keewayrkv250" in low or "rkv250" in low:
        values = ["Keeway RKV 250", "Keeway RKV"]
    elif any(word in query for word in ("موتوسيكل", "موتوسكل", "دراجة نارية")):
        values = ["motorcycle", query]
    else:
        values = [query]
    seen = set()
    return [item for item in values if item and not (item.casefold() in seen or seen.add(item.casefold()))]


def _safe_url(value: Any) -> str | None:
    url = str(value or "").strip()
    return url if url.startswith(("https://", "http://")) else None


def _rkv_relevant(item: dict[str, str]) -> bool:
    haystack = " ".join(str(item.get(k) or "") for k in ("title", "url", "thumbnail")).casefold()
    compact = haystack.replace("-", "").replace("_", "").replace(" ", "")
    return "rkv" in compact and ("keeway" in compact or "rkv250" in compact)


async def _openverse(query: str, limit: int) -> list[dict[str, str]]:
    endpoint = "https://api.openverse.org/v1/images/"
    params = {"q": query, "page_size": min(max(limit * 2, 6), 20)}
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        response = await client.get(endpoint, params=params, headers={"User-Agent": "Maak/1.0 image-search"})
        response.raise_for_status()
    items: list[dict[str, str]] = []
    for row in (response.json().get("results") or []):
        thumb = _safe_url(row.get("thumbnail"))
        page = _safe_url(row.get("foreign_landing_url")) or _safe_url(row.get("url"))
        if not thumb or not page:
            continue
        items.append({
            "thumbnail": thumb,
            "url": page,
            "title": str(row.get("title") or query)[:140],
            "source": str(row.get("source") or "Openverse")[:80],
            "license": str(row.get("license") or "")[:40],
        })
        if len(items) >= limit:
            break
    return items


async def _commons(query: str, limit: int) -> list[dict[str, str]]:
    endpoint = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": str(min(max(limit * 2, 6), 20)),
        "prop": "imageinfo",
        "iiprop": "url|mime",
        "iiurlwidth": "720",
        "format": "json",
        "formatversion": "2",
    }
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        response = await client.get(endpoint, params=params, headers={"User-Agent": "Maak/1.0 image-search"})
        response.raise_for_status()
    items: list[dict[str, str]] = []
    for page in ((response.json().get("query") or {}).get("pages") or []):
        info = (page.get("imageinfo") or [{}])[0]
        mime = str(info.get("mime") or "")
        if mime and not mime.startswith("image/"):
            continue
        thumb = _safe_url(info.get("thumburl")) or _safe_url(info.get("url"))
        landing = _safe_url(info.get("descriptionurl")) or _safe_url(info.get("url"))
        if not thumb or not landing:
            continue
        items.append({
            "thumbnail": thumb,
            "url": landing,
            "title": str(page.get("title") or query).removeprefix("File:")[:140],
            "source": "Wikimedia Commons",
            "license": "",
        })
        if len(items) >= limit:
            break
    return items


async def search_images(text: str, limit: int = 8) -> dict[str, Any]:
    query = clean_image_query(text)
    if len(query) < 2:
        return {"query": query, "items": [], "source": "none"}

    candidates = _query_candidates(query)
    normalized = query.casefold().replace("-", "").replace("_", "").replace(" ", "")
    exact_rkv = "keewayrkv250" in normalized or "rkv250" in normalized

    if exact_rkv:
        source_order = ((_commons, "Wikimedia Commons"), (_openverse, "Openverse"))
    else:
        source_order = ((_openverse, "Openverse"), (_commons, "Wikimedia Commons"))

    for searcher, source_name in source_order:
        for candidate in candidates:
            try:
                items = await searcher(candidate, limit)
                if exact_rkv:
                    items = [item for item in items if _rkv_relevant(item)]
                if items:
                    return {"query": candidate, "items": items[:limit], "source": source_name}
            except Exception:
                pass

    return {"query": query, "items": [], "source": "none"}
