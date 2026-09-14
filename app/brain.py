"""Brain engine for Me'ak - Preserving system states with natural semantic handling."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from .intent_router import Route, RouteDecision

SYSTEM_PROMPT = """
أنت "معاك"، مساعد شخصي ذكي ورفيق واعي لتقديم دعم القرار وإدارة المهام.

مبادئ الاستجابة الموحدة:
1. استجابة دلالية مرنة: افهم قصد المستخدم بأي لهجة أو أسلوب تعبير دون التقيد بقوائم كلمات ثابتة.
2. حظر الردود الآلية المكررة: يمنع تماماً استخدام العبارة الجافة "قولي أكتر" أو أي رد جاف مماثل.
3. معالجة المدخلات القصيرة والفضفضة:
   - إذا كان الكلام شخصياً، غير مكتمل، أو تمهيداً لحوار: التقط آخر فكرة ذكرها وتفاعل معها بأسلوب صديق مقرب واسأله سؤالاً مفتوحاً يدفعه للاسترسال.
4. إدارة الحالات والمهام (State Management):
   - حافظ على سياق المسودة (Draft) والعمليات النشطة، ولا تطالب بمعلومات إضافية إلا عند الحاجة الفعلية لإتمام إجراء.
"""

@dataclass
class DraftState:
    category: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    is_ready_for_execution: bool = False

class BrainEngine:
    def __init__(self, *args, **kwargs):
        pass

    def process_turn(
        self, 
        user_text: str, 
        decision: RouteDecision, 
        current_draft: DraftState | None = None,
        conversation_history: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        
        # 1. Handle Execution Trigger
        if decision.route == Route.REQUEST_EXECUTE and current_draft and current_draft.is_ready_for_execution:
            return {
                "action": "execute_order",
                "draft": current_draft,
                "message": "جاري تنفيذ الطلب فوراً..."
            }

        # 2. Handle Cancellation
        if decision.route == Route.REQUEST_CANCEL:
            return {
                "action": "cancel",
                "message": "تم إلغاء العملية الحالية. كيف يمكنني مساعدتك الآن؟"
            }

        # 3. Default Semantic Dynamic Generation via LLM
        return {
            "action": "llm_generate",
            "system_instruction": SYSTEM_PROMPT,
            "user_input": user_text,
            "current_draft": current_draft,
            "history": conversation_history or [],
            "show_ui_card": False
        }
