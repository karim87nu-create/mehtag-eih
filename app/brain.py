from __future__ import annotations

import os
from typing import Any

import httpx

from .conversation import (
    ActionProposal,
    ActionType,
    ConversationProvider,
    Intent,
    ProviderReply,
    ResilientProvider,
    ResponseStyle,
)
from .gemini_provider import build_gemini_provider


class MaakBrain(ConversationProvider):
    """Stable boundary between the MAAK product and whichever model runs the brain.

    The rest of the application talks only to this interface. The implementation
    can be Gemini today, a self-hosted MAAK model tomorrow, or another backend
    later without changing conversation, memory, request, or execution code.
    """

    def __init__(self, backend: ConversationProvider, backend_name: str | None = None):
        self.backend = backend
        self.name = f"maak-brain:{backend_name or getattr(backend, 'name', 'provider')}"
        self.degraded = bool(getattr(backend, "degraded", False))

    async def respond(self, context) -> ProviderReply:
        reply = await self.backend.respond(context)
        # Keep the backend/model metadata for diagnostics while exposing one
        # stable product-level provider name to the rest of MAAK.
        reply.provider = self.name
        return reply

    async def classify_route(self, context, draft=None) -> dict[str, Any] | None:
        classifier = getattr(self.backend, "classify_route", None)
        if classifier is None:
            return None
        return await classifier(context, draft)


class RemoteMaakBrain(ConversationProvider):
    """Adapter for a self-hosted MAAK Brain server.

    Contract:
      POST {MAAK_BRAIN_URL}/v1/respond
      POST {MAAK_BRAIN_URL}/v1/classify

    This is intentionally our own small HTTP contract rather than a vendor API,
    so the model server can move from a laptop to a GPU box without changing the
    MAAK application.
    """

    name = "maak-self-hosted"
    degraded = False

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.token = (token or "").strip()
        self.timeout = max(2.0, min(float(timeout), 60.0))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _context_payload(context) -> dict[str, Any]:
        return {
            "message": context.message,
            "history": context.history[-12:],
            "locale": context.locale,
            "active_cases": context.active_cases,
            "attachments": context.attachments[:3],
        }

    async def classify_route(self, context, draft=None) -> dict[str, Any] | None:
        payload = self._context_payload(context)
        payload["active_request_draft"] = draft
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/classify",
                headers=self._headers(),
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
        semantic_response = str(data.get("response") or "").strip()
        if semantic_response:
            setattr(context, "semantic_response", semantic_response)
        setattr(context, "semantic_payload", data)
        return data

    async def respond(self, context) -> ProviderReply:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/v1/respond",
                headers=self._headers(),
                json=self._context_payload(context),
            )
        response.raise_for_status()
        data = response.json()

        style_data = data.get("style") or {}
        style = ResponseStyle(
            language=str(style_data.get("language") or "ar"),
            dialect=str(style_data.get("dialect") or "egyptian"),
            tone=str(style_data.get("tone") or "warm"),
            mood=str(style_data.get("mood") or "neutral"),
            urgency=str(style_data.get("urgency") or "normal"),
            formality=str(style_data.get("formality") or "casual"),
        )
        try:
            intent = Intent(str(data.get("intent") or "GENERAL_QUESTION"))
        except ValueError:
            intent = Intent.GENERAL_QUESTION

        action_data = data.get("action") or {}
        try:
            action_type = ActionType(str(action_data.get("type") or "NONE"))
        except ValueError:
            action_type = ActionType.NONE
        action = ActionProposal(
            action_type,
            bool(action_data.get("authorized", False)),
            float(action_data.get("confidence") or 0.0),
            action_data.get("case_id"),
            action_data.get("case_type"),
            action_data.get("payload") or {},
        )

        return ProviderReply(
            text=str(data.get("response") or "").strip(),
            intent=intent,
            confidence=float(data.get("confidence") or 0.5),
            style=style,
            action=action,
            provider=self.name,
            model=str(data.get("model") or "maak-brain"),
            degraded=bool(data.get("degraded", False)),
        )


def build_brain(existing_provider: ConversationProvider) -> MaakBrain:
    """Build the active brain without leaking vendor choices into app code.

    Priority:
    1. Self-hosted MAAK Brain when MAAK_BRAIN_URL is configured.
    2. Configured Gemini path during the migration period.
    3. Existing local/default provider already built by the application.

    When a remote provider is unavailable, fall back to the existing contextual
    provider instead of the canned deterministic fallback. This preserves the
    conversation history and avoids generic "tell me more" replies during an
    upstream outage.
    """

    timeout = float(os.getenv("CONVERSATION_TIMEOUT_SECONDS", "20"))
    brain_url = (os.getenv("MAAK_BRAIN_URL") or "").strip()
    if brain_url:
        remote = RemoteMaakBrain(
            brain_url,
            token=os.getenv("MAAK_BRAIN_TOKEN"),
            timeout=timeout,
        )
        resilient = ResilientProvider(remote, existing_provider, timeout_seconds=timeout)
        return MaakBrain(resilient, "self-hosted")

    gemini = build_gemini_provider()
    if gemini is not None:
        # A chat turn should never sit for ~20s waiting on an upstream model.
        # Gemini normally answers much faster; if it doesn't, fail over quickly
        # to the local contextual provider so the product still feels alive.
        gemini_timeout = max(2.0, min(timeout, 5.0))
        resilient = ResilientProvider(gemini, existing_provider, timeout_seconds=gemini_timeout)
        return MaakBrain(resilient, "migration-gemini")

    return MaakBrain(existing_provider, "local-default")
