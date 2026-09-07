from fastapi.testclient import TestClient
from app.main import app
from app.db import Base, engine
from app.db import SessionLocal
from app.models import MerchantLink

client = TestClient(app)

def reset():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

def test_home_waits_for_customer_to_start():
    reset()
    r = client.get("/")
    assert r.status_code == 200
    assert "اكتب أو اتكلم" in r.text
    assert "تمام فهمتك" not in r.text
    assert "detectCategory" not in r.text

def test_request_and_merchant_offer(monkeypatch):
    reset()
    async def discovery(*_):
        return [{"external_id":"test-cake","name":"مخبز تجريبي","website":"https://example.test","source":"test"}]
    monkeypatch.setattr("app.main.discover_businesses", discovery)
    r = client.post("/requests", data={"text":"عايز تورتة في مدينة نصر بكرة في حدود 1200"})
    assert r.status_code == 200
    with SessionLocal() as db:
        token = db.query(MerchantLink).one().token
    m = client.get("/r/" + token)
    assert m.status_code == 200
    o = client.post("/r/" + token + "/offer",
                    data={"price":"1050","eta":"بكرة 7 مساء","notes":"شامل التوصيل"})
    assert o.status_code == 200
    admin = client.get("/admin")
    assert "1050" in admin.text
    assert "OFFER_RECEIVED" in admin.text

def test_above_budget_is_flagged(monkeypatch):
    reset()
    async def discovery(*_):
        return [{"external_id":"test-cake","name":"مخبز تجريبي","website":"https://example.test","source":"test"}]
    monkeypatch.setattr("app.main.discover_businesses", discovery)
    r = client.post("/requests", data={"text":"عايز تورتة في مدينة نصر بكرة في حدود 1200"})
    with SessionLocal() as db:
        token = db.query(MerchantLink).one().token
    client.post("/r/" + token + "/offer",
                data={"price":"1500","eta":"بكرة","notes":""})
    admin = client.get("/admin")
    assert "ABOVE_BUDGET" in admin.text
