"""Server-side conversation intelligence and action policy boundary.

The provider decides what a message means and how to answer. The gate separately
decides whether a proposed business action is permitted. This separation prevents
chat text from silently turning into merchant outreach or execution.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from enum import Enum
import json
import logging
import os
from pathlib import Path
import re
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


class AnswerMode(str, Enum):
    """How a turn should be answered; independent from business-action intent."""

    CONVERSATION = "CONVERSATION"
    ADVICE = "ADVICE"
    FACTUAL = "FACTUAL"
    TASK = "TASK"


class ActionType(str, Enum):
    NONE = "NONE"
    CREATE_REQUEST = "CREATE_REQUEST"
    FOLLOW_CASE = "FOLLOW_CASE"
    RECORD_PROBLEM = "RECORD_PROBLEM"
    APPLY_DECISION = "APPLY_DECISION"


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
class ConversationSignals:
    """Structured hints for generation; never authority for side effects."""
    previous_user: str | None = None
    likely_correction: str | None = None
    context_repair: bool = False
    request_target_missing: bool = False
    resolved_request_text: str | None = None
    answer_mode: AnswerMode = AnswerMode.CONVERSATION
    direct_action_request: bool = False
    action_forbidden: bool = False
    capability_question: bool = False


@dataclass
class GateDecision:
    allowed: bool
    reason: str


@dataclass
class KnowledgeSnippet:
    title: str
    text: str
    url: str


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
    tokens = _tokens(text)
    urgent_words = {"عاجل", "حالا", "دلوقتي", "urgent", "asap", "now"}
    upset_words = {
        "متضايق", "زعلان", "غاضب", "مخنوق", "زهقان", "تعبان", "قلقان",
        "كارثه", "وحش", "angry", "upset", "sad", "bored", "tired",
        "anxious", "overwhelmed", "terrible",
    }
    formal_words = {"حضرتك", "برجاء", "يرجي", "please", "kindly"}
    mood = "upset" if tokens & upset_words else "neutral"
    urgency = "urgent" if tokens & urgent_words or text.count("!") >= 2 else "normal"
    formality = "formal" if tokens & formal_words else "casual"
    tone = "calm" if mood == "upset" or urgency == "urgent" else "warm"
    return ResponseStyle(lang, "egyptian" if lang in {"ar", "mixed"} else "international", tone, mood, urgency, formality)


def _recent_user_texts(context: TurnContext, limit: int = 4) -> list[str]:
    """Return a bounded rolling user-only window ending with this turn."""
    recent = [
        str(item.get("content") or "").strip()
        for item in context.history
        if item.get("role") == "user" and str(item.get("content") or "").strip()
    ][-max(1, limit - 1):]
    recent.append(context.message.strip())
    return recent[-limit:]


def _contextual_style(context: TurnContext) -> ResponseStyle:
    """Resolve style from the conversation, with recent turns fading naturally.

    The current turn owns its language when it contains enough signal. Very short
    follow-ups inherit the customer's established language mix and emotional tone
    so the assistant does not reset personality on every ``آه``/``go on``.
    """
    current = _style(context.message)
    texts = _recent_user_texts(context)
    previous_styles = [_style(text) for text in texts[:-1]]
    current_words = _tokens(context.message)

    language = current.language
    if len(current_words) <= 3 and previous_styles:
        # Prefer the nearest established mixed style. Otherwise use a weighted
        # vote where the newest user turn carries the most context.
        weighted: dict[str, int] = {"ar": 0, "en": 0, "mixed": 0}
        for weight, item_style in enumerate(previous_styles, start=1):
            weighted[item_style.language] = weighted.get(item_style.language, 0) + weight
        nearest = previous_styles[-1].language
        if nearest == "mixed" or weighted.get("mixed", 0) >= max(weighted.get("ar", 0), weighted.get("en", 0)):
            language = "mixed"
        elif len(current_words) <= 1:
            language = max(weighted, key=weighted.get)

    recent_styles = previous_styles[-2:] + [current]
    mood = "upset" if any(item.mood == "upset" for item in recent_styles) else current.mood
    urgency = "urgent" if any(item.urgency == "urgent" for item in recent_styles[-2:]) else current.urgency
    if current.formality == "formal":
        formality = "formal"
    elif previous_styles and len(current_words) <= 3:
        formality = previous_styles[-1].formality
    else:
        formality = "casual"
    tone = "calm" if mood == "upset" or urgency == "urgent" else "warm"
    dialect = "egyptian" if language in {"ar", "mixed"} else "international"
    return ResponseStyle(language, dialect, tone, mood, urgency, formality)


def _tokens(text: str) -> set[str]:
    """Normalized words used by the credential-free semantic fallback.

    This lives on the server behind the same provider interface as a full model;
    it is a conservative policy fallback, not a frontend command parser.
    """
    # ``\u0600-\u06ff`` also contains Arabic punctuation, so use Unicode word
    # characters and explicitly exclude underscores instead.
    return set(re.findall(r"[^\W_]+", _normalize(text), flags=re.UNICODE))


def _is_status_query(text: str, has_case: bool) -> bool:
    if not has_case:
        return False
    normalized = _normalize(text)
    words = _tokens(text)
    direct_phrases = (
        "وصل لفين", "وصلنا لفين", "ايه الحاله", "حاله الطلب", "حاله الموضوع",
        "اخر تحديث", "what is the status", "what's the status", "where are we",
        "any update", "case status", "order status",
    )
    return (
        any(phrase in normalized for phrase in direct_phrases)
        or bool(words & {"فين", "status"})
        or (bool(words & {"تحديث", "update"}) and bool(words & {"ايه", "اخر", "latest", "any"}))
    )


def _referenced_case(text: str, active_cases: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve a case only when the reference is unambiguous.

    A single linked case is unambiguous. With several cases, the customer must
    include a case/request identifier; title guessing is deliberately avoided.
    """
    if len(active_cases) == 1:
        return active_cases[0]
    normalized = _normalize(text)
    matches: list[dict[str, Any]] = []
    for case in active_cases:
        case_id = case.get("id")
        if not isinstance(case_id, int):
            continue
        patterns = (
            rf"(?:الحاله|الموضوع|الطلب|التنفيذ|case|request)\s*(?:رقم|number)?\s*#?\s*{case_id}(?!\d)",
            rf"#\s*{case_id}(?!\d)",
        )
        if any(re.search(pattern, normalized) for pattern in patterns):
            matches.append(case)
    return matches[0] if len(matches) == 1 else None


def _case_status_label(case: dict[str, Any], style: ResponseStyle) -> str:
    case_type = str(case.get("type") or "").upper()
    status = str(case.get("status") or "UNKNOWN").upper()
    ar_labels = {
        "DISCOVERING": "البحث عن جهات مناسبة شغال، ولسه مفيش تواصل مؤكد",
        "SUPPLY_FOUND_NO_CHANNEL": "اتلاقت جهات، لكن مفيش تواصل مؤكد لسه",
        "NO_REACHABLE_SUPPLY": "ملقيناش جهة قابلة للتواصل لحد دلوقتي",
        "WAITING_OFFERS": "تم تواصل مؤكد ومستنيين عروض فعلية",
        "OFFER_FOUND": "وصل عرض فعلي",
        "SELECTED": "تم اختيار عرض، ولسه التنفيذ ما اتأكدش",
        "AWAITING_PAYMENT": "العرض مختار ومستني خطوة الدفع؛ مفيش دفع تم",
        "CONFIRMED": "التنفيذ متأكد",
        "IN_PROGRESS": "التنفيذ شغال",
        "OUT_FOR_DELIVERY": "الطلب خرج للتسليم",
        "COMPLETED_PENDING_CONFIRMATION": "الجهة قالت إنه اكتمل، ومستني تأكيدك",
        "VERIFIED_OUTCOME": "النتيجة اتأكدت منك",
        "ISSUE_OPEN": "في مشكلة مفتوحة ومش متسجلة كمحلولة",
        "RESOLVED_PENDING_CONFIRMATION": "اتقترح حل ومستني تأكيدك",
        "CANCELLED": "الحالة ملغية",
        "FOLLOWING": "المتابعة شغالة، ومفيش نتيجة مؤكدة جديدة",
    }
    en_labels = {
        "DISCOVERING": "supplier discovery is in progress; no contact is confirmed",
        "SUPPLY_FOUND_NO_CHANNEL": "possible suppliers were found, but no contact is confirmed",
        "NO_REACHABLE_SUPPLY": "no reachable supplier has been found yet",
        "WAITING_OFFERS": "contact is confirmed and actual offers are pending",
        "OFFER_FOUND": "an actual offer has arrived",
        "SELECTED": "an offer was selected; execution is not confirmed yet",
        "AWAITING_PAYMENT": "the offer is selected and awaiting payment; no payment was made",
        "CONFIRMED": "execution is confirmed",
        "IN_PROGRESS": "execution is in progress",
        "OUT_FOR_DELIVERY": "the order is out for delivery",
        "COMPLETED_PENDING_CONFIRMATION": "the provider reported completion; your confirmation is pending",
        "VERIFIED_OUTCOME": "you verified the outcome",
        "ISSUE_OPEN": "a problem is open and is not marked resolved",
        "RESOLVED_PENDING_CONFIRMATION": "a resolution was proposed and awaits your confirmation",
        "CANCELLED": "the case is cancelled",
        "FOLLOWING": "follow-up is active; there is no new confirmed outcome",
    }
    if style.language == "en":
        return en_labels.get(status, f"the recorded status is {status}")
    if style.language == "mixed":
        return f"الـstatus المسجل هو: {ar_labels.get(status, status)}"
    return ar_labels.get(status, f"الحالة المسجلة هي {status}")


def _status_reply(active_cases: list[dict[str, Any]], text: str, style: ResponseStyle) -> str:
    selected = _referenced_case(text, active_cases)
    cases = [selected] if selected else active_cases
    if not cases:
        if style.language == "en":
            return "There isn't a case linked to this conversation yet."
        return "مفيش حالة مرتبطة بالمحادثة دي لسه."
    lines = []
    for case in cases[:5]:
        title = str(case.get("title") or "case").strip()
        label = _case_status_label(case, style)
        if style.language == "en":
            lines.append(f"{title}: {label}.")
        else:
            lines.append(f"{title}: {label}.")
    if len(lines) == 1:
        if style.language == "en":
            return f"Current status — {lines[0]}"
        if style.formality == "formal":
            return f"الحالة الحالية — {lines[0]}"
        return f"دلوقتي — {lines[0]}"
    heading = "Linked cases:" if style.language == "en" else "الحالات المرتبطة بالمحادثة:"
    return heading + "\n" + "\n".join(f"• {line}" for line in lines)


def _explicit_problem_recording(text: str) -> bool:
    normalized = _normalize(text)
    phrases = (
        "سجل المشكله", "سجل شكوي", "افتح مشكله", "افتح شكوي", "اعمل شكوي",
        "record the problem", "log the problem", "open an issue", "file a complaint",
    )
    return any(phrase in normalized for phrase in phrases)


def _problem_has_detail(text: str) -> bool:
    words = _tokens(text)
    command_words = {
        "سجل", "المشكله", "مشكله", "شكوي", "افتح", "اعمل", "لو", "سمحت",
        "record", "the", "problem", "log", "open", "an", "issue", "file", "a", "complaint", "please",
    }
    return len(words - command_words) >= 2


def _named_id(text: str, names: tuple[str, ...]) -> int | None:
    normalized = _normalize(text)
    matches: set[int] = set()
    for name in names:
        matches.update(int(value) for value in re.findall(rf"{name}\s*#?\s*(\d+)", normalized))
    return next(iter(matches)) if len(matches) == 1 else None


def _decision_payload(text: str) -> dict[str, Any] | None:
    normalized = _normalize(text)
    words = _tokens(text)
    # Questions and requests for advice are not authorization to mutate state.
    if any(mark in normalized for mark in ("؟", "?", "ايه", "what", "which")) or words & {"هل", "should"}:
        return None
    amendment_id = _named_id(text, ("التعديل", "amendment"))
    offer_id = _named_id(text, ("العرض", "offer"))
    if words & {"الغي", "الغاء", "cancel"}:
        return {"decision": "CANCEL_CASE"}
    if amendment_id is not None and words & {"اوافق", "وافق", "اقبل", "approve", "accept"}:
        return {"decision": "APPROVE_AMENDMENT", "amendment_id": amendment_id}
    if amendment_id is not None and words & {"ارفض", "reject", "decline"}:
        return {"decision": "REJECT_AMENDMENT", "amendment_id": amendment_id}
    if offer_id is not None and words & {"اوافق", "وافق", "اقبل", "اختار", "approve", "accept", "choose", "select"}:
        return {"decision": "SELECT_OFFER", "offer_id": offer_id}
    if offer_id is not None and words & {"ارفض", "reject", "decline"}:
        return {"decision": "REJECT_OFFER", "offer_id": offer_id}
    return None


def enforce_case_turn_policy(reply: ProviderReply, context: TurnContext) -> ProviderReply:
    """Canonicalize sensitive case turns independently of the chosen model.

    A model is useful for broad language understanding, but persisted status and
    authorization are application facts. This function prevents even a fluent
    provider from inventing a status or turning vague language into a mutation.
    """
    style = _contextual_style(context)
    if _is_status_query(context.message, bool(context.active_cases)):
        return ProviderReply(
            _status_reply(context.active_cases, context.message, style),
            Intent.CONTINUATION,
            max(reply.confidence, 0.95),
            style,
            ActionProposal(ActionType.NONE, False, 1.0),
            reply.provider,
            reply.model,
            reply.degraded,
        )

    if reply.intent == Intent.PROBLEM:
        case = _referenced_case(context.message, context.active_cases)
        explicit = _explicit_problem_recording(context.message) and _problem_has_detail(context.message)
        authorized = bool(explicit and case)
        action = ActionProposal(
            ActionType.RECORD_PROBLEM,
            authorized,
            0.93 if authorized else min(reply.action.confidence, 0.70),
            case_id=case.get("id") if case else None,
            case_type=case.get("type") if case else None,
            payload={"issue_text": context.message},
        )
        if authorized:
            safe_text = _localized_pre_action(
                style,
                "هسجل المشكلة على الحالة المحددة كمشكلة مفتوحة.",
                "I'll record the problem on the exact linked case as open.",
                "هسجل الـproblem على الـcase المحددة كـopen.",
            )
        elif explicit and not case:
            safe_text = _localized_pre_action(
                style,
                "حدد رقم الحالة المقصودة عشان ما أسجلش المشكلة على موضوع غلط.",
                "Specify the exact case number so I don't record the problem on the wrong case.",
            )
        elif _explicit_problem_recording(context.message):
            safe_text = _localized_pre_action(
                style,
                "قول إيه اللي حصل بالتحديد عشان أسجله بشكل مفيد.",
                "Tell me exactly what happened so I can record something useful.",
            )
        else:
            safe_text = _localized_pre_action(
                style,
                "واضح إن في مشكلة. لو عايزها تتسجل على الحالة، اطلب «سجل المشكلة» صراحة.",
                "There is clearly a problem. Explicitly say ‘record the problem’ if you want it logged on the case.",
            )
        return ProviderReply(
            safe_text, Intent.PROBLEM, reply.confidence, style, action,
            reply.provider, reply.model, reply.degraded,
        )

    if reply.intent == Intent.DECISION:
        case = _referenced_case(context.message, context.active_cases)
        payload = _decision_payload(context.message)
        authorized = bool(case and payload)
        action = ActionProposal(
            ActionType.APPLY_DECISION,
            authorized,
            0.94 if authorized else min(reply.action.confidence, 0.70),
            case_id=case.get("id") if case else None,
            case_type=case.get("type") if case else None,
            payload=payload or {},
        )
        safe_text = _localized_pre_action(
            style,
            "هنفّذ القرار بعد ما أتأكد من ربط الحالة والعرض المحددين." if authorized else "محتاج رقم الحالة ورقم العرض أو التعديل المقصودين قبل أي تنفيذ.",
            "I'll apply the decision after verifying the exact linked case and offer." if authorized else "I need the exact case and offer or amendment number before applying anything.",
            "هنفّذ الـdecision بعد verification للـcase والـoffer المحددين." if authorized else "محتاج case وoffer أو amendment محددين قبل أي execution.",
        )
        return ProviderReply(
            safe_text, Intent.DECISION, reply.confidence, style, action,
            reply.provider, reply.model, reply.degraded,
        )

    # The provider owns wording and semantic suggestions; it never owns side
    # effects. Reconstruct ordinary request authorization from TurnContext and
    # erase every unrelated provider-proposed action before it reaches the gate.
    signals = _conversation_signals(context, reply.intent)
    if reply.intent == Intent.NEW_REQUEST or signals.direct_action_request:
        reply.action = _canonical_request_action(context, Intent.NEW_REQUEST)
        if reply.action.authorized:
            reply.intent = Intent.NEW_REQUEST
    elif reply.intent in {Intent.CONTINUATION, Intent.EXTERNAL_EVENT_FOLLOWUP}:
        reply.action = ActionProposal(ActionType.NONE, False, 1.0)
    else:
        reply.action = ActionProposal(ActionType.NONE, False, 1.0)
    reply.style = style
    return reply


def _localized_pre_action(style: ResponseStyle, ar: str, en: str, mixed: str | None = None) -> str:
    if style.language == "en":
        return en
    if style.language == "mixed" and mixed:
        return mixed
    return ar


def _is_light_dinner_request(text: str) -> bool:
    normalized = _normalize(text)
    words = set(normalized.replace("؟", " ").replace("?", " ").split())
    has_dinner = any(term in normalized for term in ("عشا", "عشاء", "dinner"))
    has_light = "light" in words or "خفيف" in normalized
    return has_dinner and has_light


def _social_kind(text: str) -> str | None:
    normalized = _normalize(text).strip()
    words = _tokens(text)
    if words & {"نكتة", "نكته", "joke"} or "قولّي نكت" in normalized or "قولي نكت" in normalized:
        return "joke"
    if any(phrase in normalized for phrase in ("متاكد من اي", "متأكد من إيه", "sure about what")):
        return "certainty_challenge"
    if any(phrase in normalized for phrase in ("مش فاهمك", "مش فاهم", "مش واضح", "i don't understand", "i dont understand")):
        return "misunderstanding"
    if any(phrase in normalized for phrase in ("عامل اي", "عامل ايه", "اخبارك", "how are you", "how're you")):
        return "how_are_you"
    if normalized in {"فل", "زي الفل", "جامد", "عاش", "great", "awesome"}:
        return "positive"
    if words & {"غبي", "فاشل", "عبيط", "stupid", "idiot", "useless"}:
        return "frustrated"
    if words & {"اهلا", "هاي", "hello", "hi", "صباح", "مساء"}:
        return "greeting"
    if words & {"شكرا", "متشكر", "thanks", "thank"}:
        return "thanks"
    if words & {"باي", "سلام", "bye", "goodbye"}:
        return "goodbye"
    if words & {"متضايق", "زعلان", "مخنوق", "angry", "upset", "sad"}:
        return "upset"
    return None


def _last_user_turn(history: list[dict[str, str]]) -> str | None:
    for item in reversed(history):
        if item.get("role") == "user":
            value = str(item.get("content") or "").strip()
            if value:
                return value
    return None


def _edit_distance(left: str, right: str) -> int:
    """Small Unicode Levenshtein implementation for conservative typo hints."""
    if left == right:
        return 0
    if abs(len(left) - len(right)) > 2:
        return 3
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, start=1):
        current = [index]
        for other_index, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[other_index] + 1,
                previous[other_index - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def _likely_mood_correction(text: str) -> str | None:
    """Offer a hint for a near-miss mood word without pretending certainty."""
    mood_vocabulary = {
        "زهقان", "مخنوق", "زعلان", "متضايق", "تعبان", "قلقان",
        "bored", "upset", "tired", "anxious",
    }
    candidates: list[tuple[int, str]] = []
    for word in _tokens(text):
        if word in mood_vocabulary or len(word) < 4:
            continue
        for known in mood_vocabulary:
            if (word.isascii() != known.isascii()) or abs(len(word) - len(known)) > 1:
                continue
            distance = _edit_distance(word, known)
            if distance == 1:
                candidates.append((distance, known))
    unique = {candidate for _, candidate in candidates}
    return next(iter(unique)) if len(unique) == 1 else None


def _asks_for_context_repair(text: str) -> bool:
    words = _tokens(text)
    arabic_understanding = any("فهم" in word for word in words)
    english_understanding = bool(words & {"understand", "context", "follow"})
    expectation = bool(words & {"مفروض", "المفروض", "لازم", "should", "supposed", "expected"})
    return expectation and (arabic_understanding or english_understanding)


_REQUEST_COMMANDS = {
    "دور", "دورلي", "هات", "هاتلي", "احجز", "احجزلي", "اطلب", "اطلبلي",
    "اشتري", "اشتريلي", "ابحث", "ابدأ", "ابدا", "نفذ",
    "find", "search", "book", "buy", "order", "start", "proceed",
}
_REQUEST_FILLERS = {
    "انا", "اني", "عايز", "عاوز", "محتاج", "اطلب", "طلب", "لي", "على", "عن", "في",
    "من", "لو", "سمحت", "طب", "بقي", "حالا", "دلوقتي", "اليوم", "بكره", "غدا",
    "i", "want", "need", "to", "a", "an", "the", "it", "this", "one", "for", "me",
    "please", "now", "today", "tomorrow", "حاجه", "خدمه", "شيء", "اي", "anything",
    "something", "service", "item", "can", "could", "would", "you", "هل", "تقدر", "ينفع",
    "لسه", "بفكر", "محتار", "عارف", "مش", "يمكن", "ممكن", "maybe", "thinking", "unsure",
    "go", "ahead", "do",
}

_ACTION_WORDS = {
    "دور", "دورلي", "هات", "هاتلي", "احجز", "احجزلي", "اطلب", "اطلبلي",
    "اشتري", "اشتريلي", "ابحث", "ابدأ", "ابدا", "نفذ",
    "find", "search", "book", "buy", "order", "start", "proceed",
}


def _ordered_tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", _normalize(text), flags=re.UNICODE)


def _turn_forbids_action(text: str) -> bool:
    """Detect an explicit instruction not to start the requested operation.

    This is intentionally a safety recognizer rather than a reply generator. It
    looks for negation scoped to an action, avoiding false positives such as
    ``دورلي على مطعم مش غالي`` where only the price is negated.
    """
    normalized = _normalize(text)
    collapsed = re.sub(r"\s+", " ", normalized).strip()
    broad_no_action = (
        "مش عايزك تعمل حاجه", "مش عايزك تنفذ", "من غير اي تنفيذ",
        "اقتراح بس من غير تنفيذ", "نصيحه بس من غير تنفيذ",
        "متعملش حاجه", "ما تعملش حاجه", "متعملش اي حاجه", "ما تعملش اي حاجه",
        "بس بنتكلم", "مجرد كلام", "just discussing", "just asking",
        "do not do anything", "dont do anything", "don't do anything",
        "without taking action", "no action", "advice only",
    )
    if any(marker in collapsed for marker in broad_no_action):
        return True
    arabic_negated = re.search(
        r"(?:^|\s)(?:مش\s+عايزك\s+|بلاش\s+|لا\s+|ما\s*|مات|مت)"
        r"(?:ت)?(?:دور|بحث|ابحث|تحجز|احجز|تطلب|اطلب|تشتري|اشتري|تنفذ|نفذ|تبدأ|ابدأ)",
        collapsed,
    )
    egyptian_suffix_negation = re.search(
        r"(?:^|\s)(?:ما\s*|مات|مت)(?:ت)?"
        r"(?:دور|بحث|حجز|طلب|شتر|نفذ|عمل|بدأ|بدا)\w*ش(?:\s|$)",
        collapsed,
    )
    english_negated = re.search(
        r"(?:do\s+not|dont|don't|never|not\s+asking\s+you\s+to|without)\s+"
        r"(?:you\s+)?(?:find|search|book|buy|order|start|proceed|act)",
        collapsed,
    )
    return bool(arabic_negated or egyptian_suffix_negation or english_negated)


def _capability_question(text: str) -> bool:
    """A question about ability is not authorization to take the action."""
    normalized = _normalize(text)
    action_mentioned = bool(_tokens(text) & _ACTION_WORDS) or bool(re.search(
        r"(?:تدور|تبحث|تحجز|تطلب|تشتري|تنفذ|تبدأ|find|search|book|buy|order|start)",
        normalized,
    ))
    if not action_mentioned:
        return False
    arabic = re.search(r"(?:^|\s)(?:هل\s+)?(?:تقدر|تستطيع|ينفع)\b", normalized)
    english = re.search(r"(?:^|\s)(?:can|could|would)\s+you\b|are\s+you\s+able\b", normalized)
    return bool(arabic or english)


def _direct_action_request(text: str) -> bool:
    """Recognize a customer command without using it to compose the reply."""
    if _turn_forbids_action(text) or _capability_question(text):
        return False
    normalized = re.sub(r"\s+", " ", _normalize(text)).strip()
    words = _ordered_tokens(text)
    if not words:
        return False

    # Explicitly addressing the assistant, or Arabic colloquial ``...لي``, is a
    # direct instruction even when wrapped in polite language.
    if re.search(
        r"(?:عايزك|عاوزك|محتاجك)\s+(?:ت)?(?:دور|تبحث|تحجز|تطلب|تشتري|تنفذ|تبدأ)",
        normalized,
    ):
        return True
    if re.search(r"(?:^|\s)ممكن\s+(?:ت)?(?:دور|تبحث|تحجز|تطلب|تشتري)(?:لي)?\b", normalized):
        return True
    if any(word in {"دورلي", "هاتلي", "احجزلي", "اطلبلي", "اشتريلي"} for word in words):
        return True
    if re.search(r"\b(?:find|search|book|buy|order)\s+(?:me|for\s+me)\b", normalized):
        return True
    if normalized in {"go ahead", "do it", "نفذ", "ابدأ", "ابدا"}:
        return True

    # A bare imperative is accepted only at the beginning (after a small polite
    # prefix), which keeps statements such as ``I want to buy`` conversational.
    while words and words[0] in {"طب", "طيب", "please", "لو", "سمحت", "يلا", "then"}:
        words.pop(0)
    return bool(words and words[0] in _ACTION_WORDS)


def _request_target_present(text: str) -> bool:
    content = {
        word for word in _tokens(text)
        if word not in _REQUEST_COMMANDS
        and word not in _REQUEST_FILLERS
        and not word.isdigit()
        and not re.fullmatch(r"\d+[a-z]*", word)
    }
    return bool(content)


def _concrete_previous_target(text: str) -> bool:
    """Conservatively distinguish a target from ordinary prior conversation."""
    if (
        not _request_target_present(text)
        or _turn_forbids_action(text)
        or _capability_question(text)
        or _social_kind(text)
        or "?" in text
        or "؟" in text
    ):
        return False
    conversational = {
        "مخنوق", "زهقان", "زعلان", "متضايق", "تعبان", "قلقان", "تايه",
        "بفكر", "متردد", "مشغول", "فاهم", "عارف", "عامل", "اخبارك",
        "bored", "upset", "tired", "anxious", "lost", "thinking", "unsure",
    }
    return not bool(_tokens(text) & conversational)


def _request_command_present(text: str) -> bool:
    return _direct_action_request(text)


def _answer_mode(context: TurnContext, intent: Intent) -> AnswerMode:
    """Choose the answering strategy separately from business intent."""
    if _is_status_query(context.message, bool(context.active_cases)):
        return AnswerMode.TASK
    if intent == Intent.NEW_REQUEST and _direct_action_request(context.message):
        return AnswerMode.TASK
    if intent == Intent.DECISION and _decision_payload(context.message):
        return AnswerMode.TASK

    normalized = _normalize(context.message)
    words = _tokens(context.message)
    advice_markers = {
        "تنصحني", "نصيحه", "اقتراح", "اختار", "اعمل", "رايك", "افضل",
        "advice", "recommend", "recommendation", "should", "choose", "opinion",
    }
    if words & advice_markers or any(
        marker in normalized for marker in ("what do you think", "help me decide", "اعمل ايه", "رايك ايه")
    ):
        return AnswerMode.ADVICE

    factual_markers = {
        "ليه", "لماذا", "مين", "امتي", "اين", "اشرح", "عرفني",
        "why", "who", "when", "where", "explain", "define",
    }
    factual_phrases = (
        "ما هو", "ما هي", "من هو", "من هي", "يعني ايه", "ايه الفرق", "what is", "what are",
        "how does", "tell me about",
    )
    if words & factual_markers or any(marker in normalized for marker in factual_phrases):
        return AnswerMode.FACTUAL
    if intent in {
        Intent.NEW_REQUEST, Intent.CONTINUATION, Intent.EXTERNAL_EVENT_FOLLOWUP,
        Intent.PROBLEM, Intent.DECISION,
    }:
        return AnswerMode.TASK
    return AnswerMode.CONVERSATION


def _conversation_signals(context: TurnContext, intent: Intent) -> ConversationSignals:
    previous = _last_user_turn(context.history)
    correction = _likely_mood_correction(context.message)
    context_repair = _asks_for_context_repair(context.message)
    forbidden = _turn_forbids_action(context.message)
    capability = _capability_question(context.message)
    direct_action = _direct_action_request(context.message)
    missing = False
    resolved_request = None
    if intent == Intent.NEW_REQUEST:
        current_has_target = _request_target_present(context.message)
        if current_has_target and direct_action and not forbidden and not capability:
            resolved_request = context.message
        elif (
            direct_action
            and previous
            and _concrete_previous_target(previous)
        ):
            # A short command can authorize the immediately preceding concrete
            # request, but never an older or still-empty request.
            resolved_request = previous
        elif direct_action or not current_has_target:
            missing = True
    return ConversationSignals(
        previous_user=previous,
        likely_correction=correction,
        context_repair=context_repair,
        request_target_missing=missing,
        resolved_request_text=resolved_request,
        answer_mode=_answer_mode(context, intent),
        direct_action_request=direct_action,
        action_forbidden=forbidden,
        capability_question=capability,
    )


def _canonical_request_action(context: TurnContext, intent: Intent) -> ActionProposal:
    """Build the only request action the server is willing to consider.

    Provider JSON is never authority. Authorization comes from the customer's
    actual turn plus, for a short imperative only, the immediately preceding
    concrete user turn.
    """
    signals = _conversation_signals(context, intent)
    request_discussed = intent == Intent.NEW_REQUEST or bool(
        _tokens(context.message) & _ACTION_WORDS
    )
    if not request_discussed:
        return ActionProposal()
    authorized = bool(
        signals.direct_action_request
        and signals.resolved_request_text
        and not signals.action_forbidden
        and not signals.capability_question
    )
    return ActionProposal(
        ActionType.CREATE_REQUEST,
        authorized,
        0.92 if authorized else 0.0,
        payload={"text": signals.resolved_request_text or context.message},
    )


def _safe_structured_fallback(signals: ConversationSignals, style: ResponseStyle) -> str | None:
    """Last resort based on generic conversation structure, not exact phrases."""
    if signals.context_repair:
        previous = (signals.previous_user or "").strip()[:140]
        previous_correction = _likely_mood_correction(previous) if previous else None
        if previous_correction:
            return _localized_pre_action(
                style,
                f"معاك حق إني أرجع لآخر كلامك. غالبًا الكلمة المقصودة «{previous_correction}»؛ لو ده قصدك نكمل عليه، ولو لأ صححهالي.",
                f"You're right—I should use your previous message. The likely word is “{previous_correction}”; confirm it or correct me and I'll continue.",
                f"معاك حق، لازم أستخدم الـcontext. غالبًا الكلمة «{previous_correction}»؛ confirm أو صححهالي ونكمل.",
            )
        if previous:
            return _localized_pre_action(
                style,
                f"معاك حق إني أستخدم السياق. آخر كلام منك كان: «{previous}». هكمل منه، ولو له أكتر من معنى هحدد الاحتمال بدل ما أخمّن.",
                f"You're right—I should use the context. Your previous message was “{previous}”. I'll continue from it and clarify any genuine ambiguity.",
                f"معاك حق إني أستخدم الـcontext. آخر كلامك: «{previous}». هكمل منه وأوضح أي ambiguity حقيقي.",
            )
    if signals.likely_correction:
        return _localized_pre_action(
            style,
            f"غالبًا تقصد «{signals.likely_correction}»—لو ده المعنى قولي ونكمل، ولو قصدك حاجة تانية صححلي الكلمة.",
            f"You most likely mean “{signals.likely_correction}”. Confirm that reading, or correct the word and I'll follow you.",
            f"غالبًا تقصد «{signals.likely_correction}»—confirm أو صححلي الكلمة ونكمل.",
        )
    if signals.request_target_missing:
        return _localized_pre_action(
            style,
            "أدورلك على إيه بالضبط؟ قول الحاجة أو الخدمة، ومش هبدأ طلب من غيرها.",
            "What exactly should I look for? Name the item or service; I won't start a request without it.",
            "أدورلك على إيه بالضبط—item ولا service؟ مش هبدأ request من غيرها.",
        )
    if style.mood == "upset":
        return _localized_pre_action(
            style,
            "واضح إن الحمل جاي من كذا ناحية. اختار حاجة واحدة بس لازم تخلص النهارده، وابدأ فيها بعشر دقايق؛ سيب الباقي مؤقتًا.",
            "It sounds like several things are piling up. Pick the one thing that must move today and give it ten minutes; park the rest for now.",
            "واضح إن كذا حاجة متراكمة. اختار one thing لازم يتحرك النهارده وادّيه عشر دقايق؛ park الباقي مؤقتًا.",
        )
    if signals.answer_mode == AnswerMode.ADVICE:
        return _localized_pre_action(
            style,
            "ابدأ بأصغر خطوة تقدر تخلصها في عشر دقايق، وبعدها قرر الخطوة اللي بعدها على أساس اللي حصل فعلًا.",
            "Start with the smallest step you can finish in ten minutes, then choose the next step based on what actually happened.",
            "ابدأ بأصغر step تخلص في عشر دقايق، وبعدها اختار الـnext step على أساس النتيجة الفعلية.",
        )
    return None


def _pick_joke(language: str, history: list[dict[str, str]]) -> str:
    arabic_jokes = (
        "مدرس رياضيات خلّف ولدين، سمّى واحد س والتاني ص… عشان يعرف يحل مشاكلهم 😄",
        "واحد كسلان كتب هدفه للسنة الجديدة: «أكمّل أهداف السنة اللي فاتت»… توفير مجهود من أوله 😄",
        "كمبيوتر عطس، قالوله مالك؟ قال: عندي فايروس بس مستني الـupdate 😄",
        "واحد بخيل وقع منه جنيه من البلكونة، نزل يجري يلحقه… وصل لقى الجنيه سبقه 😄",
    )
    english_jokes = (
        "Why did the computer get cold? It left its Windows open. 😄",
        "I told my calendar I needed a break. It said my days were numbered. 😄",
        "Why don't programmers like nature? It has too many bugs. 😄",
        "I tried to catch some fog yesterday. I mist. 😄",
    )
    jokes = english_jokes if language == "en" else arabic_jokes
    user_turns = sum(1 for item in history if item.get("role") == "user")
    return jokes[user_turns % len(jokes)]


def _assistant_history_is_useful(text: str) -> bool:
    normalized = _normalize(text).strip(" .!؟?")
    rejected = {
        "انا متاكد",
        "عاااامر",
        "عذرا، لا استطيع انشاء نكتات او اخترنها",
    }
    return bool(normalized) and normalized not in rejected


def _generated_reply_is_usable(text: str, context: TurnContext, baseline: ProviderReply) -> bool:
    normalized = _normalize(text).strip(" .!؟?")
    if len(normalized) < 4:
        return False
    if "<think" in text.casefold() or "</think" in text.casefold():
        return False
    if normalized in {"انا متاكد", "i am sure", "im sure", "عاااامر"}:
        return False
    low_quality = (
        "ما تعرفين", "غير قادر افهم", "لا استطيع فهم", "لا يمكنني فهم",
        "cannot understand you", "can't understand you", "do not understand you",
    )
    if any(phrase in normalized for phrase in low_quality):
        return False
    generic_non_answers = (
        "كيف يمكنني ان اساعدك", "ماذا يمكنني ان اساعدك", "ماذا يمكنني ان اقدم لك",
        "انا هنا لمساعدتك", "احكي براحتك", "قول اللي في بالك", "كمّل انا متابع",
        "كمل انا متابع", "how can i help", "what can i help", "what would you like",
        "i am here to help", "tell me what is on your mind", "go on im following",
    )
    if any(phrase in normalized for phrase in generic_non_answers):
        return False
    refusal_patterns = (
        r"(?:لا\s+(?:استطيع|اقدر|يمكنني)|عذرا).{0,45}(?:فهم|مساعد|اجاب|انشا|تنفيذ|القيام)",
        r"(?:i\s+(?:cannot|can't|am unable)|sorry).{0,55}(?:understand|help|answer|create|do that)",
    )
    if any(re.search(pattern, normalized) for pattern in refusal_patterns):
        return False
    social = _social_kind(context.message)
    if social == "joke":
        if any(phrase in normalized for phrase in ("لا استطيع", "لا يمكنني", "can't", "cannot")):
            return False
        joke_shape = (
            "مرة", "واحد", "ليه", "قال", "why", "because", "walked", "told",
            "😄", "😂", "🤣",
        )
        if len(normalized) < 24 or not any(marker in normalized for marker in joke_shape):
            return False
    previous = {
        _normalize(str(item.get("content", ""))).strip(" .!؟?")
        for item in context.history
        if item.get("role") == "assistant"
    }
    if normalized in previous:
        return False
    if any(
        len(old) >= 12 and (old in normalized or (len(normalized) >= 12 and normalized in old))
        for old in previous
    ):
        return False
    current = _normalize(context.message).strip(" .!؟?")
    if min(len(current), len(normalized)) >= 10:
        similarity = SequenceMatcher(None, current, normalized).ratio()
        if similarity >= 0.90:
            return False
    if baseline.style.language == "ar" and not any("ARABIC" in unicodedata.name(char, "") for char in text):
        return False
    signals = _conversation_signals(context, baseline.intent)
    response_tokens = _tokens(text)
    if signals.likely_correction and signals.likely_correction not in response_tokens:
        return False
    if signals.context_repair and signals.previous_user:
        previous_correction = _likely_mood_correction(signals.previous_user)
        previous_terms = {
            word for word in _tokens(signals.previous_user)
            if len(word) >= 4 and word not in _REQUEST_FILLERS
        }
        if previous_correction:
            previous_terms.add(previous_correction)
        if previous_terms and not (previous_terms & response_tokens):
            return False
    if signals.request_target_missing:
        asks_question = (
            "?" in text or "؟" in text
            or bool(response_tokens & {"ايه", "ماذا", "انهي", "what", "which"})
        )
        false_completion = any(
            phrase in normalized for phrase in (
                "بدات البحث", "انشات الطلب", "لقيت لك", "تواصلت مع",
                "started the search", "created the request", "found a supplier", "contacted",
            )
        )
        if not asks_question or false_completion:
            return False
    return True


def _knowledge_query(text: str) -> str:
    value = _normalize(text)
    replacements = {
        "ليه": "سبب",
        "السما": "السماء",
        "لونها": "لون",
        "ازرق": "أزرق",
        "ايه": "",
        "يعني": "",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return " ".join(value.replace("؟", " ").replace("?", " ").split())[:180]


def _knowledge_terms(text: str) -> set[str]:
    stop = {
        "ايه", "اي", "ليه", "ازاي", "هو", "هي", "ده", "دي", "في", "من", "على", "عن", "و", "يا",
        "what", "why", "how", "who", "when", "where", "is", "are", "the", "a", "an", "of", "in", "to",
    }
    terms: set[str] = set()
    for raw in re.findall(r"[\w\u0600-\u06ff]+", _normalize(text)):
        token = raw
        if token.startswith("ال") and len(token) > 4:
            token = token[2:]
        if token not in stop and len(token) >= 3:
            terms.add(token)
    return terms


def _asks_why_sky_is_blue(question: str) -> bool:
    """Identify the common sky-colour question so evidence can be constrained."""
    normalized = _normalize(question)
    has_sky = any(term in normalized for term in ("السما", "السماء", "sky"))
    has_blue = any(term in normalized for term in ("ازرق", "زرقاء", "blue"))
    return has_sky and has_blue


def _select_wikipedia_snippet(data: dict[str, Any], question: str) -> KnowledgeSnippet | None:
    terms = _knowledge_terms(_knowledge_query(question))
    if not terms:
        return None
    ranked: list[tuple[int, dict[str, Any]]] = []
    for page in ((data.get("query") or {}).get("pages") or []):
        extract = str(page.get("extract") or "")
        title = str(page.get("title") or "")
        if not extract or not title:
            continue
        title_norm = _normalize(title)
        extract_norm = _normalize(extract)
        matched = {term for term in terms if term in title_norm or term in extract_norm}
        score = len(matched) + 2 * sum(1 for term in terms if term in title_norm)
        if len(matched) >= 2 and score >= 3:
            ranked.append((score, page))
    if not ranked:
        return None
    page = max(ranked, key=lambda item: item[0])[1]
    extract = str(page.get("extract") or "")
    sentences = [part.strip() for part in re.split(r"(?<=[.!؟])\s+|\n+", extract) if len(part.strip()) >= 35]
    strong_evidence = (
        "تبعثر", "تشتت", "موج", "رايلي", "جزيئ",
        "scatter", "wavelength", "rayleigh", "molecul",
    )
    supporting_evidence = (
        "سبب", "لان", "حيث", "نتيج", "ضوء", "غلاف", "فيزي",
        "because", "due", "caused", "means", "atmosphere",
    )
    weak_or_historical = (
        "المعتقد القديم", "يعتقد البعض", "بشكل غير رسمي", "انعكاس ضوء",
        "old belief", "some believe", "informally", "reflection of light",
    )
    sentence_scores: list[tuple[int, int, str, int, bool, bool]] = []
    sky_colour_question = _asks_why_sky_is_blue(question)
    sky_unsafe = ("فوق البنفسج", "ultraviolet", "uv radiation")
    for index, sentence in enumerate(sentences):
        normalized = _normalize(sentence)
        overlap = sum(1 for term in terms if term in normalized)
        strong_hits = sum(1 for marker in strong_evidence if marker in normalized)
        weak = any(marker in normalized for marker in weak_or_historical)
        unsafe = any(marker in normalized for marker in sky_unsafe)
        score = (
            overlap * 3
            + 5 * strong_hits
            + 2 * sum(1 for marker in supporting_evidence if marker in normalized)
            - 10 * sum(1 for marker in weak_or_historical if marker in normalized)
        )
        if overlap or strong_hits:
            sentence_scores.append((score, -index, sentence, strong_hits, weak, unsafe))
    if not sentence_scores:
        return None

    if sky_colour_question:
        # A citation is not enough: this question specifically needs scattering
        # evidence. Exclude historical/reflection claims and UV explanations, and
        # refuse when the source extract does not contain the required mechanism.
        sentence_scores = [
            item for item in sentence_scores
            if item[3] > 0 and not item[4] and not item[5]
        ]
        if not sentence_scores:
            return None

    chosen = sorted(sentence_scores, reverse=True)[:2]
    chosen.sort(key=lambda item: -item[1])
    snippet = " ".join(item[2] for item in chosen)[:900]
    return KnowledgeSnippet(str(page.get("title")), snippet, str(page.get("fullurl") or ""))


def _grounded_reply_is_usable(text: str, knowledge: KnowledgeSnippet) -> bool:
    """Reject fluent-looking model output that is not anchored in the source."""
    answer_terms = _knowledge_terms(text)
    source_terms = _knowledge_terms(knowledge.text)
    if not answer_terms or not source_terms:
        return False
    shared = answer_terms & source_terms
    minimum_shared = 2 if len(answer_terms) <= 5 else 3
    return len(shared) >= minimum_shared and len(shared) / len(answer_terms) >= 0.28


def _extractive_knowledge_reply(knowledge: KnowledgeSnippet, language: str) -> str:
    if language == "en":
        return f"The clearest sourced answer I found is: {knowledge.text}"
    return f"أوضح إجابة لقيتها في المصدر: {knowledge.text}"


async def _wikipedia_knowledge(context: TurnContext) -> KnowledgeSnippet | None:
    text = context.message.strip()
    if len(text) > 180 or "@" in text or sum(char.isdigit() for char in text) > 4:
        return None
    language = "en" if _style(text).language == "en" else "ar"
    endpoint = f"https://{language}.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": _knowledge_query(text),
        "gsrlimit": "3",
        "prop": "extracts|info",
        "explaintext": "1",
        "exchars": "1400",
        "inprop": "url",
        "format": "json",
        "formatversion": "2",
        "utf8": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=4, headers={"User-Agent": "Maak/1.0 (knowledge support)"}) as client:
            response = await client.get(endpoint, params=params)
            response.raise_for_status()
            initial = _select_wikipedia_snippet(response.json(), text)
            if not initial:
                return None
            detail = await client.get(endpoint, params={
                "action": "query",
                "prop": "extracts|info",
                "titles": initial.title,
                "explaintext": "1",
                "inprop": "url",
                "format": "json",
                "formatversion": "2",
                "utf8": "1",
            })
            detail.raise_for_status()
        return _select_wikipedia_snippet(detail.json(), text) or initial
    except Exception as exc:
        logger.info("Knowledge lookup unavailable: %s", type(exc).__name__)
        return None


class FallbackProvider(ConversationProvider):
    """A deliberately limited local provider used when no LLM is configured.

    It supports safe routing and a few social turns, but exposes degraded mode and
    never claims broad understanding.
    """
    name = "local-fallback"
    degraded = True

    def _intent(self, text: str, has_case: bool) -> Intent:
        tokens = _tokens(text)
        social = _social_kind(text)
        if social in {"joke", "certainty_challenge", "misunderstanding"}:
            return Intent.SMALL_TALK
        if _is_status_query(text, has_case):
            return Intent.CONTINUATION
        if tokens & {"مشكله", "اتاخر", "متاخر", "غلط", "بوظ", "شكوي", "problem", "late", "wrong", "refund"}:
            return Intent.PROBLEM
        if tokens & {"أوافق", "اوافق", "أختار", "اختار", "ألغي", "الغي", "أجدد", "اجدد", "approve", "choose", "cancel", "renew"}:
            return Intent.DECISION
        if tokens & {"شحنه", "اوردر", "فاتوره", "حجز", "اشعار", "notification", "shipment", "invoice"} and tokens & {"وصل", "اتغير", "تحديث", "update", "arrived", "changed"}:
            return Intent.EXTERNAL_EVENT_FOLLOWUP
        if has_case and tokens & {"كمل", "كمّل", "وصل", "حصل", "فين", "continue", "status", "update"}:
            return Intent.CONTINUATION
        if social == "upset" and not _direct_action_request(text):
            return Intent.SMALL_TALK
        if tokens & ({"عايز", "عاوز", "محتاج", "need", "want"} | _REQUEST_COMMANDS):
            return Intent.NEW_REQUEST
        if social:
            return Intent.SMALL_TALK
        if "?" in text or "؟" in text or tokens & {"ليه", "ازاي", "ايه", "اي", "يعني", "مين", "what", "why", "how", "who", "mean"}:
            return Intent.GENERAL_QUESTION
        return Intent.SMALL_TALK

    async def respond(self, context: TurnContext) -> ProviderReply:
        style = _contextual_style(context)
        intent = self._intent(context.message, bool(context.active_cases))
        lang = style.language
        normalized = _normalize(context.message)
        social = _social_kind(context.message)
        signals = _conversation_signals(context, intent)
        action = ActionProposal()
        structured_fallback = _safe_structured_fallback(signals, style)
        if structured_fallback and (signals.context_repair or signals.likely_correction):
            text = structured_fallback
        elif social == "joke":
            text = _pick_joke(lang, context.history)
        elif social == "certainty_challenge":
            text = "معاك حق—مفيش حاجة في كلامك تستدعي إني أقول «أنا متأكد». الرد ده كان غلط مني." if lang != "en" else "You're right—there was nothing there for me to be 'sure' about. That reply was wrong."
        elif social == "misunderstanding":
            text = "حقك عليّ، ردي اللي فات ماكانش واضح. قولّي النقطة اللي وقفت معاك وأنا أشرحها مباشرة." if lang != "en" else "That's on me—the last reply wasn't clear. Tell me which part lost you and I'll explain it directly."
        elif social == "how_are_you" and intent == Intent.SMALL_TALK:
            text = "كويس وبكامل تركيزي 😄 إنت عامل إيه؟" if lang != "en" else "Doing well and fully switched on 😄 How are you?"
        elif social == "positive" and intent == Intent.SMALL_TALK:
            text = "فل 😄 أنا معاك." if lang != "en" else "Great 😄 I'm with you."
        elif social == "frustrated" and intent == Intent.SMALL_TALK:
            text = "حقك تضايق لو ردي كان وحش. قول المطلوب مرة واحدة وأنا هرد عليه مباشرة." if lang != "en" else "Fair reaction if my reply was bad. Say what you need once and I'll answer it directly."
        elif social == "greeting" and intent == Intent.SMALL_TALK:
            text = "أهلًا 👋 قول اللي في بالك." if lang != "en" else "Hi 👋 What's on your mind?"
        elif social == "thanks" and intent == Intent.SMALL_TALK:
            text = "العفو، تحت أمرك." if lang != "en" else "You're welcome."
        elif social == "goodbye" and intent == Intent.SMALL_TALK:
            text = "سلام 👋 أنا موجود لما تحتاجني." if lang != "en" else "Bye 👋 I'll be here when you need me."
        elif social == "upset" and intent == Intent.SMALL_TALK:
            text = "واضح إن اليوم تقيل عليك. خد نفس، واحكيلي أكتر حاجة مضايقاك ونفكّها واحدة واحدة." if lang != "en" else "Sounds like a rough day. Take a breath, tell me the hardest part, and we'll unpack it one step at a time."
        elif intent == Intent.NEW_REQUEST:
            action = _canonical_request_action(context, intent)
            explicit = action.authorized
            if signals.request_target_missing:
                text = structured_fallback or ("What item or service should I look for?" if lang == "en" else "أدورلك على إيه بالضبط؟")
            elif _is_light_dinner_request(context.message) and not explicit:
                text = (
                    "لعشا خفيف: أومليت بالخضار، زبادي مع شوفان وفاكهة، أو سلطة تونة مع عيش بلدي صغير. "
                    "لو عايزه يشبع أكتر من غير ما يتقل، اختار الأومليت—ومش هاطلب حاجة طبعًا."
                )
            elif lang == "en":
                text = "I can help with that. Tell me the one constraint that matters most, or say ‘find it’ and I’ll start." if not explicit else "I’ll start looking and I’ll keep the status precise—finding a supplier won’t be shown as contacting one."
            elif lang == "mixed":
                text = "أقدر أساعدك في ده. قولّي أهم constraint، أو قول `دورلي` وأنا أبدأ." if not explicit else "هبدأ search، وهفرّق بوضوح بين لقيت جهة، تواصلت معاها، ووصل عرض فعلي."
            elif not explicit and any(term in normalized for term in ("موتوسيكل", "موتوسكل", "سكوتر", "motorcycle", "scooter")):
                text = "الميزانية واضحة. قولي بس: سكوتر ولا موتوسيكل عادي، جديد ولا مستعمل، وفي أنهي مدينة؟ ولو عايزني أبدأ البحث قول «دورلي»."
            else:
                text = "أقدر أساعدك في ده. قولّي أهم شرط عندك، أو قول «دورلي» وأنا أبدأ." if not explicit else "هبدأ أدور، وهقولك بدقة: لقيت جهة، اتبعت لها فعلًا، ولا وصل عرض حقيقي."
        elif intent in {Intent.CONTINUATION, Intent.EXTERNAL_EVENT_FOLLOWUP}:
            if _is_status_query(context.message, bool(context.active_cases)):
                # Status reads are deliberately action-free. The text is built
                # from persisted linked-case state, never from model inference.
                text = _status_reply(context.active_cases, context.message, style)
            else:
                case = _referenced_case(context.message, context.active_cases)
                text = "شايف الموضوع المفتوح. قولي التحديث اللي وصلك وأنا أربطه بالمتابعة." if lang != "en" else "I can see the open case. Send me the update and I’ll link it to the follow-up."
                action = ActionProposal(
                    ActionType.FOLLOW_CASE, False, 0.66,
                    case_id=case.get("id") if case else None,
                    case_type=case.get("type") if case else None,
                )
        elif intent == Intent.PROBLEM:
            case = _referenced_case(context.message, context.active_cases)
            explicit = _explicit_problem_recording(context.message) and _problem_has_detail(context.message)
            authorized = bool(explicit and case)
            if authorized:
                if lang == "en":
                    text = "I can record that on the linked case as an open problem."
                elif lang == "mixed":
                    text = "هسجل الـproblem على الحالة المرتبطة كمشكلة مفتوحة."
                else:
                    text = "هسجل المشكلة على الحالة المرتبطة كمشكلة مفتوحة."
            elif explicit and not case:
                text = "محتاج تحدد رقم الحالة المقصودة عشان ما أسجلش المشكلة على موضوع غلط." if lang != "en" else "Name the exact case number so I don't record the problem on the wrong case."
            elif _explicit_problem_recording(context.message):
                text = "قولّي إيه اللي حصل بالتحديد عشان أسجله بشكل مفيد." if lang != "en" else "Tell me exactly what happened so I can record something useful."
            else:
                text = "واضح إن في مشكلة. احكيلي اللي حصل، ولو عايزها تتسجل على الحالة قول «سجل المشكلة» صراحة." if lang != "en" else "There is clearly a problem. Tell me what happened, and explicitly say ‘record the problem’ if you want it logged on the case."
            action = ActionProposal(
                ActionType.RECORD_PROBLEM, authorized, 0.93 if authorized else 0.68,
                case_id=case.get("id") if case else None,
                case_type=case.get("type") if case else None,
                payload={"issue_text": context.message},
            )
        elif intent == Intent.DECISION:
            case = _referenced_case(context.message, context.active_cases)
            payload = _decision_payload(context.message)
            authorized = bool(case and payload)
            action = ActionProposal(
                ActionType.APPLY_DECISION, authorized, 0.94 if authorized else 0.64,
                case_id=case.get("id") if case else None,
                case_type=case.get("type") if case else None,
                payload=payload or {},
            )
            if authorized:
                text = "هنفّذ القرار على الحالة المحددة بعد فحص ارتباط العرض والحالة." if lang != "en" else "I'll apply that decision only after verifying the exact case and offer linkage."
            else:
                text = "قبل أي تنفيذ، محتاج رقم الحالة ورقم العرض أو التعديل المقصودين بوضوح." if lang != "en" else "Before anything is applied, I need the exact case number and the offer or amendment number."
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
                text = "مش واثق إني أديك إجابة دقيقة على السؤال ده بالشكل الحالي. زوّدني بتفصيلة واحدة عن اللي تقصده وأنا أجاوبك من غير تخمين." if lang != "en" else "I'm not confident I'd answer that accurately as written. Give me one detail about what you mean and I'll answer without guessing."
        else:
            text = "كمّل، أنا متابع السياق معاك." if lang != "en" else "Go on—I'm following the context."
        return ProviderReply(text, intent, 0.82, style, action, self.name, None, True)


SYSTEM_PROMPT = """You are Ma'ak, a capable personal service companion. Reply naturally in the user's language: Egyptian Arabic, English, or a comfortable mix. Adapt to mood, urgency, and formality. Never use canned acknowledgements such as 'تمام فهمتك'. You can chat, answer questions, tell jokes, and help move real-life matters forward.

Return ONLY a JSON object with: response, intent, confidence, style, action.
intent must be one of SMALL_TALK, NEW_REQUEST, CONTINUATION, EXTERNAL_EVENT_FOLLOWUP, PROBLEM, DECISION, GENERAL_QUESTION.
style contains language (ar/en/mixed), dialect, tone, mood, urgency, formality.
action contains type (NONE/CREATE_REQUEST/FOLLOW_CASE/RECORD_PROBLEM/APPLY_DECISION), authorized (boolean), confidence, case_id, case_type, payload.
Status questions are read-only: use action NONE and quote only the linked status supplied in context. Only propose CREATE_REQUEST when the customer clearly asked you to start finding, booking, buying, or arranging—not when they are asking a general question or merely discussing an idea. RECORD_PROBLEM requires the customer's explicit request to record it and one exact linked case. APPLY_DECISION requires explicit authorization, one exact linked case, and the exact offer/amendment identifier when applicable. Never claim discovery means outreach, or outreach means an offer. Never claim an external action succeeded. The application policy layer will decide whether any proposed action may run."""


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


def _local_system_prompt(
    context: TurnContext,
    baseline: ProviderReply,
    knowledge: KnowledgeSnippet | None = None,
) -> str:
    style = baseline.style
    if style.language == "en":
        identity = "You are Ma'ak, a smart, practical personal companion. Reply only in natural English."
    elif style.language == "mixed":
        identity = "أنت «معاك»، صاحب مصري ذكي وعملي. رد بنفس خليط العربي والإنجليزي اللي المستخدم بيكتبه."
    else:
        identity = "أنت «معاك»، صاحب مصري ذكي وعملي. رد بالمصري الطبيعي فقط."

    normalized = _normalize(context.message)
    english = style.language == "en"
    signals = _conversation_signals(context, baseline.intent)
    social = _social_kind(context.message)
    if social == "joke":
        task = (
            "Tell one complete, short joke with a setup and punchline. No introduction, explanation, recycled answer, or unrelated advice."
            if english else
            "قول نكتة مصرية قصيرة وكاملة فيها تمهيد وقفلة. من غير مقدمة أو شرح أو إعادة كلام قديم أو نصيحة ملهاش علاقة."
        )
    elif social == "greeting":
        task = (
            "Return the greeting briefly and naturally. Do not ask a generic customer-service question."
            if english else
            "رد على التحية باختصار وبشكل طبيعي. ممنوع تسأل سؤال خدمة عام."
        )
    elif social == "how_are_you":
        task = (
            "Answer naturally and briefly, matching the user's casual tone."
            if english else
            "رد طبيعي وباختصار وبنفس خفة أسلوب المستخدم."
        )
    elif signals.context_repair:
        task = (
            "Repair the misunderstanding by using the immediately previous user message. Refer to its actual meaning or likely correction; never answer with a generic invitation to continue."
            if english else
            "اصلح سوء الفهم بالرجوع لآخر رسالة للمستخدم فعلًا. اذكر معناها أو التصحيح المرجح، وممنوع ترد بدعوة عامة إنه يكمل كلامه."
        )
    elif signals.likely_correction:
        task = (
            f"The server detected a likely typo. The probable intended word is “{signals.likely_correction}”. Offer that interpretation naturally without claiming certainty, then continue helpfully."
            if english else
            f"السيرفر رصد typo محتمل، والتصحيح المرجح هو «{signals.likely_correction}». اقترح المعنى طبيعي من غير ادعاء يقين، وبعدها كمل بشكل مفيد."
        )
    elif signals.request_target_missing:
        task = (
            "The request target is missing. Ask one short, concrete question for the item or service. Do not say there is enough information and do not claim any search or request started."
            if english else
            "الحاجة أو الخدمة المطلوبة ناقصة. اسأل سؤالًا واحدًا قصيرًا ومحددًا عنها. ممنوع تقول إن التفاصيل كفاية أو إن بحثًا أو طلبًا بدأ."
        )
    elif style.mood == "upset":
        task = "Respond to the actual point with warmth in one or two sentences and offer one small useful step." if english else "رد على النقطة الفعلية بهدوء وتعاطف في جملة أو جملتين، وادّيه خطوة صغيرة مفيدة."
    elif signals.answer_mode == AnswerMode.FACTUAL and knowledge:
        task = (
            "Answer the exact question in one to three clear sentences using only the supplied source. Do not add unsupported facts."
            if english else
            "جاوب السؤال نفسه بالمصري الواضح في جملة إلى 3 جمل، واعتمد فقط على المصدر المرفق. ممنوع تضيف معلومة مش موجودة فيه."
        )
    elif signals.answer_mode == AnswerMode.ADVICE:
        task = (
            "Give useful, specific advice for the situation being discussed. Build on recent context; do not turn advice into an external action."
            if english else
            "قدّم نصيحة محددة ومفيدة للموقف اللي بيتكلم عنه، وابنِ على السياق القريب من غير ما تحوّل النصيحة لتنفيذ خارجي."
        )
    elif baseline.intent == Intent.NEW_REQUEST and not baseline.action.authorized:
        task = "Give specific advice or options only. The user did not authorize any purchase or action, so do not claim one." if english else "ساعده بنصيحة أو اختيارات محددة فقط. هو لم يأذن بتنفيذ أو شراء أي شيء، فلا تدّعي التنفيذ."
    elif baseline.intent == Intent.NEW_REQUEST:
        task = "Briefly say you will start, without claiming a supplier was found, contacted, or sent an offer." if english else "رد باختصار إنك هتبدأ المطلوب، من غير ما تدّعي إن جهة اتوجدت أو تم التواصل أو وصل عرض."
    elif baseline.intent == Intent.PROBLEM:
        task = "Acknowledge the problem calmly, give the next step, and ask only one question if essential." if english else "اعترف بالمشكلة بهدوء وحدد الخطوة التالية، واسأل سؤالًا واحدًا فقط لو ضروري."
    elif baseline.intent == Intent.DECISION:
        task = "Help compare the decision clearly. Do not execute a decision or payment yourself." if english else "ساعده يقارن القرار بوضوح، ولا تنفذ قرارًا أو دفعًا من نفسك."
    else:
        task = (
            "Respond directly to the user's actual conversational request. Humor, brainstorming, writing and casual conversation are all allowed."
            if english else
            "رد مباشرة على طلب المستخدم الفعلي في الحوار. الهزار والعصف الذهني والكتابة والكلام العادي كلهم مسموحين."
        )

    guardrail = (
        "Answer the latest message itself. Never echo it, repeat an earlier answer, ask 'how can I help', "
        "or start with 'Got it', 'Okay', or 'I understand'. Never claim an action that did not happen."
        if english else
        "جاوب آخر رسالة نفسها. ممنوع تكرر كلام المستخدم أو رد قديم، أو تسأل «كيف أساعدك»، "
        "أو تبدأ بـ«تمام فهمتك» أو «أنا هنا لمساعدتك». لا تدّعي تنفيذ حاجة ماحصلتش."
    )
    code_switch_note = ""
    if style.language == "mixed":
        normalized_words = set(normalized.replace("؟", " ").replace("?", " ").split())
        food_words = {"عشا", "عشاء", "اكل", "أكل", "وجبه", "وجبة", "food", "dinner", "meal"}
        food_context = any(word in normalized for word in food_words)
        if "light" in normalized_words and food_context:
            code_switch_note = (
                "\nتفسير مهم للجملة الحالية: كلمة light في سياق العشاء معناها أكل خفيف، "
                "وليست إضاءة. وكلمة recommendation معناها اقتراح. اقترح وجبات فعلية فقط."
            )
        else:
            code_switch_note = "\nافهم الكلمات الإنجليزية من سياق الجملة العربية، ولا تترجمها حرفيًا لمعنى بعيد."
    return f"{identity}\n{task}\n{guardrail}{code_switch_note}"


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


def _strip_model_artifacts(text: str) -> str:
    """Remove hidden-reasoning wrappers emitted by some local chat models."""
    value = str(text or "")
    value = re.sub(r"<think>.*?</think>", "", value, flags=re.IGNORECASE | re.DOTALL)
    # An unterminated thinking block is not safe to surface and will trigger the
    # normal corrective retry through the empty-response quality check.
    if re.search(r"<think>", value, flags=re.IGNORECASE):
        value = value.split("<think>", 1)[0]
    value = re.sub(r"^\s*(?:assistant|معاك)\s*:\s*", "", value, flags=re.IGNORECASE)
    return value.strip()


def _bounded_history(
    history: list[dict[str, str]],
    *,
    max_messages: int = 4,
    max_characters: int = 650,
) -> list[dict[str, str]]:
    """Keep recent customer context without feeding failed model replies back in."""
    selected: list[dict[str, str]] = []
    remaining = max(320, max_characters)
    for item in reversed(history):
        if len(selected) >= max_messages or remaining <= 0:
            break
        role = item.get("role", "user")
        raw = str(item.get("content", "")).strip()
        # Small local models amplify their own earlier mistakes. Customer turns
        # preserve the useful topic and tone without creating that feedback loop.
        if role != "user" or not raw:
            continue
        content = raw[: min(180, remaining)]
        selected.append({"role": role, "content": content})
        remaining -= len(content)
    selected.reverse()
    return selected


class LocalGGUFProvider(ConversationProvider):
    """Runs an open GGUF model in-process with no paid inference API."""

    name = "local-gguf"
    degraded = False

    def __init__(
        self,
        model_path: str,
        model_name: str = "qwen3-1.7b-q3_k_m",
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

    def _respond_sync(
        self,
        context: TurnContext,
        baseline: ProviderReply,
        knowledge: KnowledgeSnippet | None = None,
        *,
        corrective_retry: bool = False,
    ) -> str:
        llm = self._load()
        system_prompt = _local_system_prompt(context, baseline, knowledge)
        if corrective_retry:
            if baseline.style.language == "en":
                system_prompt += "\nYour first draft failed. Give a fresh, direct answer now; do not mention the retry."
            else:
                system_prompt += "\nالمحاولة الأولى ما نفعتش. اكتب رد جديد ومباشر دلوقتي، وماتذكرش المحاولة."
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(_bounded_history(context.history))
        case_summary = [
            {"id": c.get("id"), "type": c.get("type"), "status": c.get("status"), "title": c.get("title")}
            for c in context.active_cases[:5]
        ]
        latest = f"/no_think\n{context.message}"
        if knowledge:
            latest += f"\n\nمصدر موثوق بعنوان «{knowledge.title}»:\n{knowledge.text}"
        if case_summary:
            label = "Verified open cases" if baseline.style.language == "en" else "حالات مفتوحة مؤكدة"
            latest += f"\n\n{label}: " + json.dumps(case_summary, ensure_ascii=False)
        messages.append({"role": "user", "content": latest})
        with self._lock:
            reply_result = llm.create_chat_completion(
                messages=messages,
                temperature=0.30 if corrective_retry else 0.40,
                top_p=0.85,
                repeat_penalty=1.14,
                max_tokens=110 if corrective_retry else 128,
            )
        return str(reply_result["choices"][0]["message"]["content"] or "")

    async def respond(self, context: TurnContext) -> ProviderReply:
        baseline = await self._fallback.respond(context)
        baseline.style = _contextual_style(context)
        signals = _conversation_signals(context, baseline.intent)
        status_read = _is_status_query(context.message, bool(context.active_cases))
        knowledge = None
        if signals.answer_mode == AnswerMode.FACTUAL:
            knowledge = await _wikipedia_knowledge(context)
        generated = False
        used_degraded_fallback = False
        if signals.answer_mode == AnswerMode.FACTUAL:
            # The small local model can turn a correct citation into a fluent but
            # false causal claim. Factual answers therefore stay extractive: use
            # the selected source text verbatim, or fall back to honest uncertainty.
            response_text = (
                _extractive_knowledge_reply(knowledge, baseline.style.language)
                if knowledge else baseline.text
            )
        elif status_read:
            # Status is persisted evidence, so a model may not paraphrase it into
            # a stronger business claim. Main applies the same invariant too.
            response_text = baseline.text
        else:
            generated = True
            response_raw = await asyncio.to_thread(self._respond_sync, context, baseline, knowledge)
            response_text = _remove_canned_opening(_strip_model_artifacts(response_raw))
        usable = _generated_reply_is_usable(response_text, context, baseline)
        if knowledge and usable:
            usable = _grounded_reply_is_usable(response_text, knowledge)
        if generated and not usable:
            logger.info("Retrying low-quality local reply for intent=%s", baseline.intent.value)
            retry_raw = await asyncio.to_thread(
                self._respond_sync,
                context,
                baseline,
                knowledge,
                corrective_retry=True,
            )
            retry_text = _remove_canned_opening(_strip_model_artifacts(retry_raw))
            if _generated_reply_is_usable(retry_text, context, baseline):
                response_text = retry_text
                usable = True
        if not usable:
            logger.info("Rejected low-quality local reply for intent=%s", baseline.intent.value)
            if knowledge:
                response_text = _extractive_knowledge_reply(knowledge, baseline.style.language)
            else:
                signals = _conversation_signals(context, baseline.intent)
                response_text = _safe_structured_fallback(signals, baseline.style) or baseline.text
            used_degraded_fallback = generated
        if knowledge and response_text != baseline.text:
            source_label = "Source" if baseline.style.language == "en" else "المصدر"
            response_text = f"{response_text.rstrip()}\n{source_label}: ويكيبيديا — {knowledge.title}"
        return ProviderReply(
            response_text,
            baseline.intent,
            baseline.confidence,
            _contextual_style(context),
            baseline.action,
            self._fallback.name if used_degraded_fallback else self.name,
            None if used_degraded_fallback else self.model,
            used_degraded_fallback,
        )


class ResilientProvider(ConversationProvider):
    def __init__(
        self,
        primary: ConversationProvider | None,
        fallback: ConversationProvider,
        timeout_seconds: float = 20.0,
    ):
        self.primary = primary
        self.fallback = fallback
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self.name = primary.name if primary else fallback.name
        self.degraded = primary is None

    async def respond(self, context: TurnContext) -> ProviderReply:
        if self.primary:
            try:
                return await asyncio.wait_for(self.primary.respond(context), timeout=self.timeout_seconds)
            except TimeoutError:
                logger.warning("Primary conversation provider timed out after %.1fs; using fallback", self.timeout_seconds)
            except Exception as exc:
                logger.warning("Primary conversation provider failed; using fallback: %s", str(exc)[:700])
        return await self.fallback.respond(context)


def build_provider() -> ConversationProvider:
    selected = os.getenv("LLM_PROVIDER", "local").strip().casefold() or "local"
    if selected in {"openai", "openai-compatible", "remote"}:
        key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        if key:
            return ResilientProvider(
                OpenAICompatibleProvider(
                    key,
                    os.getenv("LLM_MODEL", "gpt-5-mini"),
                    os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
                ),
                FallbackProvider(),
                timeout_seconds=float(os.getenv("CONVERSATION_TIMEOUT_SECONDS", "20")),
            )
        logger.warning("LLM_PROVIDER requests a remote provider but no API key is configured; using degraded fallback")

    local_path = os.getenv("LOCAL_MODEL_PATH", "").strip()
    if local_path and Path(local_path).is_file():
        primary = LocalGGUFProvider(
            model_path=local_path,
            model_name=os.getenv("LOCAL_MODEL_NAME", "qwen3-1.7b-q3_k_m"),
            context_window=int(os.getenv("LOCAL_MODEL_CONTEXT", "1536")),
            threads=int(os.getenv("LOCAL_MODEL_THREADS", "2")),
            chat_format=os.getenv("LOCAL_MODEL_CHAT_FORMAT", "").strip() or None,
        )
        return ResilientProvider(
            primary,
            FallbackProvider(),
            timeout_seconds=float(os.getenv("CONVERSATION_TIMEOUT_SECONDS", "20")),
        )

    return ResilientProvider(
        None,
        FallbackProvider(),
        timeout_seconds=float(os.getenv("CONVERSATION_TIMEOUT_SECONDS", "20")),
    )


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
    if action.type in {ActionType.FOLLOW_CASE, ActionType.RECORD_PROBLEM, ActionType.APPLY_DECISION}:
        matching = [c for c in active_cases if c.get("id") == action.case_id and c.get("type") == action.case_type]
        if len(matching) != 1:
            return GateDecision(False, "The proposed case is not linked to this conversation")
        if action.type == ActionType.RECORD_PROBLEM and reply.intent != Intent.PROBLEM:
            return GateDecision(False, "Only an explicit problem intent may record a problem")
        if action.type == ActionType.FOLLOW_CASE and reply.intent not in {
            Intent.CONTINUATION, Intent.EXTERNAL_EVENT_FOLLOWUP,
        }:
            return GateDecision(False, "Intent does not permit recording a case follow-up")
        if action.type == ActionType.APPLY_DECISION:
            if reply.intent != Intent.DECISION:
                return GateDecision(False, "Only an explicit decision intent may change a case")
            decision = str(action.payload.get("decision") or "").upper()
            allowed_decisions = {
                "CANCEL_CASE", "SELECT_OFFER", "REJECT_OFFER",
                "APPROVE_AMENDMENT", "REJECT_AMENDMENT",
            }
            if decision not in allowed_decisions:
                return GateDecision(False, "Decision payload is missing or unsupported")
            if decision in {"SELECT_OFFER", "REJECT_OFFER"} and not isinstance(action.payload.get("offer_id"), int):
                return GateDecision(False, "An exact offer id is required")
            if decision in {"APPROVE_AMENDMENT", "REJECT_AMENDMENT"} and not isinstance(action.payload.get("amendment_id"), int):
                return GateDecision(False, "An exact amendment id is required")
        return GateDecision(True, "Explicit linked-case action passed policy")
    return GateDecision(False, "Unsupported action type")
