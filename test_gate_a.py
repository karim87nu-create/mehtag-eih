from fastapi.testclient import TestClient
from app.main import app
from app.db import Base, engine

client = TestClient(app)

def reset():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

def test_home():
    reset()
    r = client.get("/")
    assert r.status_code == 200
    assert "محتاج إيه؟" in r.text

def test_request_and_merchant_offer():
    reset()
    r = client.post("/requests", data={"text":"عايز تورتة في مدينة نصر بكرة في حدود 1200"})
    assert r.status_code == 200
    # Demo discovery creates one website-reachable merchant link.
    marker = '/r/'
    assert marker in r.text
    token = r.text.split(marker,1)[1].split('"',1)[0]
    m = client.get("/r/" + token)
    assert m.status_code == 200
    o = client.post("/r/" + token + "/offer",
                    data={"price":"1050","eta":"بكرة 7 مساء","notes":"شامل التوصيل"})
    assert o.status_code == 200
    admin = client.get("/admin")
    assert "1050" in admin.text
    assert "OFFER_RECEIVED" in admin.text

def test_above_budget_is_flagged():
    reset()
    r = client.post("/requests", data={"text":"عايز تورتة في مدينة نصر بكرة في حدود 1200"})
    token = r.text.split('/r/',1)[1].split('"',1)[0]
    client.post("/r/" + token + "/offer",
                data={"price":"1500","eta":"بكرة","notes":""})
    admin = client.get("/admin")
    assert "ABOVE_BUDGET" in admin.text
