from __future__ import annotations

import re

from . import patched as patched_module
from .conversation import ActionProposal, ActionType, Intent, ProviderReply, ResponseStyle
from .intent_router import Route, route_turn
from .media_search import search_images

app = patched_module.app

# Natural image requests like "عايز صور الموتوسيكل" must never be folded into
# a purchase draft. Patch the runtime detector without duplicating the larger
# conversation layer.
_MEDIA_REQUEST_RE = re.compile(
    r"(?:(?:وريني|ورني|اعرض(?:لي)?|فرجني|show\s+me).{0,36}(?:صور|صوره|صورة|photos?|pictures?|images?)|"
    r"(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه).{0,24}(?:صور|صوره|صورة)|"
    r"^(?:صور|صوره|صورة)(?:\b|(?=ال|ل|\s)))",
    re.IGNORECASE,
)


def _is_media_request(text: str) -> bool:
    value = patched_module._clean(text)
    return bool(_MEDIA_REQUEST_RE.search(value)) or route_turn(value).route == Route.MEDIA_IMAGE_REQUEST


_original_request_seed = patched_module._is_request_seed


def _request_seed_without_media(text: str) -> bool:
    if _is_media_request(text):
        return False
    return _original_request_seed(text)


# Draft helpers in patched.py resolve these globals at runtime, so replacing
# them here prevents media-only turns from contaminating an open/new request.
patched_module._is_media_request = _is_media_request
patched_module._is_request_seed = _request_seed_without_media


class MediaAwareProvider:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.name = f"media-aware({getattr(wrapped, 'name', 'provider')})"
        self.degraded = bool(getattr(wrapped, "degraded", False))

    async def respond(self, context):
        if _is_media_request(context.message):
            return ProviderReply(
                "تمام، بجيبلك الصور المناسبة دلوقتي.",
                Intent.GENERAL_QUESTION,
                1.0,
                ResponseStyle(),
                ActionProposal(ActionType.NONE, False, 1.0),
                provider=self.name,
                model=None,
                degraded=False,
            )
        return await self.wrapped.respond(context)


_media_provider = MediaAwareProvider(patched_module.main_module.conversation_provider)
patched_module.main_module.conversation_provider = _media_provider


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
    # Mobile Arabic typing commonly joins the command to the article, for
    # example "صورال RKV250". Keep the product term and discard the command.
    value = re.sub(
        r"^(?:صورال|صورلي|صورل|صور(?:ه|ة)?)\s*",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()
    return await search_images(value, limit=8)
