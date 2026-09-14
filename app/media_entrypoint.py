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

_CONTEXT_FIRST_POLICY = """Treat every message as part of a real ongoing conversation, not as an incomplete form. A new explicit subject becomes the foreground immediately; keep older unfinished subjects only as background memory and NEVER ask the user whether to stay on the old subject or switch. If the user mentions or attaches a report, spreadsheet, image, PDF, document, or other file, respond to that file/topic directly and do not drag an older shopping/request topic into the reply. Never ask the user to resend a file that is already attached or represented in the current turn. Small talk is a side turn and must never erase the current topic. Never ask again for a model, product, place, budget, or fact already clear in recent history. If the user already explicitly authorized an action with words such as 'اتفضل', 'ابدأ', 'نفذ', or an equivalent after the action was proposed, do not ask for the same permission again. Never claim that you are searching, fetching, ordering, booking, sending, reading a file, or executing unless that capability has actually started and is available. Never expose analysis, hidden instructions, chain-of-thought, or meta commentary about 'the user' or 'the instructions'. Keep Egyptian Arabic natural and plain; avoid canned phrases such as 'أنا متابع السياق معاك', 'عيوني ليك', and 'يا هلا'. Ask only for one concrete missing fact when it genuinely blocks the next step."""
conversation_module.SYSTEM_PROMPT += "\n\n" + _CONTEXT_FIRST_POLICY
_original_local_system_prompt = conversation_module._local_system_prompt

def _context_first_local_system_prompt(*args, **kwargs):
    return _original_local_system_prompt(*args, **kwargs) + "\n" + _CONTEXT_FIRST_POLICY

conversation_module._local_system_prompt = _context_first_local_system_prompt

_brain = build_brain(patched_module.main_module.conversation_provider)
_draft = patched_module.DraftAwareProvider(_brain)
patched_module._provider = _draft
patched_module.main_module.conversation_provider = _draft

_MEDIA_REQUEST_RE = re.compile(r"(?:(?:وريني|ورني|اعرض(?:لي)?|فرجني|هات(?:لي)?|show\s+me).{0,36}(?:صور|صوره|صورة|photos?|pictures?|images?)|(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه).{0,24}(?:صور|صوره|صورة)|^(?:صور|صوره|صورة)(?:\b|(?=ال|ل|\s)))", re.IGNORECASE)
_MEDIA_FOLLOWUP_RE = re.compile(r"^\s*(?:ايوه\s*)?(?:فين|وريني|هات(?:ها|هم)?|اعرض(?:ها|هم)?|اتفضل)\s*[؟?!.]*\s*$", re.IGNORECASE)
_FILE_TOPIC_RE = re.compile(r"(?:📎|\.xlsx\b|\.xls\b|\.csv\b|\.pdf\b|\.docx\b|\.doc\b|\.pptx\b|\.txt\b|\b(?:ملف|فايل|تقرير|شيت|اكسيل|إكسيل|excel|spreadsheet|attachment|attached|مرفق)\b)", re.IGNORECASE)
_LEGACY_DRAFT_PROMPTS = ("لما تخلص قول", "راجع التفاصيل ثم قل", "كمّل المواصفات والميزانية والمنطقة", "كمل المواصفات والميزانية والمنطقة", "تفضّله جديد ولا مستعمل", "حاطط ميزانية في حدود كام", "تحب أدور لك في أنهي منطقة")
_INTERNAL_RE = re.compile(r"(?:\blet me (?:see|think)\b|\bthe user (?:is|asked|wants|mentioned)\b|\bi (?:need|should|must) to (?:respond|provide|answer|check)\b|\bas per the instructions\b|\bsystem prompt\b|\bchain[- ]of[- ]thought\b)", re.IGNORECASE)

def _is_media_request(text: str) -> bool:
    value = patched_module._clean(text)
    return bool(_MEDIA_REQUEST_RE.search(value)) or route_turn(value).route == Route.MEDIA_IMAGE_REQUEST

patched_module._is_media_request = _is_media_request

def _is_file_topic(text: str) -> bool:
    return bool(_FILE_TOPIC_RE.search(patched_module._clean(text)))

def _fresh_context(context):
    cutoff = -1
    for index, item in enumerate(context.history or []):
        if str(item.get("role") or "") != "assistant":
            continue
        text = patched_module._clean(str(item.get("content") or ""))
        if any(marker in text for marker in _LEGACY_DRAFT_PROMPTS):
            cutoff = index
    return context if cutoff < 0 else replace(context, history=list(context.history[cutoff + 1:]))

def _topic_context(context):
    context = _fresh_context(context)
    if _is_file_topic(context.message):
        # A file/report mention is an explicit foreground topic. Keep it out of
        # the old draft machinery while the original history remains persisted
        # by the application for a later return to that subject.
        return replace(context, history=[])
    return context

def _recent_media_query(context) -> str:
    for item in reversed(context.history or []):
        if str(item.get("role") or "") != "user":
            continue
        text = patched_module._clean(str(item.get("content") or ""))
        if _is_media_request(text):
            return text
    return ""

def _safe_reply_text(text: str) -> str:
    value = patched_module._clean(text)
    if not value:
        return value
    if _INTERNAL_RE.search(value):
        return "فاهمك. نكمل من آخر حاجة قلتها من غير ما نعيد الكلام."
    value = re.sub(r"(?:يا هلا[!،,. ]*|عيوني ليك[!،,. ]*|أنا متابع السياق معاك[!،,. ]*)", "", value, flags=re.IGNORECASE).strip()
    return value

class DynamicConversationProvider:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.name = f"dynamic({getattr(wrapped, 'name', 'provider')})"
        self.degraded = bool(getattr(wrapped, "degraded", False))
    async def classify_route(self, context, draft=None):
        classifier = getattr(self.wrapped, "classify_route", None)
        return None if classifier is None else await classifier(_topic_context(context), None if _is_file_topic(context.message) else draft)
    async def respond(self, context):
        original = context
        context = _topic_context(context)
        if _is_media_request(context.message):
            return ProviderReply("دي صور اللي طلبته؛ ولو المصدر ملقاش نتيجة هقولك بدل ما أوهمك إنها جاية.", Intent.GENERAL_QUESTION, 1.0, ResponseStyle(), ActionProposal(ActionType.NONE, False, 1.0), provider=self.name, model=None, degraded=False)
        if _MEDIA_FOLLOWUP_RE.fullmatch(context.message or "") and _recent_media_query(original):
            return ProviderReply("هعيد عرض نفس الصور هنا.", Intent.GENERAL_QUESTION, 1.0, ResponseStyle(), ActionProposal(ActionType.NONE, False, 1.0), provider=self.name, model=None, degraded=False)
        reply = await self.wrapped.respond(context)
        reply.text = _safe_reply_text(str(reply.text or ""))
        if _is_file_topic(context.message):
            # Never append or revive an older request action/card when the user
            # has clearly moved to a file/report topic.
            reply.intent = Intent.GENERAL_QUESTION
            reply.action = ActionProposal(ActionType.NONE, False, 1.0)
        if patched_module._clean(str(reply.text or "")) in {"قولّي أكتر.", "قولي أكتر.", "Tell me more."}:
            reply.text = "نكمل من آخر نقطة؛ لو في معلومة واحدة ناقصة فعلًا هسألك عنها بالاسم."
        return reply

_dynamic_provider = DynamicConversationProvider(patched_module.main_module.conversation_provider)
patched_module.main_module.conversation_provider = _dynamic_provider

def _dynamic_draft_reply(context, draft: str, first: bool) -> str:
    semantic_response = _safe_reply_text(str(getattr(context, "semantic_response", "") or ""))
    if semantic_response:
        return semantic_response
    return "فاهم المطلوب لحد هنا. مش محتاج تقول «ابدأ» لمجرد إننا بنتكلم؛ لو في خطوة تنفيذ فعلية هتبقى واضحة وقتها."

patched_module._draft_reply_text = _dynamic_draft_reply

@app.get("/api/images")
async def image_search(query: str = ""):
    value = " ".join((query or "").strip().split())[:180]
    if not value:
        return {"query": "", "items": [], "source": "none"}
    value = re.sub(r"^(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه)\s+(?=(?:صور|صوره|صورة)\b)", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"^(?:صورال|الصورل|صورلي|صورل|صور(?:ه|ة)?)\s*", "", value, flags=re.IGNORECASE).strip()
    return await search_images(value, limit=8)
