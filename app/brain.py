"""Context-Aware Brain Engine for Me'ak."""
from __future__ import annotations
from typing import Any

SYSTEM_PROMPT = """
أنت "معاك"، مساعد شخصي ذكي ورفيق واعي.
1. ممنوع نهائياً استخدام عبارات جافة مثل "قولي أكتر" أو "توضيح أكثر".
2. إذا كان كلام المستخدم قصيراً أو يمهد لحكاية (مثل: "بحب واحدة"، "اقولها هي"):
   تفاعل معه كصديق مقرب واسأله بذكاء عن تفاصيل الفكرة بأسلوب طبيعي ودود.
"""

class BrainEngine:
    def process_turn(self, user_text: str, conversation_history: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "action": "llm_generate",
            "system_instruction": SYSTEM_PROMPT,
            "user_input": user_text,
            "history": conversation_history
        }
