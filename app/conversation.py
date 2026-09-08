"""Server-side conversation intelligence and action policy boundary.

The provider decides what a message means and how to answer. The gate separately
decides whether a proposed business action is permitted. This separation prevents
chat text from silently turning into merchant outreach or execution.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import logging
import os
from pathlib import Path
import threading
import unicodedata
from typing import Any

import httpx

logger = logging.getLogger("maak.conversation")


class Intent(str, Enum):
    SMALL_TALK = "SMALL_TALK"
    NEW_REQUEST = "NEW_REQUEST"
    CONTINUATION = "CONTINUATION"
    EXTERNAL_EVENT_FOLLOWUP = "EXTERNAL_EVENT_FOLLOWUP"
    PROBLEM = "PROBLEM"
    DECISION = "DECISION"
    GENERAL_QUESTION = "GENERAL_QUESTION"


class ActionType(str, Enum):
    NONE = "NONE"
    CREATE_REQUEST = "CREATE_REQUEST"
    FOLLOW_CASE = "FOLLOW_CASE"
    RECORD_PROBLEM = "RECORD_PROBLEM"


@dataclass
class ResponseStyle:
    language: str = "ar"
    dialect: str = "egyptian"
    tone: str = "warm"
    mood: str = "neutral"
    urgency: str = "normal"
    formality: str = "casual"


@dataclass
class ActionProposal:
    type: ActionType = ActionType.NONE
    authorized: bool = False
    confidence: float = 0.0
    case_id: int | None = None
    case_type: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderReply:
    text: str
    intent: Intent
    confidence: float
    style: ResponseStyle
    action: ActionProposal = field(default_factory=ActionProposal)
    provider: str = "unknown"
    model: str | None = None
    degraded: bool = False


@dataclass
class TurnContext:
    message: str
    history: list[dict[str, str]]
    locale: str
    active_cases: list[dict[str, Any]]


@dataclass
class GateDecision:
    allowed: bool
    reason: str


class ConversationProvider(ABC):
    name = "abstract"
    degraded = False

    @abstractmethod
    async def respond(self, context: TurnContext) -> ProviderReply:
        raise NotImplementedError


def _language(text: str) -> str:
    arabic = sum(1 for c in text if "ARABIC" in unicodedata.name(c, ""))
    latin = sum(1 for c in text if "LATIN" in unicodedata.name(c, ""))
    if arabic and latin:
        return "mixed"
    return "ar" if arabic >= latin else "en"


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return value.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))


def _style(text: str) -> ResponseStyle:
    lang = _language(text)
    low = text.casefold()
    urgent_words = {"عاجل", "حالاً", "حالا", "دلوقتي", "urgent", "asap", "now"}
    upset_words = {"متضايق", "زعلان", "غاضب", "كارثة", "وحش", "angry", "upset", "terrible"}
    formal_words = {"حضرتك", "برجاء", "يرجى", "please", "kindly"}
    tokens = set(low.replace("!", " ").replace("؟", " ").replace("?", " ").split())
    mood = "upset" if tokens & upset_words else "neutral"
    urgency = "urgent" if tokens & urgent_words or text.count("!") >= 2 else "normal"
    formality = "formal" if tokens & formal_words else "casual"
    tone = "calm" if mood == "upset" or urgency == "urgent" else "warm"
    return ResponseStyle(lang, "egyptian" if lang in {"ar", "mixed"} else "international", tone, mood, urgency, formality)


class FallbackProvider(ConversationProvider):
    """A deliberately limited local provider used when no LLM is configured.

    It supports safe routing and a few social turns, but exposes degraded mode and
    never claims broad understanding.
    """
    name = "local-fallback"
    degraded = True

    def _intent(self, text: str, has_case: bool) -> Intent:
        low = _normalize(text).strip()
        tokens = set(low.replace("؟", " ").replace("?", " ").replace("!", " ").split())
        if tokens & {"نكتة", "نكته", "joke"}:
            return Intent.SMALL_TALK
        if tokens & {"مشكلة", "اتأخر", "متأخر", "غلط", "بوظ", "شكوى", "problem", "late", "wrong", "refund"}:
            return Intent.PROBLEM
        if tokens & {"أوافق", "اوافق", "أختار", "اختار", "ألغي", "الغي", "أجدد", "اجدد", "approve", "choose", "cancel", "renew"}:
            return Intent.DECISION
        if tokens & {"شحنة", "اوردر", "أوردر", "فاتورة", "حجز", "إشعار", "notification", "shipment", "invoice"} and tokens & {"وصل", "اتغير", "تحديث", "update", "arrived", "changed"}:
            return Intent.EXTERNAL_EVENT_FOLLOWUP
        if has_case and tokens & {"كمل", "كمّل", "وصل", "حصل", "فين", "continue", "status", "update"}:
            return Intent.CONTINUATION
        if tokens & {"عايز", "عاوز", "محتاج", "دورلي", "هاتلي", "احجزلي", "اشتري", "book", "find", "buy", "need", "want"}:
            return Intent.NEW_REQUEST
        if "?" in text or "؟" in text or tokens & {"ليه", "ازاي", "ايه", "اي", "يعني", "مين", "what", "why", "how", "who", "mean"}:
            return Intent.GENERAL_QUESTION
        return Intent.SMALL_TALK

    async def respond(self, context: TurnContext) -> ProviderReply:
        style = _style(context.message)
        intent = self._intent(context.message, bool(context.active_cases))
        lang = style.language
        normalized = _normalize(context.message)
        action = ActionProposal()
        if intent == Intent.SMALL_TALK and any(x in context.message.casefold() for x in ("نكت", "joke")):
            text = "مرة واحد راح يشتري راحة بال… قالوله خلصت، بس فيه انتظار مجاني 😄" if lang != "en" else "I tried to buy some peace of mind. It was out of stock, but the waiting list was free 😄"
        elif intent == Intent.NEW_REQUEST:
            explicit = any(x in context.message.casefold().split() for x in ("دورلي", "هاتلي", "احجزلي", "اشتري", "book", "find", "buy"))
            action = ActionProposal(ActionType.CREATE_REQUEST, explicit, 0.84 if explicit else 0.64, payload={"text": context.message})
            if lang == "en":
                text = "I can help with that. Tell me the one constraint that matters most, or say ‘find it’ and I’ll start." if not explicit else "I’ll start looking and I’ll keep the status precise—finding a supplier won’t be shown as contacting one."
            elif lang == "mixed":
                text = "أقدر أساعدك في ده. قولّي أهم constraint، أو قول `دورلي` وأنا أبدأ." if not explicit else "هبدأ search، وهفرّق بوضوح بين لقيت جهة، تواصلت معاها، ووصل عرض فعلي."
            else:
                text = "أقدر أساعدك في ده. قولّي أهم شرط عندك، أو قول «دورلي» وأنا أبدأ." if not explicit else "هبدأ أدور، وهقولك بدقة: لقيت جهة، اتبعت لها فعلًا، ولا وصل عرض حقيقي."
        elif intent in {Intent.CONTINUATION, Intent.EXTERNAL_EVENT_FOLLOWUP}:
            text = "شايف الموضوع المفتوح. قولي التحديث اللي وصلك وأنا أربطه بالمتابعة." if lang != "en" else "I can see the open case. Send me the update and I’ll link it to the follow-up."
            action = ActionProposal(ActionType.FOLLOW_CASE, False, 0.66)
        elif intent == Intent.PROBLEM:
            text = "ده محتاج يتسجل على الحالة نفسها. ابعتلي إيه اللي حصل بالضبط، ومش هاعتبره اتحل غير لما تأكد." if lang != "en" else "This should be recorded on the case itself. Tell me exactly what happened; I won’t mark it resolved until you confirm."
            action = ActionProposal(ActionType.RECORD_PROBLEM, False, 0.68)
        elif intent == Intent.DECISION:
            text = "قبل ما أنفّذ قرار، محتاج أربطه بالحالة والعرض المقصود بوضوح." if lang != "en" else "Before I act on that, I need to link the decision to the exact case and offer."
        elif intent == Intent.GENERAL_QUESTION:
            asks_about_mode = any(term in normalized for term in ("غير متصل", "الوضع المحدود", "قدرات المحادثه", "limited mode", "not connected"))
            if asks_about_mode:
                text = (
                    "يعني المحادثة الذكية الكاملة مش متوصلة بمزوّد ذكاء اصطناعي حاليًا. "
                    "الحفظ والمتابعة وبدء الطلبات شغالين، لكن الأسئلة المفتوحة وفهم الكلام المعقّد هيبقوا أضعف."
                    if lang != "en" else
                    "It means the full conversational AI is not connected right now. Saving, tracking, and starting requests work, but open-ended questions and complex language will be weaker."
                )
            else:
                text = "السؤال ده محتاج المحادثة الذكية الكاملة، وهي مش متصلة حاليًا. أقدر أساعدك في طلب أو متابعة محددة من غير ما أختلق إجابة." if lang != "en" else "That question needs the full conversational AI, which is not connected right now. I can still help with a specific request or follow-up without making up an answer."
        else:
            greeting = any(word in normalized.split() for word in ("اهلا", "هاي", "hello", "hi", "صباح", "مساء"))
            thanks = any(word in normalized.split() for word in ("شكرا", "متشكر", "thanks", "thank"))
            if greeting:
                text = "أهلًا 👋 قولّي محتاج إيه." if lang != "en" else "Hi 👋 What do you need?"
            elif thanks:
                text = "العفو." if lang != "en" else "You’re welcome."
            else:
                previous = next((m.get("content", "") for m in reversed(context.history) if m.get("role") == "assistant"), "")
                text = "مش لاقط قصدك في الوضع الأساسي. اكتبلي سؤالك أو طلبك بشكل مباشر شوية." if lang != "en" else "I couldn't reliably understand that in basic mode. Try stating the question or request a little more directly."
                if previous == text:
                    text = "المشكلة مش في صياغتك؛ قدرات الفهم الكاملة غير متصلة. أقدر أبدأ طلب واضح أو أتابع حالة موجودة." if lang != "en" else "The issue isn't your wording; full understanding isn't connected. I can start a clear request or follow an existing case."
        return ProviderReply(text, intent, 0.82, style, action, self.name, None, True)


SYSTEM_PROMPT = """You are Ma'ak, a capable personal service companion. Reply naturally in the user's language: Egyptian Arabic, English, or a comfortable mix. Adapt to mood, urgency, and formality. Never use canned acknowledgements such as 'تمام فهمتك'. You can chat, answer questions, tell jokes, and help move real-life matters forward.

Return ONLY a JSON object with: response, intent, confidence, style, action.
intent must be one of SMALL_TALK, NEW_REQUEST, CONTINUATION, EXTERNAL_EVENT_FOLLOWUP, PROBLEM, DECISION, GENERAL_QUESTION.
style contains language (ar/en/mixed), dialect, tone, mood, urgency, formality.
action contains type (NONE/CREATE_REQUEST/FOLLOW_CASE/RECORD_PROBLEM), authorized (boolean), confidence, case_id, case_type, payload.
Only propose CREATE_REQUEST when the customer clearly asked you to start finding, booking, buying, or arranging—not when they are asking a general question or merely discussing an idea. Never claim discovery means outreach, or outreach means an offer. Never claim an external action succeeded. The application policy layer will decide whether any proposed action may run."""


class OpenAICompatibleProvider(ConversationProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    async def respond(self, context: TurnContext) -> ProviderReply:
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(asdict(context), ensure_ascii=False)},
            ],
            "text": {"format": {"type": "json_object"}},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/responses",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if response.is_error:
                try:
                    error = response.json().get("error") or {}
                    detail = f"{error.get('type') or 'api_error'}:{error.get('code') or 'unknown'}:{error.get('message') or ''}"
                except (ValueError, AttributeError):
                    detail = "non_json_error"
                raise RuntimeError(f"OpenAI Responses API HTTP {response.status_code} {detail[:500]}")
            body = response.json()
        raw = body.get("output_text")
        if not raw:
            raw = "".join(
                part.get("text", "")
                for item in body.get("output", [])
                for part in item.get("content", [])
                if part.get("type") in {"output_text", "text"}
            )
        data = json.loads(raw)
        style = ResponseStyle(**data.get("style", {}))
        proposed = data.get("action") or {}
        action = ActionProposal(
            ActionType(proposed.get("type", "NONE")),
            bool(proposed.get("authorized", False)),
            float(proposed.get("confidence", 0)),
            proposed.get("case_id"),
            proposed.get("case_type"),
            proposed.get("payload") or {},
        )
        return ProviderReply(
            data["response"], Intent(data["intent"]), float(data.get("confidence", 0.5)),
            style, action, self.name, self.model, False,
        )


def _local_system_prompt(context: TurnContext, baseline: ProviderReply) -> str:
    style = baseline.style
    if style.language == "en":
        identity = "You are Ma'ak, a smart, practical personal companion. Reply only in natural English."
    elif style.language == "mixed":
        identity = "أنت «معاك»، صاحب مصري ذكي وعملي. رد بنفس خليط العربي والإنجليزي اللي المستخدم بيكتبه."
    else:
        identity = "أنت «معاك»، صاحب مصري ذكي وعملي. رد بالمصري الطبيعي فقط."

    normalized = _normalize(context.message)
    english = style.language == "en"
    if baseline.intent == Intent.SMALL_TALK and any(word in normalized for word in ("نكته", "نكتة", "joke")):
        task = "Tell one complete, clear joke with a punchline in two lines. Do not introduce or explain it." if english else "احكي نكتة مفهومة كاملة لها نهاية مضحكة في سطرين. لا تشرحها ولا تقدم لها."
    elif style.mood == "upset":
        task = "Respond warmly in one or two sentences and offer one small useful step. Do not ask a generic question." if english else "رد بهدوء وتعاطف في جملة أو جملتين، وادّيه خطوة صغيرة مفيدة. لا تسأله سؤالًا عامًا."
    elif baseline.intent == Intent.NEW_REQUEST and not baseline.action.authorized:
        task = "Give specific advice or options only. The user did not authorize any purchase or action, so do not claim one." if english else "ساعده بنصيحة أو اختيارات محددة فقط. هو لم يأذن بتنفيذ أو شراء أي شيء، فلا تدّعي التنفيذ."
    elif baseline.intent == Intent.NEW_REQUEST:
        task = "Briefly say you will start, without claiming a supplier was found, contacted, or sent an offer." if english else "رد باختصار إنك هتبدأ المطلوب، من غير ما تدّعي إن جهة اتوجدت أو تم التواصل أو وصل عرض."
    elif baseline.intent == Intent.PROBLEM:
        task = "Acknowledge the problem calmly, give the next step, and ask only one question if essential." if english else "اعترف بالمشكلة بهدوء وحدد الخطوة التالية، واسأل سؤالًا واحدًا فقط لو ضروري."
    elif baseline.intent == Intent.DECISION:
        task = "Help compare the decision clearly. Do not execute a decision or payment yourself." if english else "ساعده يقارن القرار بوضوح، ولا تنفذ قرارًا أو دفعًا من نفسك."
    else:
        task = "Answer directly and briefly. If unsure of a fact, say so rather than inventing it." if english else "جاوب مباشرة وباختصار. لو معلومة مش متأكد منها قل إنك مش متأكد بدل اختراعها."

    guardrail = (
        "Never start with: Got it, Okay, I understand. Do not mention internal labels or JSON. "
        "Never claim a booking, purchase, payment, supplier contact, or offer that did not happen."
        if english else
        "ممنوع تبدأ بـ: تمام فهمتك، أنا هنا احكي براحتك، حسنا. لا تذكر تصنيفات داخلية ولا JSON. "
        "لا تدّعي حجزًا أو شراءً أو دفعًا أو تواصلًا لم يحدث."
    )
    return f"{identity}\n{task}\n{guardrail}"


def _remove_canned_opening(text: str) -> str:
    value = text.strip()
    for opening in (
        "تمام فهمتك", "تمام، فهمتك", "تمام. فهمتك",
        "أنا هنا احكي براحتك", "أنا هنا أحكي براحتك", "أنا هنا. احكي براحتك",
        "Okay, I understand", "Got it", "Okay",
    ):
        if value.casefold().startswith(opening.casefold()):
            cleaned = value[len(opening):].lstrip(" .،,:!—-")
            return cleaned or value
    return value


class LocalGGUFProvider(ConversationProvider):
    """Runs an open GGUF model in-process with no paid inference API."""

    name = "local-gguf"
    degraded = False

    def __init__(
        self,
        model_path: str,
        model_name: str = "qwen2.5-1.5b-instruct-q3_k_m",
        context_window: int = 1536,
        threads: int = 2,
        chat_format: str | None = None,
    ):
        self.model_path = str(Path(model_path))
        self.model = model_name
        self.context_window = max(768, min(context_window, 4096))
        self.threads = max(1, min(threads, 8))
        self.chat_format = chat_format
        self._llm = None
        self._lock = threading.RLock()
        self._fallback = FallbackProvider()

    def _load(self):
        if self._llm is not None:
            return self._llm
        with self._lock:
            if self._llm is None:
                if not Path(self.model_path).is_file():
                    raise FileNotFoundError(f"Local model not found: {self.model_path}")
                from llama_cpp import Llama
                self._llm = Llama(
                    model_path=self.model_path,
                    n_ctx=self.context_window,
                    n_threads=self.threads,
                    n_threads_batch=self.threads,
                    n_batch=128,
                    use_mmap=True,
                    use_mlock=False,
                    chat_format=self.chat_format,
                    verbose=False,
                )
                logger.info("Local conversation model loaded: %s", self.model)
        return self._llm

    def _respond_sync(self, context: TurnContext, baseline: ProviderReply) -> str:
        llm = self._load()
        messages: list[dict[str, str]] = [{"role": "system", "content": _local_system_prompt(context, baseline)}]
        for item in context.history[-6:]:
            role = item.get("role", "user")
            if role in {"user", "assistant"}:
                messages.append({"role": role, "content": str(item.get("content", ""))[:900]})
        case_summary = [
            {"id": c.get("id"), "type": c.get("type"), "status": c.get("status"), "title": c.get("title")}
            for c in context.active_cases[:5]
        ]
        latest = context.message
        if case_summary:
            latest += "\n\nOpen cases (reference only): " + json.dumps(case_summary, ensure_ascii=False)
        messages.append({"role": "user", "content": latest})
        with self._lock:
            reply_result = llm.create_chat_completion(
                messages=messages,
                temperature=0.78,
                top_p=0.92,
                repeat_penalty=1.08,
                max_tokens=220,
            )
        return str(reply_result["choices"][0]["message"]["content"] or "")

    async def respond(self, context: TurnContext) -> ProviderReply:
        baseline = await self._fallback.respond(context)
        response_raw = await asyncio.to_thread(self._respond_sync, context, baseline)
        response_text = _remove_canned_opening(response_raw)
        if not response_text:
            response_text = baseline.text
        return ProviderReply(
            response_text,
            baseline.intent,
            baseline.confidence,
            _style(context.message),
            baseline.action,
            self.name,
            self.model,
            False,
        )


class ResilientProvider(ConversationProvider):
    def __init__(self, primary: ConversationProvider | None, fallback: ConversationProvider):
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name if primary else fallback.name
        self.degraded = primary is None

    async def respond(self, context: TurnContext) -> ProviderReply:
        if self.primary:
            try:
                return await self.primary.respond(context)
            except Exception as exc:
                logger.warning("Primary conversation provider failed; using fallback: %s", str(exc)[:700])
        return await self.fallback.respond(context)


def build_provider() -> ConversationProvider:
    local_path = os.getenv("LOCAL_MODEL_PATH", "").strip()
    if local_path and Path(local_path).is_file():
        primary = LocalGGUFProvider(
            model_path=local_path,
            model_name=os.getenv("LOCAL_MODEL_NAME", "qwen2.5-1.5b-instruct-q3_k_m"),
            context_window=int(os.getenv("LOCAL_MODEL_CONTEXT", "1536")),
            threads=int(os.getenv("LOCAL_MODEL_THREADS", "2")),
            chat_format=os.getenv("LOCAL_MODEL_CHAT_FORMAT", "").strip() or None,
        )
        return ResilientProvider(primary, FallbackProvider())

    key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("LLM_MODEL", "gpt-5-mini")
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    primary = OpenAICompatibleProvider(key, model, base) if key else None
    return ResilientProvider(primary, FallbackProvider())


def gate_action(reply: ProviderReply, active_cases: list[dict[str, Any]]) -> GateDecision:
    action = reply.action
    if action.type == ActionType.NONE:
        return GateDecision(False, "No business action proposed")
    if not action.authorized:
        return GateDecision(False, "Customer intent is not explicit enough for an external action")
    if action.confidence < 0.78:
        return GateDecision(False, "Action confidence is below the safety threshold")
    if action.type == ActionType.CREATE_REQUEST:
        if reply.intent != Intent.NEW_REQUEST:
            return GateDecision(False, "Only a new-request intent may create a request")
        return GateDecision(True, "Explicit new request passed policy")
    if action.type in {ActionType.FOLLOW_CASE, ActionType.RECORD_PROBLEM}:
        matching = [c for c in active_cases if c.get("id") == action.case_id and c.get("type") == action.case_type]
        if not matching:
            return GateDecision(False, "The proposed case is not linked to this conversation")
        if reply.intent not in {Intent.CONTINUATION, Intent.EXTERNAL_EVENT_FOLLOWUP, Intent.PROBLEM}:
            return GateDecision(False, "Intent does not permit changing a case")
        return GateDecision(True, "Explicit linked-case action passed policy")
    return GateDecision(False, "Unsupported action type")
