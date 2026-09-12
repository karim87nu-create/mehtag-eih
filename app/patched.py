"""Production entrypoint with a narrow draft-preservation layer.

The base conversation model remains responsible for general chat.  This module
only hardens one operational invariant: a request built over several short
turns must keep its details until the customer explicitly says to start.
"""
from __future__ import annotations

import re

from . import main as main_module
from .conversation import ActionProposal, ActionType, Intent, ProviderReply


app = main_module.app
_original_provider = main_module.conversation_provider
_original_policy = main_module.enforce_case_turn_policy


_AR_REQUEST_START = re.compile(
    r"(?:^|\s)(?:عايز|عاوز|محتاج|محتاجه|عايزه|عاوزه|بدور\s+على|بدور\s+علي)(?:\s|$)",
    re.IGNORECASE,
)
_EN_REQUEST_START = re.compile(r"\b(?:i\s+want|i\s+need|looking\s+for)\b", re.IGNORECASE)

_CONTROL_EXECUTE = {
    "ابدأ", "ابدا", "ابدأ البحث", "ابدا البحث", "ابدأ دلوقتي", "ابدا دلوقتي",
    "نفذ", "نفّذ", "دورلي", "دور", "كمل وابدأ", "ابدأ التنفيذ", "ابدا التنفيذ",
    "start", "go", "go ahead", "proceed", "search now",
}
_CONTROL_CANCEL = {
    "بلاش", "خلاص بلاش", "الغيه", "الغي", "إلغي", "متبدأش", "ما تبدأش",
    "متنفذش", "ما تنفذش", "مش عايز", "خلاص مش عايز", "cancel", "never mind",
    "nevermind", "stop", "don't start", "do not start",
}
_SMALL_TALK = {
    "شكرا", "شكرًا", "متشكر", "تسلم", "تمام شكرا", "thanks", "thank you",
    "اهلا", "أهلا", "هاي", "hello", "hi", "باي", "bye",
}
_AREAS = (
    "مدينة نصر", "التجمع", "القاهرة الجديدة", "مصر الجديدة", "المعادي",
    "مدينتي", "الرحاب", "الدقي", "المهندسين", "الزمالك", "الهرم",
    "الجيزة", "شبرا", "حلوان", "6 أكتوبر", "اكتوبر", "الشيخ زايد", "القاهرة",
)


def _clean(value: str) -> str:
    return " ".join((value or "").strip().split())


def _norm(value: str) -> str:
    text = _clean(value).lower()
    return text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"}))


def _is_request_seed(text: str) -> bool:
    value = _clean(text)
    return bool(_AR_REQUEST_START.search(value) or _EN_REQUEST_START.search(value))


def _is_execute_control(text: str) -> bool:
    value = _norm(text)
    return value in {_norm(item) for item in _CONTROL_EXECUTE}


def _is_cancel_control(text: str) -> bool:
    value = _norm(text)
    return value in {_norm(item) for item in _CONTROL_CANCEL}


def _is_small_talk(text: str) -> bool:
    return _norm(text) in {_norm(item) for item in _SMALL_TALK}


def _user_turns(context) -> list[str]:
    turns = [
        _clean(str(item.get("content") or ""))
        for item in context.history
        if item.get("role") == "user" and _clean(str(item.get("content") or ""))
    ]
    turns.append(_clean(context.message))
    return turns


def _draft_segment(context) -> list[str]:
    """Return the current not-yet-executed request segment, if one exists."""
    turns = _user_turns(context)
    current_index = len(turns) - 1

    # A previous explicit execute/cancel command closes the older draft.  This
    # prevents details from an already-created or abandoned request leaking into
    # a later one.
    start_window = 0
    for index, turn in enumerate(turns[:-1]):
        if _is_execute_control(turn) or _is_cancel_control(turn):
            start_window = index + 1

    seed_index = None
    for index in range(start_window, current_index + 1):
        if _is_request_seed(turns[index]):
            seed_index = index
    if seed_index is None:
        return []

    segment = []
    for turn in turns[seed_index:current_index + 1]:
        if _is_execute_control(turn) or _is_cancel_control(turn) or _is_small_talk(turn):
            continue
        segment.append(turn)
    return segment


def _looks_like_budget(text: str) -> bool:
    value = _norm(text)
    if any(word in value for word in ("ميزاني", "حدود", "لحد", "بحد اقصي", "budget")):
        return False
    return bool(re.fullmatch(r"[0-9٠-٩][0-9٠-٩,.]*\s*(?:الف|ألف|k|ج|جنيه)?", _clean(text), re.IGNORECASE))


def _format_fragment(text: str) -> str:
    value = _clean(text)
    if _looks_like_budget(value):
        return f"ميزانية {value}"
    if value in _AREAS:
        return f"في {value}"
    return value


def _draft_text(context) -> str | None:
    segment = _draft_segment(context)
    if not segment:
        return None
    return "، ".join(_format_fragment(item) for item in segment)


def _is_draft_detail(context) -> bool:
    """Conservative continuation check so general chat still goes to the model."""
    segment = _draft_segment(context)
    if not segment:
        return False
    current = _clean(context.message)
    if _is_request_seed(current):
        return True
    if _is_small_talk(current) or _is_execute_control(current) or _is_cancel_control(current):
        return False
    # Request details are normally short: condition, budget, area, colour, size,
    # etc.  Long/question turns remain model-owned.
    words = current.split()
    if len(words) <= 8 and "?" not in current and "؟" not in current:
        return True
    return any(area in current for area in _AREAS) or _looks_like_budget(current)


def _draft_reply_text(context, draft: str, first: bool) -> str:
    current = _clean(context.message)
    if first:
        return "تمام. كمّل المواصفات والميزانية والمنطقة، ولما تخلص قول «ابدأ»."
    return f"تمام، ضفت «{current}». الطلب لحد دلوقتي: {draft}. لما تخلص قول «ابدأ»."


class DraftAwareProvider:
    """Expose truthful provider metadata while leaving general chat untouched."""

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.name = f"draft-aware({getattr(wrapped, 'name', 'provider')})"
        self.degraded = bool(getattr(wrapped, "degraded", False))

    async def respond(self, context):
        # The policy wrapper below owns the deterministic draft behaviour.  We
        # still invoke the configured provider so unrelated language behaviour
        # remains identical to the base application.
        return await self.wrapped.respond(context)


_provider = DraftAwareProvider(_original_provider)
main_module.conversation_provider = _provider


def _draft_aware_policy(reply, context):
    """Run the existing safety policy, then repair only request-draft turns."""
    safe = _original_policy(reply, context)
    draft = _draft_text(context)
    current = _clean(context.message)

    # Cancelling a pending draft is a hard boundary.  It never creates a case,
    # and a later bare "ابدأ" cannot resurrect the abandoned request.
    if draft and _is_cancel_control(current):
        return ProviderReply(
            "تمام، لغيت الطلب قبل التنفيذ. لو عايز تبدأ طلب جديد قولي من الأول.",
            Intent.CONTINUATION,
            1.0,
            safe.style,
            ActionProposal(ActionType.NONE, False, 1.0),
            provider=_provider.name,
            model=getattr(safe, "model", None),
            degraded=_provider.degraded,
        )

    # A bare start command without a live draft is never enough authority to
    # create a request.  This also prevents an abandoned draft from being
    # resurrected through the base policy's short-imperative context repair.
    if _is_execute_control(current) and not draft:
        return ProviderReply(
            "مفيش طلب جاهز للتنفيذ دلوقتي. قولي محتاج إيه الأول وبعدها قول «ابدأ».",
            Intent.CONTINUATION,
            1.0,
            safe.style,
            ActionProposal(ActionType.NONE, False, 1.0),
            provider=_provider.name,
            model=getattr(safe, "model", None),
            degraded=_provider.degraded,
        )

    # A bare explicit start command executes the accumulated draft, not the
    # single word "ابدأ" and not merely the immediately preceding fragment.
    if draft and _is_execute_control(current):
        return ProviderReply(
            f"تمام، هبدأ على الطلب ده: {draft}.",
            Intent.NEW_REQUEST,
            1.0,
            safe.style,
            ActionProposal(ActionType.CREATE_REQUEST, True, 1.0, payload={"text": draft}),
            provider=_provider.name,
            model=getattr(safe, "model", None),
            degraded=_provider.degraded,
        )

    # A request seed and its short follow-up details are a draft, not separate
    # small-talk turns and not executable actions by themselves.
    if draft and _is_draft_detail(context):
        first = len(_draft_segment(context)) == 1 and _is_request_seed(current)
        return ProviderReply(
            _draft_reply_text(context, draft, first),
            Intent.NEW_REQUEST if first else Intent.CONTINUATION,
            1.0,
            safe.style,
            ActionProposal(ActionType.NONE, False, 1.0),
            provider=_provider.name,
            model=getattr(safe, "model", None),
            degraded=_provider.degraded,
        )

    # Keep thanks after an active request contextual instead of resetting to a
    # greeting.  No business action is ever proposed here.
    if _is_small_talk(current) and context.active_cases:
        return ProviderReply(
            "العفو، أنا متابع الطلب معاك.",
            Intent.SMALL_TALK,
            1.0,
            safe.style,
            ActionProposal(ActionType.NONE, False, 1.0),
            provider=_provider.name,
            model=getattr(safe, "model", None),
            degraded=_provider.degraded,
        )

    return safe


main_module.enforce_case_turn_policy = _draft_aware_policy