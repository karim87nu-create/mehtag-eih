from fastapi.testclient import TestClient

import app.main as main_module
from app.conversation import LocalGGUFProvider
from app.db import Base, engine
from app.main import app
from app.models import ConversationAction, ConversationMessage, Request


client = TestClient(app)
CUSTOMER = "a940d7e6-6c2b-4f1a-9d2e-76e994c87a54"


def reset():
    client.cookies.clear()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def local_provider(tmp_path, llama):
    model_file = tmp_path / "model.gguf"
    model_file.write_bytes(b"test")
    provider = LocalGGUFProvider(str(model_file))
    provider._llm = llama
    return provider


def send(message, thread_id=None):
    payload = {"message": message, "customer_ref": CUSTOMER, "locale": "ar-EG"}
    if thread_id:
        payload["thread_id"] = thread_id
    return client.post("/api/chat", json=payload)


def test_local_model_repairs_likely_typo_and_uses_previous_turn(tmp_path, monkeypatch):
    reset()

    class ContextModel:
        calls = 0

        def create_chat_completion(self, **kwargs):
            self.calls += 1
            messages = kwargs["messages"]
            system = messages[0]["content"]
            latest = messages[-1]["content"]
            if self.calls == 1:
                assert '"likely_correction": "زهقان"' in system
                assert "طب انا وهقان" in latest
                return {"choices": [{"message": {"content": "غالبًا قصدك زهقان؛ لو ده قصدك نغيّر الجو سوا، ولو لأ صححلي الكلمة."}}]}
            assert any(
                item["role"] == "user" and item["content"] == "طب انا وهقان"
                for item in messages
            )
            assert '"previous_user": "طب انا وهقان"' in system
            return {"choices": [{"message": {"content": "معاك حق؛ من رسالتك اللي فاتت غالبًا قصدك زهقان، وده السياق اللي هكمل منه."}}]}

    model = ContextModel()
    monkeypatch.setattr(main_module, "conversation_provider", local_provider(tmp_path, model))

    first = send("طب انا وهقان")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["provider"]["name"] == "local-gguf"
    assert "زهقان" in first_body["message"]["content"]
    assert "ما تعرفين" not in first_body["message"]["content"]
    assert "غير قادر" not in first_body["message"]["content"]

    second = send("انت المفروض تفهم", first_body["thread_id"])
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert "زهقان" in second_body["message"]["content"]
    assert "كمّل، أنا متابع" not in second_body["message"]["content"]
    assert model.calls == 2

    with main_module.SessionLocal() as db:
        assert db.query(Request).count() == 0
        assert [row.status for row in db.query(ConversationAction).order_by(ConversationAction.id)] == [
            "BLOCKED", "BLOCKED",
        ]
        assert [row.content for row in db.query(ConversationMessage).filter_by(role="USER").order_by(ConversationMessage.id)] == [
            "طب انا وهقان", "انت المفروض تفهم",
        ]


def test_incomplete_request_and_short_followup_stay_model_led_but_block_action(tmp_path, monkeypatch):
    reset()

    class ClarifyingModel:
        calls = 0

        def create_chat_completion(self, **kwargs):
            self.calls += 1
            messages = kwargs["messages"]
            system = messages[0]["content"]
            assert '"missing_request_fields": ["target"]' in system
            assert '"authorized": false' in system
            if self.calls == 1:
                return {"choices": [{"message": {"content": "عايز تطلب إيه بالضبط—حاجة ولا خدمة؟"}}]}
            assert any(
                item["role"] == "user" and item["content"] == "عايز اطلب"
                for item in messages
            )
            return {"choices": [{"message": {"content": "أدورلك على إيه؟ قول الحاجة أو الخدمة الأول."}}]}

    model = ClarifyingModel()
    monkeypatch.setattr(main_module, "conversation_provider", local_provider(tmp_path, model))

    first = send("عايز اطلب")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["intent"] == "NEW_REQUEST"
    assert first_body["action"] == {
        "type": "CREATE_REQUEST",
        "status": "BLOCKED",
        "reason": "Customer intent is not explicit enough for an external action",
    }
    assert "إيه" in first_body["message"]["content"]

    second = send("دور", first_body["thread_id"])
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["intent"] == "NEW_REQUEST"
    assert second_body["action"]["type"] == "CREATE_REQUEST"
    assert second_body["action"]["status"] == "BLOCKED"
    assert "أدورلك على إيه" in second_body["message"]["content"]
    assert model.calls == 2

    with main_module.SessionLocal() as db:
        assert db.query(Request).count() == 0
        assert db.query(ConversationAction).filter_by(status="EXECUTED").count() == 0
