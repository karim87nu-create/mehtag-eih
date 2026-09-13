"""Deterministic first-pass routing for every chat turn.

The router describes the capability a turn belongs to.  It never authorizes a
side effect: request execution remains behind the existing conversation gate.
Keeping this cheap and conservative also avoids invoking the local model for
controls, media, status and small-talk turns with deterministic answers.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import unicodedata


class Route(str, Enum):
    CASUAL_CHAT = "casual_chat"
    FACTUAL_QUESTION = "factual_question"
    COMPARISON_RECOMMENDATION = "comparison_recommendation"
    MEDIA_IMAGE_REQUEST = "media/image_request"
    REQUEST_SEED = "request_seed"
    REQUEST_DETAIL = "request_detail"
    REQUEST_EXECUTE = "request_execute"
    REQUEST_CANCEL = "request_cancel"
    REQUEST_STATUS_FOLLOWUP = "request_status/followup"
    EXTERNAL_ACTION = "external_action"
    FUTURE_TASK = "future_task"


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    confidence: float
    deterministic: bool = True


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", " ".join((text or "").split())).casefold()
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return value.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"}))


def words(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", normalize(text), flags=re.UNICODE))


_EXECUTE = {"ابدا", "ابدا البحث", "ابدا دلوقتي", "نفذ", "دور", "دورلي", "كمل وابدا", "ابدا التنفيذ", "start", "go", "go ahead", "proceed", "search now"}
_CANCEL = {"بلاش", "خلاص بلاش", "الغيه", "الغي", "متبداش", "ما تبداش", "متنفذش", "ما تنفذش", "مش عايز", "خلاص مش عايز", "cancel", "never mind", "nevermind", "stop", "don't start", "do not start"}
_CASUAL = {"بص", "بصي", "اسمع", "اسمعني", "شكرا", "متشكر", "تسلم", "اهلا", "هاي", "hello", "hi", "باي", "bye", "thanks", "thank you"}
_AREAS = {"مدينه نصر", "التجمع", "القاهره الجديده", "مصر الجديده", "المعادي", "مدينتي", "الرحاب", "الدقي", "المهندسين", "الزمالك", "الهرم", "الجيزه", "شبرا", "حلوان", "اكتوبر", "الشيخ زايد", "القاهره"}


def route_turn(text: str, *, draft_exists: bool = False) -> RouteDecision:
    value = normalize(text)
    token_set = words(text)
    if value in _EXECUTE:
        return RouteDecision(Route.REQUEST_EXECUTE, 1.0)
    if value in _CANCEL:
        return RouteDecision(Route.REQUEST_CANCEL, 1.0)
    if value in _CASUAL:
        return RouteDecision(Route.CASUAL_CHAT, 1.0)

    if re.search(r"(?:صور|صوره|photos?|pictures?|images?)", value) and (
        token_set & {"عايز", "عاوز", "محتاج", "وريني", "ورني", "اعرضلي", "فرجني", "show"}
        or value.startswith(("صور", "صوره"))
    ):
        return RouteDecision(Route.MEDIA_IMAGE_REQUEST, 0.99)

    if any(p in value for p in ("فين الطلب", "حاله الطلب", "الطلب وصل", "وصل لفين", "وصلنا لفين", "اخر تحديث", "order status", "request status", "where is my order")):
        return RouteDecision(Route.REQUEST_STATUS_FOLLOWUP, 0.98)

    if any(p in value for p in ("فكرني", "بكره", "بعد ساعه", "remind me", "tomorrow", "later")):
        return RouteDecision(Route.FUTURE_TASK, 0.95)
    if any(p in value for p in ("ابعته ل", "ابعت ل", "كلم ", "send it to", "message ")):
        return RouteDecision(Route.EXTERNAL_ACTION, 0.94)

    if any(p in value for p in ("رشحلي", "قارن", "ايه الافضل", "مين الافضل", "هو ده كويس", "انا متردد", "رايك", "recommend", "compare", "is this good", "i am unsure")):
        return RouteDecision(Route.COMPARISON_RECOMMENDATION, 0.97)

    # Explicit information-only language wins over generic "عايز" request
    # wording, including while a draft is live.
    if any(p in value for p in ("عايز اعرف", "عاوز اعرف", "محتاج اعرف", "مش اشتري", "للمعرفه", "ايه الفرق", "اشرحلي", "يعني ايه", "i want to know", "not buy")):
        return RouteDecision(Route.FACTUAL_QUESTION, 0.99)
    if "?" in text or "؟" in text or token_set & {"ليه", "ازاي", "ايه", "مين", "امتي", "what", "why", "how", "who", "when", "which"}:
        return RouteDecision(Route.FACTUAL_QUESTION, 0.93)

    seed = bool(re.search(r"(?:^|\s)(?:عايز|عاوز|محتاج|محتاجه|عايزه|عاوزه|بدور\s+عل[ىي]|i\s+want|i\s+need|looking\s+for)(?:\s|$)", value))
    if seed:
        return RouteDecision(Route.REQUEST_SEED, 0.94)

    if draft_exists:
        detail_markers = {"ميزانيه", "جنيه", "الف", "جديد", "مستعمل", "لون", "مقاس", "موديل", "قبل", "حدود", "budget", "new", "used", "size", "color", "model"}
        model_or_amount = any(any(ch.isdigit() for ch in token) for token in token_set)
        area = any(place in value for place in _AREAS)
        if len(value.split()) <= 8 and (token_set & detail_markers or model_or_amount or area or len(value.split()) <= 3):
            return RouteDecision(Route.REQUEST_DETAIL, 0.88)

    return RouteDecision(Route.CASUAL_CHAT, 0.65, deterministic=False)
