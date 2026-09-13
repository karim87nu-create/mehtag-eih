from __future__ import annotations

import re

from . import patched as patched_module
from .media_search import search_images

app = patched_module.app

# Natural image requests like "عايز صور الموتوسيكل" must never be folded into
# a purchase draft. Patch the runtime detector without duplicating the larger
# conversation layer.
patched_module._MEDIA_REQUEST_RE = re.compile(
    r"(?:(?:وريني|ورني|اعرض(?:لي)?|فرجني|show\s+me).{0,36}(?:صور|صوره|صورة|photos?|pictures?|images?)|"
    r"(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه).{0,20}(?:صور|صوره|صورة)|"
    r"^(?:صور|صوره|صورة)\b)",
    re.IGNORECASE,
)


@app.get("/api/images")
async def image_search(query: str = ""):
    value = " ".join((query or "").strip().split())[:180]
    if not value:
        return {"query": "", "items": [], "source": "none"}
    value = re.sub(
        r"^(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه)\s+(?=(?:صور|صوره|صورة)\b)",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    return await search_images(value, limit=8)
