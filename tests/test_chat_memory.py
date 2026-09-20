import uuid

from fastapi.testclient import TestClient

import app.main as main_module
from app.conversation import ActionProposal, ActionType, Intent, ProviderReply, ResponseStyle
from app.db import Base, engine
from app.models import MemoryFact


CUSTOMER_A = "a1bf7475-a69a-4f44-8982-c209b5f5748c"
CUSTOMER_B = "b2cf8586-b70b-4055-9093-d310c6a6859d"
client = TestClient(main_module.app)


def reset():
    client.cookies.clear()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def send(message, customer=CUSTOMER_A, thread_id=None):
    client.cookies.clear()
    payload = {
        "message": message,
        "client_turn_id": str(uuid.uuid4()),
        "locale": "ar-EG",
    }
    if thread_id:
        payload["thread_id"] = thread_id
    return client.post("/api/chat", json=payload, headers={"X-Customer-Ref": customer})


def test_explicit_memory_survives_new_conversation_and_can_be_forgotten():
    reset()
    saved = send("افتكر إن اسمي كريم")
    assert saved.status_code == 200, saved.text
    assert saved.json()["provider"]["name"] == "maak-memory"
    assert "افتكرتها" in saved.json()["message"]["content"]

    recalled = send("إنت فاكر عني إيه؟")
    assert recalled.status_code == 200
    assert "اسمي كريم" in recalled.json()["message"]["content"]

    other_customer = send("إنت فاكر عني إيه؟", CUSTOMER_B)
    assert "مفيش معلومات مؤكدة" in other_customer.json()["message"]["content"]

    forgotten = send("انسى إن اسمي كريم")
    assert "نسيت المعلومة" in forgotten.json()["message"]["content"]
    assert "مفيش معلومات مؤكدة" in send("إنت فاكر عني إيه؟").json()["message"]["content"]


def test_memory_correction_replaces_old_fact_without_duplicates():
    reset()
    send("افتكر إن اسمي كريم")
    corrected = send("بدل ما تفتكر اسمي كريم، افتكر إن اسمي محمود")
    assert "صححت المعلومة" in corrected.json()["message"]["content"]
    recalled = send("إنت فاكر عني إيه؟").json()["message"]["content"]
    assert "اسمي محمود" in recalled
    assert "اسمي كريم" not in recalled
    with main_module.SessionLocal() as db:
        assert db.query(MemoryFact).filter_by(customer_ref=CUSTOMER_A, source_type="chat_explicit").count() == 1


def test_confirmed_memory_is_passed_to_normal_model_turn(monkeypatch):
    reset()
    send("افتكر إن بفضل الرد المختصر")

    class CapturingProvider:
        name = "capture"
        degraded = False
        context = None

        async def respond(self, context):
            self.context = context
            return ProviderReply(
                "رد مختصر", Intent.GENERAL_QUESTION, 1.0, ResponseStyle(),
                ActionProposal(ActionType.NONE, False, 1.0),
                provider=self.name, model="test", degraded=False,
            )

    provider = CapturingProvider()
    monkeypatch.setattr(main_module, "conversation_provider", provider)
    response = send("قولي فكرة عملية")
    assert response.status_code == 200
    assert provider.context is not None
    assert provider.context.memories == [
        {"type": "fact", "key": provider.context.memories[0]["key"], "value": "بفضل الرد المختصر"}
    ]


def test_natural_stable_facts_are_learned_and_corrected_automatically(monkeypatch):
    reset()

    class CapturingProvider:
        name = "capture"
        degraded = False
        contexts = []

        async def respond(self, context):
            self.contexts.append(context)
            return ProviderReply(
                "تمام", Intent.GENERAL_QUESTION, 1.0, ResponseStyle(),
                ActionProposal(ActionType.NONE, False, 1.0),
                provider=self.name, model="test", degraded=False,
            )

    provider = CapturingProvider()
    monkeypatch.setattr(main_module, "conversation_provider", provider)
    send("اسمي كريم")
    assert {item["value"] for item in provider.contexts[-1].memories} == {"اسمي كريم"}

    send("اسمي محمود")
    values = {item["value"] for item in provider.contexts[-1].memories}
    assert "اسمي محمود" in values
    assert "اسمي كريم" not in values
    assert "اسمي محمود" in send("إنت فاكر عني إيه؟").json()["message"]["content"]


def test_natural_style_and_dialect_preferences_are_learned_without_hijacking_reply(monkeypatch):
    reset()

    class CapturingProvider:
        name = "capture"
        degraded = False
        context = None

        async def respond(self, context):
            self.context = context
            return ProviderReply(
                "رد النموذج", Intent.GENERAL_QUESTION, 1.0, ResponseStyle(),
                ActionProposal(ActionType.NONE, False, 1.0),
                provider=self.name, model="test", degraded=False,
            )

    provider = CapturingProvider()
    monkeypatch.setattr(main_module, "conversation_provider", provider)
    response = send("أنا بفضل الرد المختصر")
    assert response.json()["provider"]["name"] == "capture"
    assert response.json()["message"]["content"] == "رد النموذج"
    assert any(item["key"] == "preference:response_style" for item in provider.context.memories)

    send("كلمني بالمصري")
    assert any(item["key"] == "preference:language" for item in provider.context.memories)


def test_automatic_memory_rejects_temporary_and_sensitive_details():
    reset()
    send("النهارده عايز الرد مختصر")
    send("رقم بطاقتي 4111111111111111")
    send("كلمة السر بتاعتي secret123")
    send("اسمي إيه؟")
    send("أنا دايمًا بفضل 4111111111111111")
    with main_module.SessionLocal() as db:
        assert db.query(MemoryFact).filter_by(customer_ref=CUSTOMER_A, source_type="chat_auto").count() == 0


def test_automatic_memory_is_isolated_and_can_be_forgotten():
    reset()
    send("اسمي كريم")
    assert "مفيش معلومات مؤكدة" in send("إنت فاكر عني إيه؟", CUSTOMER_B).json()["message"]["content"]
    assert "نسيت المعلومة" in send("انسى إن اسمي كريم").json()["message"]["content"]
    assert "مفيش معلومات مؤكدة" in send("إنت فاكر عني إيه؟").json()["message"]["content"]
