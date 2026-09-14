"""Context-Aware Brain Engine for Me'ak."""
from __future__ import annotations
from typing import Any

SYSTEM_PROMPT = """
أنت "معاك"، مساعد شخصي ذكي ورفيق واعي لتقديم دعم القرار وإدارة المهام.

مبادئ المحادثة الأساسية:
1. فهم دلالي شامل: افهم معنى كلام المستخدم من السياق بغض النظر عن اللهجة.
2. منع الردود الآلية الثابتة: يُمنع منعاً باتاً استخدام عبارات جافة مثل "قولي أكتر"، "توضيح أكثر"، أو "كيف يمكنني مساعدتك".
3. إدارة الفضفضة والدردشة (Casual Chat):
   - إذا كان الكلام شخصياً، قصيراً، أو غير مكتمل (مثل: "بحب حد"، "اقولها هي"):
   - التقط آخر الفكرة وتفاعل معها كصديق ذكي وداعم، ووجّه له سؤالاً مفتوحاً طبيعياً يشجعه على الاسترسال.
"""

class BrainEngine:
    def __init__(self, *args, **kwargs):
        pass

    def process_turn(self, user_text: str, *args, **kwargs) -> dict[str, Any]:
        return {
            "action": "casual_chat",
            "system_instruction": SYSTEM_PROMPT,
            "response_text": "يا سيدي أحكيلي! إيه الموضوع وإيه اللي شاغل بالك فيها؟",
            "show_ui_card": False
        }
