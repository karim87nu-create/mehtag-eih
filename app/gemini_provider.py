from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import httpx

from .conversation import (
    ActionProposal,
    ActionType,
    ConversationProvider,
    Intent,
    ProviderReply,
    ResponseStyle,
    SYSTEM_PROMPT,
)

logger = logging.getLogger("maak.gemini")


class GeminiProvider(ConversationProvider):
    """Gemini native GenerateContent provider with inline image/file support."""

    name = "gemini"
    degraded = False

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

    @staticmethod
    def _inline_part(attachment: dict[str, str]) -> dict[str, Any] | None:
        data_url = str(attachment.get("data_url") or "")
        if not data_url.startswith("data:") or "," not in data_url:
            return None
        header, encoded = data_url.split(",", 1)
        mime_type = str(attachment.get("mime_type") or "application/octet-stream")
        if ";base64" not in header:
            try:
                encoded = base64.b64encode(encoded.encode()).decode()
            except Exception:
                return None
        return {"inline_data": {"mime_type": mime_type, "data": encoded}}

    async def respond(self, context) -> ProviderReply:
        context_payload = {
            "message": context.message,
            "history": context.history[-12:],
            "locale": context.locale,
            "active_cases": context.active_cases,
        }
        parts: list[dict[str, Any]] = [
            {"text": json.dumps(context_payload, ensure_ascii=False)}
        ]
        for attachment in context.attachments[:3]:
            part = self._inline_part(attachment)
            if part:
                parts.append(part)

        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.35,
                "maxOutputTokens": 900,
                "responseMimeType": "application/json",
            },
        }
        url = f"{self.base_url}/models/{self.model}:generateContent"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                url,
                params={"key": self.api_key},
                headers={"Content-Type": "application/json"},
                json=payload,
            )
        if response.is_error:
            detail = response.text[:700]
            raise RuntimeError(f"Gemini HTTP {response.status_code}: {detail}")
        body = response.json()
        candidates = body.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini returned no candidates")
        output_parts = ((candidates[0].get("content") or {}).get("parts") or [])
        raw = "".join(str(part.get("text") or "") for part in output_parts).strip()
        if not raw:
            raise RuntimeError("Gemini returned an empty response")
        data = json.loads(raw)
        style_data = data.get("style") or {}
        style = ResponseStyle(
            language=str(style_data.get("language") or "ar"),
            dialect=str(style_data.get("dialect") or "egyptian"),
            tone=str(style_data.get("tone") or "warm"),
            mood=str(style_data.get("mood") or "neutral"),
            urgency=str(style_data.get("urgency") or "normal"),
            formality=str(style_data.get("formality") or "casual"),
        )
        proposed = data.get("action") or {}
        try:
            action_type = ActionType(str(proposed.get("type") or "NONE"))
        except ValueError:
            action_type = ActionType.NONE
        action = ActionProposal(
            action_type,
            bool(proposed.get("authorized", False)),
            float(proposed.get("confidence") or 0),
            proposed.get("case_id"),
            proposed.get("case_type"),
            proposed.get("payload") or {},
        )
        try:
            intent = Intent(str(data.get("intent") or "GENERAL_QUESTION"))
        except ValueError:
            intent = Intent.GENERAL_QUESTION
        return ProviderReply(
            str(data.get("response") or "").strip(),
            intent,
            float(data.get("confidence") or 0.5),
            style,
            action,
            self.name,
            self.model,
            False,
        )


def build_gemini_provider() -> ConversationProvider | None:
    provider = os.getenv("LLM_PROVIDER", "").strip().casefold()
    if provider not in {"gemini", "google", "google-gemini"}:
        return None
    key = (os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        logger.warning("Gemini selected but no API key is configured")
        return None
    model = (os.getenv("LLM_MODEL") or "gemini-2.5-flash").strip()
    return GeminiProvider(key, model)
