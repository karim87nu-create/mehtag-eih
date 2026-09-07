import asyncio

from fastapi.testclient import TestClient

import app.main as main_module
from app.conversation import (
    ActionProposal, ActionType, FallbackProvider, Intent, ProviderReply,
    ResponseStyle, TurnContext, gate_action,
)
from app.db import Base, engine
from app.main import app
from app.models import ConversationAction, ConversationMessage, ConversationThread, Request


client = TestClient(app)


def reset():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def fallback(message, cases=None):
    return asyncio.run(FallbackProvider().respond(TurnContext(message, [], "ar-EG", cases or [])))


def test_language_and_tone_adaptation():
    ar = fallback("أنا متضايق ومحتاج ده حالاً!!")
    assert ar.style.language == "ar"
    assert ar.style.mood == "upset"
    assert ar.style.urgency == "urgent"
    en = fallback("Please tell me a joke")
    assert en.style.language == "en"
    assert "peace of mind" in en.text
    mixed = fallback("محتاج book فندق")
    assert mixed.style.language == "mixed"


def test_small_talk_and_questions_do_not_cross_action_gate():
    joke = fallback("قولّي نكتة")
    assert joke.intent == Intent.SMALL_TALK
    assert joke.action.type == ActionType.NONE
    assert not gate_action(joke, []).allowed
    question = fallback("إيه الفرق بين العرض والسعر؟")
    assert question.intent == Intent.GENERAL_QUESTION
    assert question.action.type == ActionType.NONE


def test_gate_rejects_action_for_wrong_intent():
    reply = ProviderReply(
        "answer", Intent.GENERAL_QUESTION, .99, ResponseStyle(),
        ActionProposal(ActionType.CREATE_REQUEST, True, .99, payload={"text": "x"}),
    )
    decision = gate_action(reply, [])
    assert not decision.allowed
    assert "Only a new-request" in decision.reason


def test_chat_persists_thread_messages_and_blocks_unwarranted_action(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    response = client.post("/api/chat", json={"message":"قولّي نكتة", "customer_ref":"cust-1", "locale":"ar-EG"})
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "SMALL_TALK"
    assert body["provider"]["degraded"] is True
    assert body["action"]["status"] == "BLOCKED"

    with main_module.SessionLocal() as db:
        assert db.query(ConversationThread).count() == 1
        assert db.query(ConversationMessage).count() == 2
        action = db.query(ConversationAction).one()
        assert action.status == "BLOCKED"
        assert db.query(Request).count() == 0

    history = client.get(f"/api/conversations/{body['thread_id']}?customer_ref=cust-1")
    assert history.status_code == 200
    assert [m["role"] for m in history.json()["messages"]] == ["user", "assistant"]


def test_explicit_request_runs_discovery_but_never_claims_outreach(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    async def fake_discovery(*_):
        return [{"external_id":"supplier-1", "name":"جهة متاحة", "website":"https://example.test", "source":"test"}]
    monkeypatch.setattr(main_module, "discover_businesses", fake_discovery)

    response = client.post("/api/chat", json={"message":"دورلي على سباك في المعادي", "customer_ref":"cust-2"})
    assert response.status_code == 200
    body = response.json()
    assert body["action"]["status"] == "EXECUTED"
    assert body["card"]["status"] == "SUPPLY_FOUND_NO_CHANNEL"
    assert "مفيش تواصل مؤكد" in body["card"]["label"]
    assert body["card"]["phase"] == "discovery"

    with main_module.SessionLocal() as db:
        request = db.query(Request).one()
        assert request.status == "SUPPLY_FOUND_NO_CHANNEL"
