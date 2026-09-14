"""Single-Pass Routing Interface for Me'ak."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

class Route(str, Enum):
    DYNAMIC_LLM = "dynamic_llm"
    STRICT_CANCEL = "strict_cancel"

@dataclass(frozen=True)
class RouteDecision:
    route: Route
    confidence: float = 1.0

def route_turn(text: str, *, draft_exists: bool = False) -> RouteDecision:
    value = (text or "").strip().lower()
    
    # Only hard-kill on explicit cancellation
    if value in ("إلغاء", "الغي", "بلاش", "cancel"):
        return RouteDecision(Route.STRICT_CANCEL)

    # Pass everything else directly to the LLM Brain
    return RouteDecision(Route.DYNAMIC_LLM)
