from fastapi.testclient import TestClient
from app.main import app
from app.db import Base, engine
from app.db import SessionLocal
from app.models import MerchantLink

client = TestClient(app)
CUSTOMER = "5ed77d43-36f1-4ee1-9e3c-5ef84be0c1f4"
CUSTOMER_HEADERS = {"X-Customer-Ref": CUSTOMER}

def reset():
    client.cookies.clear()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

def test_home_waits_for_customer_to_start():
    reset()
    r = client.get("/")
    assert r.status_code == 200
    assert "اكتب أو اتكلم" in r.text
    assert "تمام فهمتك" not in r.text
    assert "detectCategory" not in r.text
    assert "AbortController" in r.text
    assert "الاتصال اتقطع قبل ما أعرف النتيجة" in r.text
    assert "رسالتك ما اتحولتش لأي إجراء" not in r.text
    assert "function storedGet" in r.text
    assert "historyReady=initialize()" in r.text

    sw = client.get("/static/sw.js")
    assert sw.status_code == 200
    assert 'mehtag-eih-v3' in sw.text
    assert "skipWaiting" in sw.text
    assert "url.pathname.startsWith('/api/')" in sw.text

def test_request_and_merchant_offer(monkeypatch):
    reset()
    monkeypatch.setenv("MAAK_EXECUTION_ADMIN_TOKEN", "test-admin")
    async def discovery(*_):
        return [{"external_id":"test-cake","name":"مخبز تجريبي","website":"https://example.test","source":"test"}]
    monkeypatch.setattr("app.main.discover_businesses", discovery)
    r = client.post("/requests", data={"text":"عايز تورتة في مدينة نصر بكرة في حدود 1200"}, headers=CUSTOMER_HEADERS)
    assert r.status_code == 200
    with SessionLocal() as db:
        token = db.query(MerchantLink).one().token
    m = client.get("/r/" + token)
    assert m.status_code == 200
    o = client.post("/r/" + token + "/offer",
                    data={"price":"1050","eta":"بكرة 7 مساء","notes":"شامل التوصيل"})
    assert o.status_code == 200
    admin = client.get("/admin", headers={"Authorization":"Bearer test-admin"})
    assert "1050" in admin.text
    assert "OFFER_RECEIVED" in admin.text

def test_above_budget_is_flagged(monkeypatch):
    reset()
    monkeypatch.setenv("MAAK_EXECUTION_ADMIN_TOKEN", "test-admin")
    async def discovery(*_):
        return [{"external_id":"test-cake","name":"مخبز تجريبي","website":"https://example.test","source":"test"}]
    monkeypatch.setattr("app.main.discover_businesses", discovery)
    r = client.post("/requests", data={"text":"عايز تورتة في مدينة نصر بكرة في حدود 1200"}, headers=CUSTOMER_HEADERS)
    with SessionLocal() as db:
        token = db.query(MerchantLink).one().token
    client.post("/r/" + token + "/offer",
                data={"price":"1500","eta":"بكرة","notes":""})
    admin = client.get("/admin", headers={"Authorization":"Bearer test-admin"})
    assert "ABOVE_BUDGET" in admin.text
