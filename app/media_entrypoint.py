from __future__ import annotations

from dataclasses import replace
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
_LEGACY_DRAFT_PROMPTS = (
    "لما تخلص قول",
    "راجع التفاصيل ثم قل",
    "كمّل المواصفات والميزانية والمنطقة",
    "كمل المواصفات والميزانية والمنطقة",
)
_PRODUCT_HINT_RE = re.compile(
    r"(?:موتوسيكل|موتسيكل|عربيه|عربية|سياره|سيارة|موبايل|تليفون|لابتوب|لاب توب|"
    r"شقه|شقة|مكتب|جهاز|تلفزيون|غساله|غسالة|تكييف|bike|motorcycle|car|phone|laptop)",
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


def _without_legacy_draft_history(context):
    """Cut off the old form-like request flow completely.

    Old assistant prompts must not keep an obsolete draft alive. This preserves
    newer conversation turns while preventing the historic draft from being
    executable or from steering Gemini.
    """
    cutoff = -1
    for index, item in enumerate(context.history or []):
        if str(item.get("role") or "") != "assistant":
            continue
        text = patched_module._clean(str(item.get("content") or ""))
        if any(marker in text for marker in _LEGACY_DRAFT_PROMPTS):
            cutoff = index
    if cutoff < 0:
        return context
    return replace(context, history=list(context.history[cutoff + 1 :]))


_original_draft_segment = patched_module._draft_segment


def _safe_condition(text: str) -> bool:
    value = patched_module._norm(text)
    return bool(re.fullmatch(r"(?:عايزه?\s+|عاوزه?\s+|يفضل\s+)?(?:جديد|مستعمل|new|used)", value))


def _guided_segment(context):
    clean_context = _without_legacy_draft_history(context)
    raw = _original_draft_segment(clean_context)
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


def _strict_draft_detail(context) -> bool:
    """Only an actual answer/constraint may mutate a live draft.

    The old router treated almost any short phrase as REQUEST_DETAIL. That made
    objections and normal conversation such as 'منا بقولك' or 'اي العبط ده'
    become request data. Keep deterministic controls separate and accept only
    recognisable constraints here; ambiguous language stays normal chat.
    """
    segment = _guided_segment(context)
    if not segment:
        return False
    current = patched_module._clean(context.message)
    if _request_seed_without_media(current):
        return True
    if (
        patched_module._is_small_talk(current)
        or patched_module._is_execute_control(current)
        or patched_module._is_cancel_control(current)
        or patched_module._is_conversation_only_turn(current)
    ):
        return False
    kind = patched_module._detail_kind(current)
    if kind == "condition":
        return _safe_condition(current)
    return kind in {"budget", "area", "model"} or patched_module._is_explicit_correction(current)


patched_module._is_draft_detail = _strict_draft_detail


def _guided_reply(context, draft: str, first: bool) -> str:
    segment = _guided_segment(context)
    details = segment[1:] if len(segment) > 1 else []
    kinds = {patched_module._detail_kind(item) for item in details}
    seed = segment[0] if segment else ""

    # Product-shopping requests benefit from condition/budget questions. Do not
    # force the same form onto services or everyday conversation.
    if _PRODUCT_HINT_RE.search(seed):
        if "condition" not in kinds:
            return "تمام. تفضّله جديد ولا مستعمل؟"
        if "budget" not in kinds:
            return "تمام. حاطط ميزانية في حدود كام؟"
        if "area" not in kinds:
            return "حلو. تحب أدور لك في أنهي منطقة؟"
        return "تمام، كده الصورة واضحة. نبدأ؟"

    if first:
        return "تمام. احكيلي محتاج إيه بالظبط، وأنا هسألك بس عن التفصيلة اللي ناقصة."
    return "تمام، فهمتك. كمّل براحتك."


patched_module._draft_reply_text = _guided_reply


class MediaAwareProvider:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.name = f"media-aware({getattr(wrapped, 'name', 'provider')})"
        self.degraded = bool(getattr(wrapped, "degraded", False))

    async def respond(self, context):
        context = _without_legacy_draft_history(context)
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
        return await classifier(_without_legacy_draft_history(context), draft)


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
