"""Universal Context-Aware Routing Engine for Me'ak."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import re
import unicodedata

ROUTER_VERSION = "2026-09-14.6-universal-semantic"

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

def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", " ".join((text or "").split())).casefold()
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return value.translate(str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ى":"ي","ة":"ه"}))

def route_turn(text: str, *, draft_exists: bool=False) -> RouteDecision:
    value = normalize(text)
    
    # 1. Global Strict Triggers (Universal Actions)
    if any(k in value for k in ("الغاء", "الغي", "بلاش", "cancel", "stop")):
        return RouteDecision(Route.REQUEST_CANCEL, 1.0)
    if any(k in value for k in ("نفذ", "اعتمد", "ابدا", "go ahead", "proceed")):
        return RouteDecision(Route.REQUEST_EXECUTE, 1.0)

    # 2. Universal Semantic Fallback
    # If there is an active draft, treat short or ambiguous turns as contextual details,
    # leaving exact dialect understanding to the Gemini Model (Brain) with full chat context.
    if draft_exists:
        return RouteDecision(Route.REQUEST_DETAIL, 0.70, fits_active_draft=True, deterministic=False)

    # If no draft exists, delegate ambiguous inputs to LLM/Semantic classification
    # so dialects across Egypt and the world are processed organically without hardcoded lists.
    return RouteDecision(Route.CASUAL_CHAT, 0.50, deterministic=False)
