from __future__ import annotations

from . import patched as patched_module
from .media_search import search_images

app = patched_module.app


@app.get("/api/images")
async def image_search(query: str = ""):
    value = " ".join((query or "").strip().split())[:180]
    if not value:
        return {"query": "", "items": [], "source": "none"}
    return await search_images(value, limit=8)
