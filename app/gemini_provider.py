from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import httpx

from .conversation import ActionProposal, ActionType, ConversationProvider, Intent, ProviderReply, ResponseStyle, SYSTEM_PROMPT

logger = logging.getLogger("maak.gemini")

class GeminiProvider(ConversationProvider):
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
            encoded = base64.b64encode(encoded.encode()).decode()
        return {"inline_data": {"mime_type": mime_type, "data": encoded}}

    async def _generate_json(self, system: str, parts: list[dict[str, Any]], max_tokens: int = 900) -> dict[str, Any]:
        payload = {"system_instruction":{"parts":[{"text":system}]},"contents":[{"role":"user","parts":parts}],"generationConfig":{"temperature":0.25,"maxOutputTokens":max_tokens,"responseMimeType":"application/json"}}
        url = f"{self.base_url}/models/{self.model}:generateContent"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, params={"key":self.api_key}, headers={"Content-Type":"application/json"}, json=payload)
        if response.is_error:
            raise RuntimeError(f"Gemini HTTP {response.status_code}: {response.text[:700]}")
        candidates = response.json().get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini returned no candidates")
        raw = "".join(str(p.get("text") or "") for p in ((candidates[0].get("content") or {}).get("parts") or [])).strip()
        if not raw:
            raise RuntimeError("Gemini returned an empty response")
        return json.loads(raw)

    async def classify_route(self, context, draft=None) -> dict[str, Any]:
        system = """You are the semantic turn controller for MAAK, a natural Egyptian Arabic everyday companion and assistant.
Understand the CURRENT turn from meaning and context, never from word count or a fixed domain checklist.
Return JSON only with: route, confidence, fits_active_draft, response.
route must be one of: casual_chat, factual_question, comparison_recommendation, request_seed, request_detail, request_execute, request_cancel, request_status/followup, media/image_request, external_action, future_task.
request_seed: a genuine request for MAAK to pursue/find/arrange/get/solve something. A feeling, preference, joke, objection or ordinary desire is not automatically a request.
request_detail: only when this turn truly answers, constrains or corrects the active request. Short text, numbers, places and product words are NOT details merely because a draft exists. fits_active_draft must be true only when meaning and context establish the connection.
request_execute: only explicit permission to start. request_cancel: explicit cancellation.
response is the actual user-facing reply for this turn. If a real request is being formed, ask at most ONE materially useful next question, selected dynamically for that exact request. Do not use a fixed condition/budget/location flow. If enough is known, naturally say it is ready and invite the user to start. If the turn is side conversation, answer it normally without pretending it changed the request. Use warm natural Egyptian Arabic, concise and non-form-like. Never invent facts or execution."""
        payload={"message":context.message,"recent_history":context.history[-10:],"active_request_draft":draft,"active_cases":context.active_cases,"locale":context.locale}
        data = await self._generate_json(system,[{"text":json.dumps(payload,ensure_ascii=False)}],500)
        # Preserve the semantic reply on the same TurnContext so the safety
        # policy can use this exact inference without a second model call.
        setattr(context, "semantic_response", str(data.get("response") or "").strip())
        setattr(context, "semantic_payload", data)
        return data

    async def respond(self, context) -> ProviderReply:
        context_payload={"message":context.message,"history":context.history[-12:],"locale":context.locale,"active_cases":context.active_cases}
        parts=[{"text":json.dumps(context_payload,ensure_ascii=False)}]
        for attachment in context.attachments[:3]:
            part=self._inline_part(attachment)
            if part: parts.append(part)
        data=await self._generate_json(SYSTEM_PROMPT,parts)
        style_data=data.get("style") or {}
        style=ResponseStyle(language=str(style_data.get("language") or "ar"),dialect=str(style_data.get("dialect") or "egyptian"),tone=str(style_data.get("tone") or "warm"),mood=str(style_data.get("mood") or "neutral"),urgency=str(style_data.get("urgency") or "normal"),formality=str(style_data.get("formality") or "casual"))
        proposed=data.get("action") or {}
        try: action_type=ActionType(str(proposed.get("type") or "NONE"))
        except ValueError: action_type=ActionType.NONE
        action=ActionProposal(action_type,bool(proposed.get("authorized",False)),float(proposed.get("confidence") or 0),proposed.get("case_id"),proposed.get("case_type"),proposed.get("payload") or {})
        try: intent=Intent(str(data.get("intent") or "GENERAL_QUESTION"))
        except ValueError: intent=Intent.GENERAL_QUESTION
        return ProviderReply(str(data.get("response") or "").strip(),intent,float(data.get("confidence") or .5),style,action,self.name,self.model,False)

def build_gemini_provider() -> ConversationProvider | None:
    provider=os.getenv("LLM_PROVIDER","").strip().casefold()
    if provider not in {"gemini","google","google-gemini"}: return None
    key=(os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        logger.warning("Gemini selected but no API key is configured")
        return None
    return GeminiProvider(key,(os.getenv("LLM_MODEL") or "gemini-2.5-flash").strip())
