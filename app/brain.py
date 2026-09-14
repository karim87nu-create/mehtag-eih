"""Brain logic for 'Me'ak': Personal assistant & contextual chat."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from .intent_router import Route, RouteDecision

SYSTEM_PROMPT = """
انت "معاك"، مساعد شخصي ذكي وودود جداً.
قواعد صارمة للرد:
1. يمنع منعاً باتاً استخدام عبارات جافة أو آليّة مثل "قولي أكثر"، "فضلاً توضيح"، أو "أنا هنا لمساعدتك".
2. إذا كان كلام المستخدم قصير أو يمهد لحكاية/فضفضة (مثل: "بحب واحدة"، "مش عارف أقولها إزاي"، "عايز أحكيلك"):
   - رد بأسلوب صديق مقرب وداعم.
   - امسك في آخر كلمة قالها واسأله عنها بذكاء (مثال: "تقصد خايف من رد فعلها لما تعترف لها؟" أو "إيه اللي موقفك لحد دلوقتي؟").
3. حافظ على روح الحوار وسلاسته بدون رسميات أو إجبار على تعبئة استمارات.
"""

@dataclass
class DraftState:
    category: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    is_ready_for_execution: bool = False

class BrainEngine:
    def __init__(self):
        pass

    def process_turn(self, user_text: str, decision: RouteDecision, current_draft: DraftState | None) -> dict[str, Any]:
        # Always prioritize friendly casual flow unless an explicit order is active
        if decision.route == Route.CASUAL_CHAT or not current_draft:
            return {
                "action": "casual_chat",
                "system_instruction": SYSTEM_PROMPT,
                "show_ui_card": False
            }

        if decision.route == Route.REQUEST_EXECUTE:
            return {
                "action": "execute_order",
                "convert_to_asset": True,
                "message": "جاري التنفيذ فوراً..."
            }

        return {
            "action": "casual_chat",
            "system_instruction": SYSTEM_PROMPT,
            "show_ui_card": False
        }
