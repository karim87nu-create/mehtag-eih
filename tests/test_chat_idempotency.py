from datetime import datetime, timedelta
import uuid

from fastapi.testclient import TestClient

import app.main as main_module
from app.conversation import ActionProposal, ActionType, Intent, ProviderReply, ResponseStyle
from app.db import Base, engine
from app.models import (
    ConversationAction, ConversationCaseLink, ConversationMessage,
    ConversationThread, ConversationTurn, Request,
)


CUSTOMER = "619d17b8-e850-43f4-90ce-90b759eb22e1"
client = TestClient(main_module.app)


class CountingProvider:
    name = "test"
    degraded = False

    def __init__(self, action=False):
        self.calls = 0
        self.action = action

    async def respond(self, context):
        self.calls += 1
        proposal = ActionProposal()
        intent = Intent.SMALL_TALK
        if self.action:
            intent = Intent.NEW_REQUEST
            proposal = ActionProposal(
                ActionType.CREATE_REQUEST, True, .99,
                payload={"text": context.message},
            )
        return ProviderReply(
            "رد واحد", intent, .99, ResponseStyle(), proposal,
            provider=self.name, model="test-model", degraded=False,
        )


def reset():
    client.cookies.clear()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def post(message, turn_id, thread_id=None):
    body = {"message": message, "client_turn_id": turn_id, "locale": "ar-EG"}
    if thread_id:
        body["thread_id"] = thread_id
    return client.post("/api/chat", json=body, headers={"X-Customer-Ref": CUSTOMER})


def test_completed_turn_replays_exact_envelope_without_provider_or_rows(monkeypatch):
    reset()
    provider = CountingProvider()
    monkeypatch.setattr(main_module, "conversation_provider", provider)
    turn_id = str(uuid.uuid4())

    first = post("مساء الفل", turn_id)
    second = post("مساء الفل", turn_id)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert provider.calls == 1
    with main_module.SessionLocal() as db:
        assert db.query(ConversationTurn).one().status == "COMPLETED"
        assert db.query(ConversationMessage).count() == 2
        assert db.query(ConversationAction).count() == 1
    assert post("نص مختلف", turn_id).status_code == 409


def test_processing_turn_rejects_concurrent_replay_and_stale_turn_resumes(monkeypatch):
    reset()
    provider = CountingProvider()
    monkeypatch.setattr(main_module, "conversation_provider", provider)
    turn_id = str(uuid.uuid4())
    message = "كمل معايا"
    with main_module.SessionLocal() as db:
        thread = ConversationThread(id=str(uuid.uuid4()), customer_ref=CUSTOMER)
        db.add(thread); db.flush()
        turn = ConversationTurn(
            thread_id=thread.id, customer_ref=CUSTOMER, client_turn_id=turn_id,
            request_fingerprint=main_module._chat_turn_fingerprint(message, "ar-EG"),
            status="PROCESSING", lease_token=str(uuid.uuid4()),
        )
        db.add(turn); db.commit()
        thread_id = thread.id

    busy = post(message, turn_id, thread_id)
    assert busy.status_code == 409
    assert busy.headers["retry-after"] == "2"
    with main_module.SessionLocal() as db:
        turn = db.query(ConversationTurn).one()
        turn.updated_at = datetime.utcnow() - timedelta(seconds=main_module.CHAT_TURN_STALE_SECONDS + 1)
        db.commit()

    resumed = post(message, turn_id, thread_id)
    assert resumed.status_code == 200, resumed.text
    assert provider.calls == 1
    with main_module.SessionLocal() as db:
        assert db.query(ConversationTurn).one().attempt_count == 2
        assert db.query(ConversationMessage).count() == 2


def test_create_request_is_once_per_client_turn(monkeypatch):
    reset()
    provider = CountingProvider(action=True)
    monkeypatch.setattr(main_module, "conversation_provider", provider)

    async def no_businesses(*_args, **_kwargs):
        return []

    monkeypatch.setattr(main_module, "discover_businesses", no_businesses)
    turn_id = str(uuid.uuid4())
    first = post("دورلي على سباك في المعادي", turn_id)
    second = post("دورلي على سباك في المعادي", turn_id)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    with main_module.SessionLocal() as db:
        assert db.query(Request).count() == 1
        assert db.query(Request).one().source_turn_id == turn_id
        assert db.query(ConversationCaseLink).count() == 1
        assert db.query(ConversationAction).filter_by(status="EXECUTED").count() == 1

