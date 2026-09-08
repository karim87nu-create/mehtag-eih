import asyncio

from fastapi.testclient import TestClient

import app.main as main_module
from app.conversation import (
    ActionProposal, ActionType, FallbackProvider, Intent, ProviderReply,
    LocalGGUFProvider, ResilientProvider, ResponseStyle, TurnContext, _local_system_prompt,
    build_provider, gate_action,
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
    assert "Windows open" in en.text
    mixed = fallback("محتاج book فندق")
    assert mixed.style.language == "mixed"


def test_common_social_turns_are_natural_and_contextual():
    hello = fallback("عامل إيه؟")
    assert hello.intent == Intent.SMALL_TALK
    assert "إنت عامل إيه" in hello.text
    assert "أنا متأكد" not in hello.text

    confused = asyncio.run(FallbackProvider().respond(TurnContext(
        "مش فاهمك", [{"role": "assistant", "content": "رد سابق"}], "ar-EG", [],
    )))
    assert confused.intent == Intent.SMALL_TALK
    assert "ردي اللي فات" in confused.text

    challenge = fallback("متأكد من إيه؟")
    assert "الرد ده كان غلط" in challenge.text

    greeted_request = fallback("هاي، دورلي على سباك")
    assert greeted_request.intent == Intent.NEW_REQUEST
    assert greeted_request.action.authorized is True
    assert gate_action(greeted_request, []).allowed


def test_repeated_joke_request_gets_a_complete_different_joke():
    first = asyncio.run(FallbackProvider().respond(TurnContext("قولي نكتة", [], "ar-EG", [])))
    second = asyncio.run(FallbackProvider().respond(TurnContext(
        "قولي نكتة", [
            {"role": "user", "content": "قولي نكتة"},
            {"role": "assistant", "content": first.text},
        ], "ar-EG", [],
    )))
    assert first.text != second.text
    assert "😄" in first.text and "😄" in second.text


def test_small_talk_and_questions_do_not_cross_action_gate():
    joke = fallback("قولّي نكتة")
    assert joke.intent == Intent.SMALL_TALK
    assert joke.action.type == ActionType.NONE
    assert not gate_action(joke, []).allowed
    question = fallback("إيه الفرق بين العرض والسعر؟")
    assert question.intent == Intent.GENERAL_QUESTION
    assert question.action.type == ActionType.NONE


def test_degraded_mode_explains_itself_instead_of_repeating_generic_text():
    reply = fallback("يعني إيه غير متصلة؟")
    assert reply.intent == Intent.GENERAL_QUESTION
    assert "المحادثة الذكية الكاملة" in reply.text
    assert "أنا هنا. احكي براحتك" not in reply.text


def test_gate_rejects_action_for_wrong_intent():
    reply = ProviderReply(
        "answer", Intent.GENERAL_QUESTION, .99, ResponseStyle(),
        ActionProposal(ActionType.CREATE_REQUEST, True, .99, payload={"text": "x"}),
    )
    decision = gate_action(reply, [])
    assert not decision.allowed
    assert "Only a new-request" in decision.reason


def test_local_model_adapts_language_but_cannot_invent_action(tmp_path, monkeypatch):
    model_file = tmp_path / "model.gguf"
    model_file.write_bytes(b"test")
    provider = LocalGGUFProvider(str(model_file))

    class FakeLlama:
        def create_chat_completion(self, **kwargs):
            assert "response_format" not in kwargs
            return {"choices": [{"message": {"content": "مرة واحد بخيل... ضحك بالتقسيط 😄"}}]}

    provider._llm = FakeLlama()
    reply = asyncio.run(provider.respond(TurnContext("قولّي نكتة", [], "ar-EG", [])))
    assert reply.provider == "local-gguf"
    assert reply.degraded is False
    assert reply.style.language == "ar"
    assert reply.action.authorized is False
    assert not gate_action(reply, []).allowed


def test_local_model_rejects_repetitive_nonsense_and_bad_history(tmp_path):
    model_file = tmp_path / "model.gguf"
    model_file.write_bytes(b"test")
    provider = LocalGGUFProvider(str(model_file))
    captured = {}

    class BadLlama:
        def create_chat_completion(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return {"choices": [{"message": {"content": "أنا متأكد."}}]}

    provider._llm = BadLlama()
    context = TurnContext(
        "ليه السما لونها أزرق؟",
        [{"role": "assistant", "content": "أنا متأكد."}],
        "ar-EG", [],
    )
    reply = asyncio.run(provider.respond(context))
    assert reply.text != "أنا متأكد."
    assert all(message["content"] != "أنا متأكد." for message in captured["messages"])


def test_mixed_food_language_is_disambiguated_and_action_stays_blocked(tmp_path):
    context = TurnContext(
        "محتاج recommendation لعشا light بس مش عايزك تطلب حاجة",
        [], "ar-EG", [],
    )
    baseline = fallback(context.message)
    prompt = _local_system_prompt(context, baseline)
    assert "أكل خفيف" in prompt
    assert "وليست إضاءة" in prompt
    assert baseline.action.authorized is False
    assert not gate_action(baseline, []).allowed

    model_file = tmp_path / "model.gguf"
    model_file.write_bytes(b"test")
    provider = LocalGGUFProvider(str(model_file))

    class MustNotGenerate:
        def create_chat_completion(self, **_):
            raise AssertionError("vetted semantic reply must bypass unreliable generation")

    provider._llm = MustNotGenerate()
    reply = asyncio.run(provider.respond(context))
    assert "أومليت" in reply.text
    assert "إضاءة" not in reply.text
    assert "مش هاطلب" in reply.text
    assert reply.provider == "local-gguf"


def test_build_provider_prefers_existing_local_model(tmp_path, monkeypatch):
    model_file = tmp_path / "model.gguf"
    model_file.write_bytes(b"test")
    monkeypatch.setenv("LOCAL_MODEL_PATH", str(model_file))
    monkeypatch.setenv("LLM_API_KEY", "must-not-be-selected")
    provider = build_provider()
    assert provider.name == "local-gguf"
    assert provider.degraded is False


def test_resilient_provider_times_out_to_an_honest_fallback():
    class SlowProvider(FallbackProvider):
        name = "slow-test"

        async def respond(self, context):
            await asyncio.sleep(1.2)
            return await super().respond(context)

    provider = ResilientProvider(SlowProvider(), FallbackProvider(), timeout_seconds=1)
    reply = asyncio.run(provider.respond(TurnContext("عامل إيه؟", [], "ar-EG", [])))
    assert reply.provider == "local-fallback"
    assert reply.degraded is True
    assert "إنت عامل إيه" in reply.text


def test_local_model_preserves_explicit_customer_authorization(tmp_path):
    model_file = tmp_path / "model.gguf"
    model_file.write_bytes(b"test")
    provider = LocalGGUFProvider(str(model_file))

    class FakeLlama:
        def create_chat_completion(self, **_):
            return {"choices": [{"message": {"content": "تمام فهمتك، هساعدك أدور."}}]}

    provider._llm = FakeLlama()
    reply = asyncio.run(provider.respond(TurnContext("دورلي على سباك", [], "ar-EG", [])))
    assert not reply.text.startswith("تمام فهمتك")
    assert reply.action.type == ActionType.CREATE_REQUEST
    assert reply.action.authorized is True
    assert gate_action(reply, []).allowed


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
