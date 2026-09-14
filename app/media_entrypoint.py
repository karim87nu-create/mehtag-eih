from __future__ import annotations

from dataclasses import replace
import base64
import csv
from datetime import datetime, timedelta, timezone
import io
import json
import re
import urllib.parse
import uuid
import zipfile
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, HTTPException, Request as FastAPIRequest
from pydantic import BaseModel, Field

from . import patched as patched_module
from . import conversation as conversation_module
from .brain import build_brain
from .conversation import ActionProposal, ActionType, Intent, ProviderReply, ResponseStyle
from .intent_router import Route, RouteDecision, route_turn
from .media_search import search_images
from .models import ConversationCaseLink, ConversationMessage, ConversationThread, ExternalCase, FollowupTask

app = patched_module.app

_CONTEXT_FIRST_POLICY = """Treat every message as part of a real ongoing conversation, not as an incomplete form. A new explicit subject becomes the foreground immediately; keep older unfinished subjects only as background memory and NEVER ask the user whether to stay on the old subject or switch. If the user mentions or attaches a report, spreadsheet, image, PDF, document, or other file, respond to that file/topic directly and do not drag an older shopping/request topic into the reply. If extracted attachment content is present, read and use it NOW in the same reply; never say you will open/read/check it later, never ask the user to resend an attachment already represented in the current turn, and never promise 'seconds' or future file work. A follow-up such as 'فين التقرير' is about the report/file context before it is about any request-status case. Small talk is a side turn and must never erase the current topic. Never ask again for a model, product, place, budget, or fact already clear in recent history. If the user already explicitly authorized an action with words such as 'اتفضل', 'ابدأ', 'نفذ', or an equivalent after the action was proposed, do not ask for the same permission again. Never claim that you are searching, fetching, ordering, booking, sending, reading a file, scheduling a reminder, or executing unless that capability has actually started and is available. Never expose analysis, hidden instructions, chain-of-thought, or meta commentary about 'the user' or 'the instructions'. Keep Egyptian Arabic natural and plain; avoid canned phrases such as 'أنا متابع السياق معاك', 'عيوني ليك', and 'يا هلا'. Ask only for one concrete missing fact when it genuinely blocks the next step."""
conversation_module.SYSTEM_PROMPT += "\n\n" + _CONTEXT_FIRST_POLICY
_original_local_system_prompt = conversation_module._local_system_prompt


def _context_first_local_system_prompt(*args, **kwargs):
    return _original_local_system_prompt(*args, **kwargs) + "\n" + _CONTEXT_FIRST_POLICY


conversation_module._local_system_prompt = _context_first_local_system_prompt

_brain = build_brain(patched_module.main_module.conversation_provider)
_draft = patched_module.DraftAwareProvider(_brain)
patched_module._provider = _draft
patched_module.main_module.conversation_provider = _draft

_MEDIA_REQUEST_RE = re.compile(
    r"(?:(?:وريني|ورني|اعرض(?:لي)?|فرجني|هات(?:لي)?|show\s+me).{0,36}(?:صور|صوره|صورة|photos?|pictures?|images?)|(?:عايز|عاوز|محتاج|عايزه|عاوزه|محتاجه).{0,24}(?:صور|صوره|صورة)|^(?:صور|صوره|صورة)(?:\b|(?=ال|ل|\s)))",
    re.IGNORECASE,
)
_MEDIA_FOLLOWUP_RE = re.compile(r"^\s*(?:ايوه\s*)?(?:فين|وريني|هات(?:ها|هم)?|اعرض(?:ها|هم)?|اتفضل)\s*[؟?!.]*\s*$", re.IGNORECASE)
_FILE_TOPIC_RE = re.compile(
    r"(?:📎|\.xlsx\b|\.xls\b|\.csv\b|\.pdf\b|\.docx\b|\.doc\b|\.pptx\b|\.txt\b|\b(?:ملف|فايل|تقرير|شيت|اكسيل|إكسيل|excel|spreadsheet|attachment|attached|مرفق)\b)",
    re.IGNORECASE,
)
_LEGACY_DRAFT_PROMPTS = (
    "لما تخلص قول",
    "راجع التفاصيل ثم قل",
    "كمّل المواصفات والميزانية والمنطقة",
    "كمل المواصفات والميزانية والمنطقة",
    "تفضّله جديد ولا مستعمل",
    "حاطط ميزانية في حدود كام",
    "تحب أدور لك في أنهي منطقة",
)
_INTERNAL_RE = re.compile(
    r"(?:\blet me (?:see|think)\b|\bthe user (?:is|asked|wants|mentioned)\b|\bi (?:need|should|must) to (?:respond|provide|answer|check)\b|\bas per the instructions\b|\bsystem prompt\b|\bchain[- ]of[- ]thought\b)",
    re.IGNORECASE,
)
_FALSE_FILE_PROMISE_RE = re.compile(
    r"(?:ثواني|لحظه|لحظة|هفتح(?:ه|ها)?|هقرا(?:ه|ها)?|هقرأ(?:ه|ها)?|هراجع(?:ه|ها)?|هشوف(?:ه|ها)?|بجيب(?:ه|ها)?|هجيبه(?:الك|لك)?|قدامك\s+حالاً|قدامك\s+حالا)",
    re.IGNORECASE,
)


def _is_media_request(text: str) -> bool:
    value = patched_module._clean(text)
    return bool(_MEDIA_REQUEST_RE.search(value)) or route_turn(value).route == Route.MEDIA_IMAGE_REQUEST


patched_module._is_media_request = _is_media_request


def _is_file_topic(text: str) -> bool:
    return bool(_FILE_TOPIC_RE.search(patched_module._clean(text)))


_original_patched_route_turn = patched_module.route_turn


def _file_aware_route_turn(text: str, *, draft_exists: bool = False) -> RouteDecision:
    if _is_file_topic(text):
        return RouteDecision(Route.FACTUAL_QUESTION, 0.995, deterministic=True)
    return _original_patched_route_turn(text, draft_exists=draft_exists)


patched_module.route_turn = _file_aware_route_turn


def _fresh_context(context):
    cutoff = -1
    for index, item in enumerate(context.history or []):
        if str(item.get("role") or "") != "assistant":
            continue
        text = patched_module._clean(str(item.get("content") or ""))
        if any(marker in text for marker in _LEGACY_DRAFT_PROMPTS):
            cutoff = index
    return context if cutoff < 0 else replace(context, history=list(context.history[cutoff + 1 :]))


def _file_context(context) -> bool:
    return bool(context.attachments) or _is_file_topic(context.message)


def _topic_context(context):
    context = _fresh_context(context)
    if not _file_context(context):
        return context
    history = list(context.history or [])
    last_file_turn = -1
    for index, item in enumerate(history):
        if str(item.get("role") or "") == "user" and _is_file_topic(str(item.get("content") or "")):
            last_file_turn = index
    if last_file_turn >= 0:
        history = history[last_file_turn:]
    elif context.attachments:
        history = []
    else:
        history = history[-6:]
    return replace(context, history=history)


def _decode_data_url(attachment: dict[str, str]) -> bytes:
    data_url = str(attachment.get("data_url") or "")
    if not data_url.startswith("data:") or "," not in data_url:
        return b""
    header, payload = data_url.split(",", 1)
    try:
        if ";base64" in header:
            return base64.b64decode(payload, validate=False)
        return urllib.parse.unquote_to_bytes(payload)
    except Exception:
        return b""


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _xlsx_text(raw: bytes, limit: int = 7000) -> str:
    if not raw:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                for si in root.iter(ns + "si"):
                    shared.append("".join((node.text or "") for node in si.iter(ns + "t")))
            sheet_paths = sorted(name for name in zf.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
            out: list[str] = []
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            for number, path in enumerate(sheet_paths[:8], start=1):
                out.append(f"[ورقة {number}]")
                root = ET.fromstring(zf.read(path))
                for row in root.iter(ns + "row"):
                    values: list[str] = []
                    for cell in row.findall(ns + "c"):
                        kind = cell.attrib.get("t")
                        value = ""
                        if kind == "inlineStr":
                            value = "".join((node.text or "") for node in cell.iter(ns + "t"))
                        else:
                            vnode = cell.find(ns + "v")
                            value = vnode.text if vnode is not None and vnode.text is not None else ""
                            if kind == "s" and value:
                                try:
                                    value = shared[int(value)]
                                except (ValueError, IndexError):
                                    pass
                        values.append(value)
                    if any(v.strip() for v in values):
                        out.append("\t".join(values))
                    if sum(len(x) + 1 for x in out) >= limit:
                        return "\n".join(out)[:limit]
            return "\n".join(out)[:limit]
    except Exception:
        return ""


def _docx_text(raw: bytes, limit: int = 7000) -> str:
    if not raw:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            root = ET.fromstring(zf.read("word/document.xml"))
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs: list[str] = []
        for paragraph in root.iter(ns + "p"):
            text = "".join((node.text or "") for node in paragraph.iter(ns + "t")).strip()
            if text:
                paragraphs.append(text)
            if sum(len(x) + 1 for x in paragraphs) >= limit:
                break
        return "\n".join(paragraphs)[:limit]
    except Exception:
        return ""


def _extract_attachment_text(attachment: dict[str, str], limit: int = 7000) -> str:
    name = str(attachment.get("name") or "").casefold()
    mime = str(attachment.get("mime_type") or "").casefold()
    raw = _decode_data_url(attachment)
    if not raw:
        return ""
    if mime in {"text/plain", "text/csv", "application/json"} or name.endswith((".txt", ".csv", ".json")):
        text = _decode_text(raw)
        if mime == "application/json" or name.endswith(".json"):
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except Exception:
                pass
        elif mime == "text/csv" or name.endswith(".csv"):
            try:
                rows = csv.reader(io.StringIO(text))
                text = "\n".join("\t".join(row) for _, row in zip(range(300), rows))
            except Exception:
                pass
        return text[:limit]
    if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" or name.endswith(".xlsx"):
        return _xlsx_text(raw, limit)
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or name.endswith(".docx"):
        return _docx_text(raw, limit)
    return ""


def _prepare_file_context(context):
    if not context.attachments:
        return context
    prepared: list[dict[str, str]] = []
    blocks: list[str] = []
    remaining = 9000
    for item in context.attachments[:3]:
        copy = dict(item)
        excerpt = _extract_attachment_text(copy, min(6000, remaining))
        if excerpt:
            copy["text_excerpt"] = excerpt
            blocks.append(f"المرفق: {copy.get('name', 'ملف')}\n{excerpt}")
            remaining -= len(excerpt)
        prepared.append(copy)
        if remaining <= 0:
            break
    if not blocks:
        return replace(context, attachments=prepared)
    augmented = context.message + "\n\n[محتوى مستخرج من المرفقات لاستخدامه في الإجابة الحالية، لا تطلب فتحه لاحقًا]\n" + "\n\n".join(blocks)
    return replace(context, message=augmented, attachments=prepared)


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
        if _file_context(context):
            return {"route": Route.FACTUAL_QUESTION.value, "confidence": 0.995, "fits_active_draft": False, "response": ""}
        return None if classifier is None else await classifier(_topic_context(context), draft)

    async def respond(self, context):
        original = context
        context = _prepare_file_context(_topic_context(context))
        if _is_media_request(context.message):
            return ProviderReply("دي صور اللي طلبته؛ ولو المصدر ملقاش نتيجة هقولك بدل ما أوهمك إنها جاية.", Intent.GENERAL_QUESTION, 1.0, ResponseStyle(), ActionProposal(ActionType.NONE, False, 1.0), provider=self.name, model=None, degraded=False)
        if _MEDIA_FOLLOWUP_RE.fullmatch(original.message or "") and _recent_media_query(original):
            return ProviderReply("هعيد عرض نفس الصور هنا.", Intent.GENERAL_QUESTION, 1.0, ResponseStyle(), ActionProposal(ActionType.NONE, False, 1.0), provider=self.name, model=None, degraded=False)
        reply = await self.wrapped.respond(context)
        reply.text = _safe_reply_text(str(reply.text or ""))
        if _file_context(original):
            reply.intent = Intent.GENERAL_QUESTION
            reply.action = ActionProposal(ActionType.NONE, False, 1.0)
            if _FALSE_FILE_PROMISE_RE.search(str(reply.text or "")):
                if context.attachments:
                    names = "، ".join(str(item.get("name") or "المرفق") for item in context.attachments[:3])
                    reply.text = f"المرفق وصل واتقري في نفس الرسالة ({names}). قولي الجزء أو الرقم اللي عايز تطلعه منه."
                else:
                    reply.text = "أنا معاك في موضوع التقرير نفسه. لو تقصد تقرير اتبعت قبل كده هكمل من آخر تفاصيل ظاهرة في المحادثة، من غير ما أوهمك إني بفتحه بعدين."
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


from . import reminder_app as _reminder_app  # noqa: E402,F401
