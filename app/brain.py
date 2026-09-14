"""Context-Aware Brain Engine for Me'ak."""
from __future__ import annotations
from typing import Any
from .intent_router import Route, RouteDecision

SYSTEM_PROMPT = """
أنت "معاك"، مساعد شخصي ذكي ورفيق واعي لتقديم دعم القرار وإدارة المهام.

مبادئ المحادثة الأساسية:
1. فهم دلالي شامل: افهم معنى كلام المستخدم من السياق بغض النظر عن اللهجة (مصرية، خليجية، أو غيرها).
2. منع الردود الآلية الثابتة: يُمنع منعاً باتاً استخدام عبارات جافة مثل "قولي أكتر"، "توضيح أكثر"، أو "كيف يمكنني مساعدتك".
3. إدارة الفضفضة والدردشة (Casual Chat):
   - إذا كان الكلام شخصياً، قصيراً، أو غير مكتمل (مثل: "بحب واحدة"، "مش عارف أقولها إزاي"):
   - التقط آخر الفكرة وتفاعل معها كصديق ذكي وداعم، ووجّه له سؤالاً مفتوحاً طبيعياً يشجعه على الاسترسال.
4. إدارة الطلبات (Request Execution):
   - لا تفتح مسودة طلب (Draft) إلا إذا طلب المستخدم إجراءً عملياً صريحاً (شراء، حجز، صيانة).
   - عند توفر معلومات كافية لطلب معين، قدم خياراً واحداً ممتازاً (One Best Option) للتأكيد.
"""

class BrainEngine:
    def process_turn(self, user_text: str, decision: RouteDecision, conversation_history: list[dict[str, str]]) -> dict[str, Any]:
        if decision.route == Route.STRICT_CANCEL:
            return {
                "action": "cancel",
                "response_text": "تم إلغاء الأمر الحالي. أقدر أساعدك في إيه تاني؟"
            }

        # Send context + prompt directly to the Gemini LLM Engine
        return {
            "action": "llm_generate",
            "system_instruction": SYSTEM_PROMPT,
            "user_input": user_text,
            "history": conversation_history
        }
