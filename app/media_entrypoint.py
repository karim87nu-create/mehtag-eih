from __future__ import annotations

import os
import re

from . import patched as patched_module
from .conversation import (
    ActionProposal,
    ActionType,
    FallbackProvider,
    Intent,
    ProviderReply,
    ResilientProvider,
    ResponseStyle,
)
from .gemini_provider import build_gemini_provider
from .intent_router import Route, route_turn
from .media_search import search_images

app = patched_module.app

# Experience-only intelligence activation. Legacy Railway services keep app.main.
_gemini_provider = build_gemini_provider()
if _gemini_provider is not None:
    patched_module.main_module.conversation_provider = ResilientProvider(
        _gemini_provider,
        FallbackProvider(),
        timeout_seconds=float(os.getenv("CONVERSATION_TIMEOUT_SECONDS", "20")),
    )

_MEDIA_REQUEST_RE = re.compile(
    r"(?:(?:وريني|ورني|اعرض(?:لي)?|فرجني|show\s+me).{0,36}(?:صور|صوره|صورة|photos?|pictures?|images?)|"
    r"(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه).{0,24}(?:صور|صوره|صورة)|"
    r"^(?:صور|صوره|صورة)(?:\b|(?=ال|ل|\s)))",
    re.IGNORECASE,
)
_EVERYDAY_WANT_RE = re.compile(
    r"^(?:انا\s+)?(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه)\s+"
    r"(?:اكل|آكل|اشرب|أشرب|انام|أنام|ارتاح|أرتاح|قهوه|قهوة|شاي|ميه|مياه|"
    r"افطر|أفطر|اتغدى|أتغدى|اتعشى|أتعشى|اكل حاجه|آكل حاجة)(?:\s|$)",
    re.IGNORECASE,
)


def _is_media_request(text: str) -> bool:
    value = patched_module._clean(text)
    return bool(_MEDIA_REQUEST_RE.search(value)) or route_turn(value).route == Route.MEDIA_IMAGE_REQUEST


_original_request_seed = patched_module._is_request_seed


def _request_seed_without_media(text: str) -> bool:
    value = patched_module._clean(text)
    if _is_media_request(value) or _EVERYDAY_WANT_RE.search(value):
        return False
    return _original_request_seed(value)


patched_module._is_media_request = _is_media_request
patched_module._is_request_seed = _request_seed_without_media

# A live draft is a conversation, not a bag that swallows every following turn.
# Keep only answers that are recognisable as request constraints. In particular,
# phrases such as "بحب جديد" or an objection must not become a condition merely
# because they contain the word "جديد".
_original_draft_segment = patched_module._draft_segment


def _safe_condition(text: str) -> bool:
    value = patched_module._norm(text)
    return bool(re.fullmatch(r"(?:عايزه?\s+|عاوزه?\s+|يفضل\s+)?(?:جديد|مستعمل|new|used)", value))


def _guided_segment(context):
    raw = _original_draft_segment(context)
    if not raw:
        return raw
    kept = [raw[0]]
    for fragment in raw[1:]:
        value = patched_module._clean(fragment)
        kind = patched_module._detail_kind(value)
        if kind == "condition" and not _safe_condition(value):
            continue
        if kind in {"budget", "area", "model"} or (kind == "condition" and _safe_condition(value)):
            kept.append(value)
            continue
        if patched_module._is_explicit_correction(value):
            kept.append(value)
    return patched_module._resolve_explicit_corrections(kept)


patched_module._draft_segment = _guided_segment


def _guided_reply(context, draft: str, first: bool) -> str:
    segment = _guided_segment(context)
    details = segment[1:] if len(segment) > 1 else []
    kinds = {patched_module._detail_kind(item) for item in details}
    if "condition" not in kinds:
        return "تمام، نمشيها واحدة واحدة. تفضّله جديد ولا مستعمل؟"
    if "budget" not in kinds:
        return "حلو. حاطط ميزانية في حدود كام؟"
    if "area" not in kinds:
        return "تمام. تحب أدور لك في أنهي منطقة؟"
    return "تمام، كده عندي الأساسيات. لو التفاصيل دي مناسبة ليك نبدأ، ولو عايز تعدّل حاجة قولّي."


patched_module._draft_reply_text = _guided_reply


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

    async def classify_route(self, context, draft=None):
        classifier = getattr(self.wrapped, "classify_route", None)
        if classifier is None:
            return None
        return await classifier(context, draft)


_media_provider = MediaAwareProvider(patched_module.main_module.conversation_provider)
patched_module.main_module.conversation_provider = _media_provider


@app.get("/api/images")
async def image_search(query: str = ""):
    value = " ".join((query or "").strip().split())[:180]
    if not value:
        return {"query": "", "items": [], "source": "none"}
    value = re.sub(
        r"^(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه)\s+(?=(?:صور|صوره|صورة)\b)",
        "", value, flags=re.IGNORECASE,
    ).strip()
    value = re.sub(
        r"^(?:صورال|صورلي|صورل|صور(?:ه|ة)?)\s*", "", value, flags=re.IGNORECASE,
    ).strip()
    return await search_images(value, limit=8)
