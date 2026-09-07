"""Server-side conversation intelligence and action policy boundary.

The provider decides what a message means and how to answer. The gate separately
decides whether a proposed business action is permitted. This separation prevents
chat text from silently turning into merchant outreach or execution.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import os
import unicodedata
from typing import Any

import httpx


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
        low = text.casefold().strip()
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
        if "?" in text or "؟" in text or tokens & {"ليه", "إزاي", "ازاي", "إيه", "ايه", "مين", "what", "why", "how", "who"}:
            return Intent.GENERAL_QUESTION
        return Intent.SMALL_TALK

    async def respond(self, context: TurnContext) -> ProviderReply:
        style = _style(context.message)
        intent = self._intent(context.message, bool(context.active_cases))
        lang = style.language
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
            text = "أنا شغال دلوقتي بوضع محدود، فممكن أساعد في الأسئلة البسيطة ومسارات المتابعة، لكن مش هادّعي إجابة كاملة من غير مزوّد الذكاء المتصل." if lang != "en" else "I’m currently in limited mode. I can handle simple questions and follow-up routing, but I won’t pretend to give a full answer without the connected AI provider."
        else:
            text = "أنا هنا. احكي براحتك." if lang != "en" else "I’m here. Go ahead."
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
            response.raise_for_status()
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
            except Exception:
                pass
        return await self.fallback.respond(context)


def build_provider() -> ConversationProvider:
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
