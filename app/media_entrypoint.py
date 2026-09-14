from __future__ import annotations

from dataclasses import replace
import re

from . import patched as patched_module
from . import conversation as conversation_module
from .brain import build_brain
from .conversation import ActionProposal, ActionType, Intent, ProviderReply, ResponseStyle
from .intent_router import Route, route_turn
from .media_search import search_images

app = patched_module.app

_CONTEXT_FIRST_POLICY = """Treat every message as part of the ongoing conversation, not as an incomplete form field. Brevity or an unfinished-sounding phrase is not by itself a reason to ask the user to continue. Use recent conversation to answer or continue directly whenever reasonably possible. Ask only when one concrete missing fact genuinely blocks an accurate answer or the requested action, and ask specifically for that fact. Do not use generic continuation prompts as a fallback."""
conversation_module.SYSTEM_PROMPT += "\n\n" + _CONTEXT_FIRST_POLICY
_original_local_system_prompt = conversation_module._local_system_prompt

def _context_first_local_system_prompt(*args, **kwargs):
    return _original_local_system_prompt(*args, **kwargs) + "\n" + _CONTEXT_FIRST_POLICY

conversation_module._local_system_prompt = _context_first_local_system_prompt

_brain = build_brain(patched_module.main_module.conversation_provider)
_draft = patched_module.DraftAwareProvider(_brain)
patched_module._provider = _draft
patched_module.main_module.conversation_provider = _draft

_MEDIA_REQUEST_RE = re.compile(r"(?:(?:وريني|ورني|اعرض(?:لي)?|فرجني|show\s+me).{0,36}(?:صور|صوره|صورة|photos?|pictures?|images?)|(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه).{0,24}(?:صور|صوره|صورة)|^(?:صور|صوره|صورة)(?:\b|(?=ال|ل|\s)))", re.IGNORECASE)
_LEGACY_DRAFT_PROMPTS = ("لما تخلص قول", "راجع التفاصيل ثم قل", "كمّل المواصفات والميزانية والمنطقة", "كمل المواصفات والميزانية والمنطقة", "تفضّله جديد ولا مستعمل", "حاطط ميزانية في حدود كام", "تحب أدور لك في أنهي منطقة")

def _is_media_request(text: str) -> bool:
    value = patched_module._clean(text)
    return bool(_MEDIA_REQUEST_RE.search(value)) or route_turn(value).route == Route.MEDIA_IMAGE_REQUEST

patched_module._is_media_request = _is_media_request

def _fresh_context(context):
    cutoff = -1
    for index, item in enumerate(context.history or []):
        if str(item.get("role") or "") != "assistant":
            continue
        text = patched_module._clean(str(item.get("content") or ""))
        if any(marker in text for marker in _LEGACY_DRAFT_PROMPTS):
            cutoff = index
    return context if cutoff < 0 else replace(context, history=list(context.history[cutoff + 1:]))

class DynamicConversationProvider:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.name = f"dynamic({getattr(wrapped, 'name', 'provider')})"
        self.degraded = bool(getattr(wrapped, "degraded", False))
    async def classify_route(self, context, draft=None):
        classifier = getattr(self.wrapped, "classify_route", None)
        return None if classifier is None else await classifier(_fresh_context(context), draft)
    async def respond(self, context):
        context = _fresh_context(context)
        if _is_media_request(context.message):
            return ProviderReply("تمام، بجيبلك الصور المناسبة دلوقتي.", Intent.GENERAL_QUESTION, 1.0, ResponseStyle(), ActionProposal(ActionType.NONE, False, 1.0), provider=self.name, model=None, degraded=False)
        reply = await self.wrapped.respond(context)
        if patched_module._clean(str(reply.text or "")) in {"قولّي أكتر.", "قولي أكتر.", "Tell me more."}:
            reply.text = "هتعامل مع رسالتك كجزء من الكلام اللي قبلها، ومش هطلب منك تفاصيل إلا لو في معلومة محددة فعلًا لازمة للرد أو التنفيذ."
        return reply

_dynamic_provider = DynamicConversationProvider(patched_module.main_module.conversation_provider)
patched_module.main_module.conversation_provider = _dynamic_provider

def _dynamic_draft_reply(context, draft: str, first: bool) -> str:
    semantic_response = patched_module._clean(str(getattr(context, "semantic_response", "") or ""))
    if semantic_response:
        return semantic_response
    return "الطلب لسه عندي زي ما هو. مش هفترض إنك محتاج تزود تفاصيل؛ لو في معلومة محددة لازمة عشان نكمل هطلبها بالاسم."

patched_module._draft_reply_text = _dynamic_draft_reply

@app.get("/api/images")
async def image_search(query: str = ""):
    value = " ".join((query or "").strip().split())[:180]
    if not value:
        return {"query": "", "items": [], "source": "none"}
    value = re.sub(r"^(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه)\s+(?=(?:صور|صوره|صورة)\b)", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"^(?:صورال|صورلي|صورل|صور(?:ه|ة)?)\s*", "", value, flags=re.IGNORECASE).strip()
    return await search_images(value, limit=8)
