import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.media_entrypoint import DynamicConversationProvider, app


def test_home_cache_busts_media_script_and_retry_is_supported():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "/static/media.js?v=7db23a7-stable" in response.text
    assert "/static/media-hotfix.js?v=7db23a7-stable" in response.text

    script = TestClient(app).get("/static/media.js").text
    assert "lastImageQuery" in script
    assert "followupCue.test(text)" in script
    assert "مش هقول إن الصور جاهزة وهي مش ظاهرة" in script


def test_response_timing_is_visible_without_changing_the_payload():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert float(response.headers["X-Maak-Response-Ms"]) >= 0
    assert response.headers["Server-Timing"].startswith("app;dur=")


def test_where_followup_after_image_request_does_not_attach_old_request():
    provider = DynamicConversationProvider(SimpleNamespace(name="stub", degraded=False))
    context = SimpleNamespace(
        message="ايوه فين؟",
        history=[
            {"role": "user", "content": "عايز صور موتوسيكل RKV250"},
            {"role": "assistant", "content": "هحاول أعرض لك صور متاحة فعلًا"},
        ],
    )

    reply = asyncio.run(provider.respond(context))

    assert reply.intent.value == "GENERAL_QUESTION"
    assert reply.action.type.value == "NONE"
    assert "هعيد عرض نفس الصور" in reply.text
