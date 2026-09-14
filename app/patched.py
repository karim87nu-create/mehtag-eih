"""Production entrypoint with a narrow draft-preservation layer.

The base conversation model remains responsible for general chat. This module
hardens request-draft boundaries and keeps deterministic/simple turns off the
slow local model path when we already know the truthful answer.
"""
from __future__ import annotations

import re

from . import main as main_module
from .conversation import ActionProposal, ActionType, Intent, ProviderReply, ResponseStyle
from .intent_router import Route, RouteDecision, route_turn, semantic_decision


app = main_module.app
_original_provider = main_module.conversation_provider
_original_policy = main_module.enforce_case_turn_policy


_AR_REQUEST_START = re.compile(
    r"(?:^|\s)(?:عايز|عاوز|محتاج|محتاجه|عايزه|عاوزه|بدور\s+على|بدور\s+علي)(?:\s|$)",
    re.IGNORECASE,
)
_EN_REQUEST_START = re.compile(r"\b(?:i\s+want|i\s+need|looking\s+for)\b", re.IGNORECASE)
_MEDIA_REQUEST_RE = re.compile(
    r"(?:وريني|ورني|اعرض(?:لي)?|فرجني|show\s+me).{0,28}(?:صور|صوره|صورة|photos?|pictures?|images?)",
    re.IGNORECASE,
)

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
_ATTENTION_CUES = {
    "بص", "بصي", "اسمع", "اسمعني", "شوف", "شوفي", "look", "listen",
}
_HOW_ARE_YOU_RE = re.compile(r"^(?:انت\s+)?عامل\s+(?:ع|ا|اي|ايا|ايه)\s*[؟?]?$", re.IGNORECASE)
_CONVERSATION_PREFIXES = (
    "ايه ", "إيه ", "ازاي ", "إزاي ", "ليه ", "مين ", "فين ", "امتى ", "إمتى ",
    "هل ", "وريني ", "ورني ", "اعرض ", "اعرضلي ", "فرجني ", "اشرح ", "اشرحلي ",
    "قولي ", "قوللي ", "احكيلي ", "فهمني ", "عرفني ",
    "what ", "why ", "how ", "who ", "where ", "when ", "which ", "show me ",
    "explain ", "tell me ", "can you ", "could you ",
)
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
    return route_turn(text).route == Route.REQUEST_SEED


def _is_execute_control(text: str) -> bool:
    return route_turn(text).route == Route.REQUEST_EXECUTE


def _is_cancel_control(text: str) -> bool:
    return route_turn(text).route == Route.REQUEST_CANCEL


def _is_small_talk(text: str) -> bool:
    return route_turn(text).route == Route.CASUAL_CHAT and (
        _norm(text) in {_norm(item) for item in _SMALL_TALK}
        or bool(_HOW_ARE_YOU_RE.fullmatch(_norm(text)))
    )


def _is_attention_cue(text: str) -> bool:
    return _norm(text) in {_norm(item) for item in _ATTENTION_CUES}


def _is_media_request(text: str) -> bool:
    return route_turn(text).route == Route.MEDIA_IMAGE_REQUEST


def _is_conversation_only_turn(text: str) -> bool:
    """Turns that must be answered, never silently turned into request details."""
    current = _clean(text)
    normalized = _norm(current)
    route = route_turn(current).route
    if route in {Route.FACTUAL_QUESTION, Route.COMPARISON_RECOMMENDATION, Route.MEDIA_IMAGE_REQUEST,
                 Route.REQUEST_STATUS_FOLLOWUP, Route.EXTERNAL_ACTION, Route.FUTURE_TASK}:
        return True
    if _is_attention_cue(current) or _is_media_request(current):
        return True
    return any(normalized.startswith(_norm(prefix)) for prefix in _CONVERSATION_PREFIXES)


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

    # A previous explicit execute/cancel command closes the older draft. This
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
    for index, turn in enumerate(turns[seed_index:current_index + 1], start=seed_index):
        if _is_execute_control(turn) or _is_cancel_control(turn) or _is_small_talk(turn):
            continue
        # Questions and conversational commands can happen while a draft is
        # open, but they are not constraints. Keep the draft alive without
        # contaminating the executable request text.
        if index > seed_index and _is_conversation_only_turn(turn):
            continue
        segment.append(turn)
    return _resolve_explicit_corrections(segment)


def _detail_kind(text: str) -> str | None:
    """Classify only constraints safe enough to replace deterministically."""
    value = _norm(text)
    correction_number = _is_explicit_correction(text) and any(ch.isdigit() for ch in value) and not re.search(r"[a-z]", value)
    if any(ch.isdigit() for ch in value) and (
        _looks_like_budget(text) or any(word in value for word in ("ميزاني", "جنيه", "الف", "ألف", "budget"))
        or correction_number
    ):
        return "budget"
    if any(area in value for area in (_norm(item) for item in _AREAS)):
        return "area"
    if set(value.split()) & {"جديد", "مستعمل", "new", "used"}:
        return "condition"
    if any(word in value for word in ("موديل", "model")) or re.fullmatch(r"[a-z]+[a-z0-9-]*\d+[a-z0-9-]*", value):
        return "model"
    return None


def _is_explicit_correction(text: str) -> bool:
    value = _norm(text)
    return bool(
        re.search(r"(?:^|\s)(?:بدل|مش|لا)(?:\s|،|$)", value)
        or any(marker in value for marker in ("غيرها ل", "غيره ل", "خليها ", "خليه ", "قصدي ", "مش قصدي "))
    )


def _resolve_explicit_corrections(segment: list[str]) -> list[str]:
    """Replace the last same-kind constraint instead of keeping contradictions.

    The user's wording remains visible in chat.  Only the executable draft is
    normalized, and only for explicit corrections with a recognizable kind.
    """
    resolved: list[str] = []
    for fragment in segment:
        kind = _detail_kind(fragment)
        if _is_explicit_correction(fragment) and kind:
            resolved = [item for item in resolved if _detail_kind(item) != kind]
        resolved.append(fragment)
    return resolved


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
    if (
        _is_small_talk(current)
        or _is_execute_control(current)
        or _is_cancel_control(current)
        or _is_conversation_only_turn(current)
    ):
        return False
    return route_turn(current, draft_exists=True).route == Route.REQUEST_DETAIL


def _draft_reply_text(context, draft: str, first: bool) -> str:
    current = _clean(context.message)
    if first:
        return "تمام. كمّل المواصفات والميزانية والمنطقة، ولما تخلص قول «ابدأ»."
    return f"تمام، ضفت «{current}». الطلب لحد دلوقتي: {draft}. لما تخلص قول «ابدأ»."


def _placeholder(intent: Intent = Intent.CONTINUATION) -> ProviderReply:
    """Cheap reply used only when the policy below deterministically replaces it."""
    return ProviderReply(
        "",
        intent,
        1.0,
        ResponseStyle(),
        ActionProposal(ActionType.NONE, False, 1.0),
        provider="draft-fast-path",
        model=None,
        degraded=False,
    )


class DraftAwareProvider:
    """Keep deterministic turns fast while leaving open-ended chat to the model."""

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.name = f"draft-aware({getattr(wrapped, 'name', 'provider')})"
        self.degraded = bool(getattr(wrapped, "degraded", False))

    async def respond(self, context):
        current = _clean(context.message)
        draft = _draft_text(context)
        decision = route_turn(current, draft_exists=bool(draft))

        # Product/model codes and numeric constraints are structured data, not
        # prose intent. A tiny local model must not reinterpret e.g. "rkv250"
        # as chat and erase an otherwise valid draft detail.
        structured_detail = bool(
            draft
            and decision.route == Route.REQUEST_DETAIL
            and (
                any(ch.isdigit() for ch in current)
                or (len(current.split()) == 1 and not current.endswith(("?", "؟")))
            )
        )

        # Deterministic recognition is reserved for clear controls and cheap,
        # high-certainty paths. Ambiguous language is classified semantically
        # using the live draft and recent conversation rather than a phrase list.
        needs_semantic = not decision.deterministic or (
            bool(draft) and decision.route in {Route.REQUEST_DETAIL, Route.CASUAL_CHAT}
        )
        if structured_detail:
            needs_semantic = False
        semantic_payload = None
        if needs_semantic:
            classifier = getattr(self.wrapped, "classify_route", None)
            if classifier is not None:
                semantic_payload = await classifier(context, draft)
                semantic = semantic_decision(semantic_payload)
                if semantic is not None and semantic.confidence >= 0.68:
                    decision = semantic
        setattr(context, "semantic_route_decision", decision)
        route = decision.route

        # These paths are fully determined by the server policy below. Running a
        # 1.7B local model first only adds seconds of latency and cannot improve
        # the result.
        semantic_detail = (
            draft and route == Route.REQUEST_DETAIL and decision.fits_active_draft
        )
        deterministic_detail = _is_draft_detail(context) and not (
            needs_semantic and not decision.deterministic
        )
        if draft and (_is_execute_control(current) or _is_cancel_control(current) or semantic_detail or deterministic_detail):
            return _placeholder(Intent.NEW_REQUEST if _is_request_seed(current) else Intent.CONTINUATION)
        if _is_execute_control(current) and not draft:
            return _placeholder(Intent.CONTINUATION)
        if _HOW_ARE_YOU_RE.fullmatch(_norm(current)):
            return ProviderReply(
                "تمام الحمد لله، معاك. إنت عامل إيه؟",
                Intent.SMALL_TALK,
                1.0,
                ResponseStyle(),
                ActionProposal(ActionType.NONE, False, 1.0),
                provider="router-fast-path",
                model=None,
                degraded=False,
            )
        if _is_small_talk(current) and context.active_cases:
            return _placeholder(Intent.SMALL_TALK)

        # Very short attention turns should feel instant and natural.
        if _is_attention_cue(current):
            return ProviderReply(
                "معاك، قول.",
                Intent.SMALL_TALK,
                1.0,
                ResponseStyle(),
                ActionProposal(ActionType.NONE, False, 1.0),
                provider="draft-fast-path",
                model=None,
                degraded=False,
            )

        if route == Route.REQUEST_STATUS_FOLLOWUP and not context.active_cases:
            return ProviderReply(
                "مفيش طلب مرتبط بالمحادثة دي لسه.", Intent.CONTINUATION, 1.0,
                ResponseStyle(), ActionProposal(ActionType.NONE, False, 1.0),
                provider="router-fast-path", model=None, degraded=False,
            )

        if route == Route.FUTURE_TASK:
            return ProviderReply(
                "أقدر أفهم المهمة، لكن جدولة التذكير لوقت لاحق لسه مش متوصلة هنا.",
                Intent.GENERAL_QUESTION, 1.0, ResponseStyle(),
                ActionProposal(ActionType.NONE, False, 1.0), provider="router-fast-path", model=None, degraded=False,
            )

        if route == Route.EXTERNAL_ACTION:
            return ProviderReply(
                "فهمت الإجراء الخارجي المطلوب، لكن محتاج قناة موثقة ومصرح بها قبل أي إرسال فعلي.",
                Intent.GENERAL_QUESTION, 1.0, ResponseStyle(),
                ActionProposal(ActionType.NONE, False, 1.0), provider="router-fast-path", model=None, degraded=False,
            )

        # For open-ended advice/comparison turns, reuse the natural answer from
        # the same semantic inference. This avoids a second model call and makes
        # the route affect the answer, not just the request safety boundary.
        semantic_response = _clean(str((semantic_payload or {}).get("response") or ""))
        if (
            semantic_response
            and decision.confidence >= 0.68
            and route in {Route.CASUAL_CHAT, Route.COMPARISON_RECOMMENDATION}
        ):
            return ProviderReply(
                semantic_response, Intent.GENERAL_QUESTION, decision.confidence,
                ResponseStyle(), ActionProposal(ActionType.NONE, False, 1.0),
                provider="semantic-router", model=getattr(getattr(self.wrapped, "primary", None), "model", None),
                degraded=False,
            )

        # The current web client cannot render internet image results yet. Say
        # that plainly instead of swallowing the command as a draft detail or
        # replying with a generic "continue" message.
        if _is_media_request(current):
            return ProviderReply(
                "طلبك واضح. عرض صور من الإنترنت جوه المحادثة لسه مش متوصل، فمش هعتبر كلامك تكملة لطلب قديم. أقدر أساعدك بالمواصفات أو نكمّل كطلب بحث/شراء لو ده هدفك.",
                Intent.GENERAL_QUESTION,
                1.0,
                ResponseStyle(),
                ActionProposal(ActionType.NONE, False, 1.0),
                provider="draft-fast-path",
                model=None,
                degraded=False,
            )

        # Greetings/thanks are already handled well by the deterministic
        # fallback; do not spend several seconds generating them locally.
        if _is_small_talk(current):
            fallback = getattr(self.wrapped, "fallback", None)
            if fallback is not None:
                return await fallback.respond(context)

        return await self.wrapped.respond(context)


_provider = DraftAwareProvider(_original_provider)
main_module.conversation_provider = _provider


def _draft_aware_policy(reply, context):
    """Run the existing safety policy, then repair only request-draft turns."""
    safe = _original_policy(reply, context)
    draft = _draft_text(context)
    current = _clean(context.message)
    decision = getattr(context, "semantic_route_decision", None)

    # Cancelling a pending draft is a hard boundary. It never creates a case,
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
    # create a request. This also prevents an abandoned draft from being
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
    semantic_detail = bool(
        decision
        and decision.route == Route.REQUEST_DETAIL
        and decision.fits_active_draft
        and decision.confidence >= 0.68
    )
    semantic_conversation = bool(
        decision
        and decision.route in {
            Route.CASUAL_CHAT, Route.FACTUAL_QUESTION, Route.COMPARISON_RECOMMENDATION,
            Route.MEDIA_IMAGE_REQUEST, Route.REQUEST_STATUS_FOLLOWUP, Route.EXTERNAL_ACTION,
            Route.FUTURE_TASK,
        }
    )
    if draft and (semantic_detail or (_is_draft_detail(context) and not semantic_conversation)):
        first = len(_draft_segment(context)) == 1 and _is_request_seed(current)
        # If an older request is already linked to the thread, using
        # CONTINUATION here would make the base endpoint attach that old case's
        # card to the new draft. Mark the new draft as NEW_REQUEST while still
        # proposing no action, so the UI cannot imply we are editing the old case.
        draft_intent = Intent.NEW_REQUEST if first or context.active_cases else Intent.CONTINUATION
        return ProviderReply(
            _draft_reply_text(context, draft, first),
            draft_intent,
            1.0,
            safe.style,
            ActionProposal(ActionType.NONE, False, 1.0),
            provider=_provider.name,
            model=getattr(safe, "model", None),
            degraded=_provider.degraded,
        )

    # Keep thanks after an active request contextual instead of resetting to a
    # greeting. No business action is ever proposed here.
    if _HOW_ARE_YOU_RE.fullmatch(_norm(current)):
        return ProviderReply(
            "تمام الحمد لله، معاك. إنت عامل إيه؟",
            Intent.SMALL_TALK,
            1.0,
            safe.style,
            ActionProposal(ActionType.NONE, False, 1.0),
            provider=_provider.name,
            model=getattr(safe, "model", None),
            degraded=_provider.degraded,
        )
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
