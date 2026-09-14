"""Universal Context-Aware Routing Engine for Me'ak."""
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

@dataclass(frozen=True)
class RouteDecision:
    route: Route
    confidence: float
    deterministic: bool = True
    fits_active_draft: bool = False
    execute_now: bool = False

def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", " ".join((text or "").split())).casefold()
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return value.translate(str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ى":"ي","ة":"ه"}))

def route_turn(text: str, *, draft_exists: bool=False) -> RouteDecision:
    value = normalize(text)
    
    # 1. Direct Execution Triggers
    if any(k in value for k in ("الغاء", "الغي", "بلاش", "cancel")):
        return RouteDecision(Route.REQUEST_CANCEL, 1.0)
    if any(k in value for k in ("نفذ", "اعتمد", "ابدا", "proceed")):
        return RouteDecision(Route.REQUEST_EXECUTE, 1.0)

    # 2. Direct Casual & Storytelling Routing
    # Any storytelling or emotional text is routed to CASUAL_CHAT
    if not draft_exists or any(p in value for p in ("احكيلك", "بحب", "اقولها", "عارف", "حاجه", "حاجة", "مش عارف", "واحده")):
        return RouteDecision(Route.CASUAL_CHAT, 0.95, deterministic=False)

    # 3. Active Draft Support
    if draft_exists:
        return RouteDecision(Route.REQUEST_DETAIL, 0.70, fits_active_draft=True, deterministic=False)

    return RouteDecision(Route.CASUAL_CHAT, 0.85, deterministic=False)
