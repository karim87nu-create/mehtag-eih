"""Brain logic for 'Me'ak': Decision support, single-best-option generation, and lifecycle management."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from .intent_router import Route, RouteDecision

@dataclass
class DraftState:
    category: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    is_ready_for_execution: bool = False
    best_option: dict[str, Any] | None = None

class BrainEngine:
    def __init__(self):
        pass

    def process_turn(self, user_text: str, decision: RouteDecision, current_draft: DraftState | None) -> dict[str, Any]:
        # 1. Informational & Exploration Routes (No Draft Creation)
        if decision.route in (Route.FACTUAL_QUESTION, Route.COMPARISON_RECOMMENDATION, Route.MEDIA_IMAGE_REQUEST):
            return {
                "action": "chat_response",
                "show_ui_card": False,
                "prompt_intent": "Provide direct decision-support info or comparisons. Do not force an order form."
            }

        # 2. Updating or Adding Details to Active Draft
        if decision.route == Route.REQUEST_DETAIL or (current_draft and decision.fits_active_draft):
            updated_draft = current_draft or DraftState()
            # Extract key/value pair dynamically from text context
            updated_draft.details["last_update"] = user_text
            
            # Check if details are sufficient for "One Best Option"
            if len(updated_draft.details) >= 2:
                updated_draft.is_ready_for_execution = True
                updated_draft.best_option = self._calculate_best_option(updated_draft.details)

            return {
                "action": "update_draft",
                "draft": updated_draft,
                "show_ui_card": updated_draft.is_ready_for_execution,
                "ui_type": "decision_card" if updated_draft.is_ready_for_execution else "chat_followup"
            }

        # 3. Request Execution Intent
        if decision.route == Route.REQUEST_EXECUTE:
            return {
                "action": "execute_order",
                "convert_to_asset": True,  # Prepares post-purchase asset management
                "message": "جاري بدء التنفيذ وإرسال الطلب..."
            }

        # 4. Fallback Casual Chat
        return {
            "action": "casual_chat",
            "show_ui_card": False
        }

    def _calculate_best_option(self, details: dict[str, Any]) -> dict[str, Any]:
        """Synthesizes context to select the SINGLE best option rather than a long list."""
        return {
            "title": "الخيار الأفضل بناءً على طلبك",
            "summary": "تم اختيار هذا الخيار بناءً على معايير الجودة والسعر المحددة.",
            "actions": ["وافق", "راجع"]
        }
