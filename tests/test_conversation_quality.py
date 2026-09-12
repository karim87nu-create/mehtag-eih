import asyncio

import pytest

import app.conversation as conversation
from app.conversation import (
    ActionProposal,
    ActionType,
    FallbackProvider,
    Intent,
    LocalGGUFProvider,
    ProviderReply,
    ResponseStyle,
    TurnContext,
    enforce_case_turn_policy,
    gate_action,
)


def run(provider, context):
    return asyncio.run(provider.respond(context))


def local_provider(tmp_path, llama):
    model_file = tmp_path / "model.gguf"
    model_file.write_bytes(b"test")
    provider = LocalGGUFProvider(str(model_file), model_name="test-model")
    provider._llm = llama
    return provider


@pytest.mark.parametrize(
    ("message", "allowed"),
    [
        ("دورلي على سباك في الدقي", True),
        ("find me a plumber near Dokki", True),
        ("مش عايزك تدور على سباك، بس بنتكلم", False),
        ("ما تدورش على فندق دلوقتي", False),
        ("هل تقدر تدورلي على سباك؟", False),
        ("Can you find me a plumber?", False),
        ("I only want advice, don't book a hotel", False),
        ("دور بس متعملش أي حاجة؛ إحنا بنتكلم", False),
        ("I want to buy a motorcycle someday", False),
    ],
)
def test_turn_context_is_the_canonical_action_authority(message, allowed):
    context = TurnContext(message, [], "ar-EG", [])
    # Start with an intentionally over-authorized model result. The server must
    # reconstruct authority from the real customer turn rather than trust it.
    model_reply = ProviderReply(
        "رد طبيعي",
        Intent.NEW_REQUEST,
        0.99,
        ResponseStyle(),
        ActionProposal(ActionType.CREATE_REQUEST, True, 0.99, payload={"text": "invented"}),
        "untrusted-test-model",
    )
    safe_reply = enforce_case_turn_policy(model_reply, context)
    assert gate_action(safe_reply, []).allowed is allowed
    assert safe_reply.action.authorized is allowed
    if allowed:
        assert safe_reply.action.payload["text"] == message


def test_short_command_resolves_only_the_immediately_previous_concrete_target():
    concrete = "عايز موتوسيكل مستعمل لحد 50 ألف في المعادي"
    context = TurnContext(
        "دور",
        [
            {"role": "user", "content": concrete},
            {"role": "assistant", "content": "الجديد ولا المستعمل؟"},
        ],
        "ar-EG",
        [],
    )
    reply = run(FallbackProvider(), context)
    assert reply.action.authorized is True
    assert reply.action.payload["text"] == concrete
    assert gate_action(reply, []).allowed

    interrupted = TurnContext(
        "دور",
        [
            {"role": "user", "content": concrete},
            {"role": "assistant", "content": "الجديد ولا المستعمل؟"},
            {"role": "user", "content": "لسه بفكر"},
            {"role": "assistant", "content": "براحتك"},
        ],
        "ar-EG",
        [],
    )
    blocked = run(FallbackProvider(), interrupted)
    assert blocked.action.authorized is False
    assert not gate_action(blocked, []).allowed

    emotional = TurnContext(
        "دور",
        [
            {"role": "user", "content": "أنا مخنوق ومش عارف أبدأ منين"},
            {"role": "assistant", "content": "نبدأ بخطوة صغيرة"},
        ],
        "ar-EG",
        [],
    )
    assert run(FallbackProvider(), emotional).action.authorized is False


def test_emotional_tone_rolls_forward_from_recent_user_context():
    context = TurnContext(
        "مش عارف أبدأ منين",
        [
            {"role": "user", "content": "أنا مخنوق ومتوتر من الشغل"},
            {"role": "assistant", "content": "خلينا نفكها بهدوء"},
        ],
        "ar-EG",
        [],
    )
    reply = run(FallbackProvider(), context)
    assert reply.style.mood == "upset"
    assert reply.style.tone == "calm"
    assert reply.style.language == "ar"


def test_short_followup_keeps_established_mixed_language_style():
    context = TurnContext(
        "وكمل",
        [
            {"role": "user", "content": "عايز plan بسيط عشان أنظم وقتي"},
            {"role": "assistant", "content": "نقسم اليوم لبلوكات صغيرة"},
        ],
        "ar-EG",
        [],
    )
    reply = run(FallbackProvider(), context)
    assert reply.style.language == "mixed"
    assert reply.style.dialect == "egyptian"


def test_arbitrary_advice_turn_is_model_led_and_does_not_call_wikipedia(tmp_path, monkeypatch):
    calls = {"model": 0, "knowledge": 0}

    async def knowledge_must_not_run(_):
        calls["knowledge"] += 1
        raise AssertionError("conversational advice is not a knowledge lookup")

    class AdviceModel:
        def create_chat_completion(self, **kwargs):
            calls["model"] += 1
            assert '"answer_mode": "ADVICE"' in kwargs["messages"][0]["content"]
            assert kwargs["messages"][-1]["content"].startswith("/no_think")
            return {
                "choices": [{
                    "message": {
                        "content": "خلّي هدفك صفحتين بس كل يوم؛ الاستمرارية الصغيرة هنا أهم من الحماس الكبير."
                    }
                }]
            }

    monkeypatch.setattr(conversation, "_wikipedia_knowledge", knowledge_must_not_run)
    provider = local_provider(tmp_path, AdviceModel())
    context = TurnContext(
        "تنصحني أعمل إيه؟ بقالي أسبوع ببدأ كتاب وبقف بعد صفحتين.",
        [{"role": "user", "content": "بحاول أرجع للقراءة"}],
        "ar-EG",
        [],
    )
    reply = run(provider, context)
    assert calls == {"model": 1, "knowledge": 0}
    assert "صفحتين" in reply.text
    assert reply.provider == "local-gguf"


def test_local_model_gets_one_corrective_retry_for_refusal_and_strips_thinking(tmp_path):
    class RetryModel:
        def __init__(self):
            self.prompts = []

        def create_chat_completion(self, **kwargs):
            self.prompts.append(kwargs["messages"][0]["content"])
            if len(self.prompts) == 1:
                return {
                    "choices": [{
                        "message": {"content": "<think>سأرفض</think> عذرًا، لا أستطيع فهمك."}
                    }]
                }
            return {
                "choices": [{
                    "message": {
                        "content": "<think>رد مباشر</think> نبدأ بخطوة صغيرة: قول أكتر جزء معطلك وأنا أمسكه معاك."
                    }
                }]
            }

    model = RetryModel()
    provider = local_provider(tmp_path, model)
    reply = run(provider, TurnContext("أنا تايه في الموضوع ده", [], "ar-EG", []))
    assert len(model.prompts) == 2
    assert "CORRECTIVE RETRY" not in model.prompts[0]
    assert "CORRECTIVE RETRY" in model.prompts[1]
    assert "<think>" not in reply.text
    assert "خطوة صغيرة" in reply.text


def test_local_history_is_bounded_but_keeps_the_newest_context(tmp_path):
    captured = {}

    class HistoryModel:
        def create_chat_completion(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return {"choices": [{"message": {"content": "نكمل من آخر نقطة بدل ما نعيد من الأول."}}]}

    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"turn-{index}-" + ("x" * 500)}
        for index in range(30)
    ]
    provider = local_provider(tmp_path, HistoryModel())
    run(provider, TurnContext("كمّل من هنا", history, "ar-EG", []))
    dialogue = captured["messages"][1:-1]
    assert len(dialogue) <= 6
    assert sum(len(item["content"]) for item in dialogue) <= 1100
    assert "turn-29" in dialogue[-1]["content"]


def test_remote_provider_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("LOCAL_MODEL_PATH", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "present-but-not-authorized")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    default = conversation.build_provider()
    assert default.primary is None
    assert default.degraded is True

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    opted_in = conversation.build_provider()
    assert isinstance(opted_in.primary, conversation.OpenAICompatibleProvider)
    assert opted_in.degraded is False
