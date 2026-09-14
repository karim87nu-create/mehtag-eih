"""Conservative deterministic routing; ambiguous language goes to semantic classification."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import re
import unicodedata

ROUTER_VERSION = "2026-09-14.4-context-aware-fix"

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
    fits_active_draft: bool = False
    execute_now: bool = False

def semantic_decision(data: dict | None) -> RouteDecision | None:
    if not isinstance(data, dict):
        return None
    try:
        route = Route(str(data.get("route") or ""))
        confidence = max(0.0, min(float(data.get("confidence", 0.0)), 1.0))
    except (TypeError, ValueError):
        return None
    return RouteDecision(route, confidence, False, bool(data.get("fits_active_draft", False)), bool(data.get("execute_now", False)))

def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", " ".join((text or "").split())).casefold()
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return value.translate(str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ى":"ي","ة":"ه"}))

def words(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", normalize(text), flags=re.UNICODE))

_EXECUTE={"ابدا","ابدا البحث","ابدا دلوقتي","نفذ","دور","دورلي","كمل وابدا","ابدا التنفيذ","start","go","go ahead","proceed","search now"}
_CANCEL={"بلاش","خلاص بلاش","الغيه","الغي","متبداش","ما تبداش","متنفذش","ما تنفذش","مش عايز","خلاص مش عايز","cancel","never mind","nevermind","stop","don't start","do not start"}
_CASUAL={"بص","بصي","اسمع","اسمعني","شكرا","متشكر","تسلم","اهلا","هاي","hello","hi","باي","bye","thanks","thank you"}
_CONTINUATIONS={"برضو","برضه","كمان","ايوة","تمام","ماشي","أه","اه","ماشى","بالظبط"}

def route_turn(text: str, *, draft_exists: bool=False) -> RouteDecision:
    value=normalize(text); token_set=words(text)
    if value in _EXECUTE: return RouteDecision(Route.REQUEST_EXECUTE,1.0)
    if value in _CANCEL: return RouteDecision(Route.REQUEST_CANCEL,1.0)
    if value in _CASUAL: return RouteDecision(Route.CASUAL_CHAT,1.0)
    if re.fullmatch(r"(?:انت\s+)?عامل\s+(?:ع|ا|اي|ايا|ايه)\s*[؟?]?",value): return RouteDecision(Route.CASUAL_CHAT,.99)
    
    # 1. Handling Continuations or Detail inputs when a Draft exists or in ongoing context
    if draft_exists:
        if value in _CONTINUATIONS or re.search(r"(?:^|\s)(?:بدل|مش|لا،?|لا\s+)(?:\s|$)",value) or any(p in value for p in ("غيرها ل","غيره ل","خليها ","خليه ","قصدي ","مش قصدي ")):
            return RouteDecision(Route.REQUEST_DETAIL, .98, fits_active_draft=True)
            
    if re.search(r"(?:صور|صوره|photos?|pictures?|images?)",value) and (token_set & {"عايز","عاوز","محتاج","وريني","ورني","اعرضلي","فرجني","show"} or value.startswith(("صور","صوره"))): return RouteDecision(Route.MEDIA_IMAGE_REQUEST,.99)
    if any(p in value for p in ("فين الطلب","حاله الطلب","الطلب وصل","وصل لفين","وصلنا لفين","اخر تحديث","order status","request status","where is my order")): return RouteDecision(Route.REQUEST_STATUS_FOLLOWUP,.98)
    if any(p in value for p in ("فكرني","بكره","بعد ساعه","remind me","tomorrow","later")): return RouteDecision(Route.FUTURE_TASK,.95)
    if any(p in value for p in ("ابعته ل","ابعت ل","كلم ","send it to","message ")): return RouteDecision(Route.EXTERNAL_ACTION,.94)
    if any(p in value for p in ("رشحلي","قارن","ايه الافضل","مين الافضل","هو ده كويس","انا متردد","رايك","recommend","compare","is this good","i am unsure")): return RouteDecision(Route.COMPARISON_RECOMMENDATION,.97)
    if any(p in value for p in ("عايز اعرف","عاوز اعرف","محتاج اعرف","مش اشتري","للمعرفه","ايه الفرق","اشرحلي","يعني ايه","i want to know","not buy")): return RouteDecision(Route.FACTUAL_QUESTION,.99)
    if "?" in text or "؟" in text or token_set & {"ليه","ازاي","ايه","مين","امتي","what","why","how","who","when","which"}: return RouteDecision(Route.FACTUAL_QUESTION,.93)
    
    if re.search(r"(?:^|\s)(?:عايز|عاوز|محتاج|محتاجه|عايزه|عاوزه|بدور\s+عل[ىي]|i\s+want|i\s+need|looking\s+for)(?:\s|$)",value): return RouteDecision(Route.REQUEST_SEED,.82,deterministic=False)
    
    # 2. Short model/detail inputs handling when no explicit seed was matched directly
    if draft_exists or len(value.split()) <= 3:
        return RouteDecision(Route.REQUEST_DETAIL, .75, fits_active_draft=draft_exists, deterministic=False)

    return RouteDecision(Route.CASUAL_CHAT,.55,deterministic=False)
    
