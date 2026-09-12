import asyncio

from fastapi.testclient import TestClient

from app.locale import resolve_locale
from app import services
import app.main as main_module
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Business, ExecutionCase, Offer, PaymentIntent, Request


CUSTOMER = "f160a85c-b6d3-4da5-adc7-1504a0fd1f98"


def test_egypt_remains_the_operational_default():
    locale = resolve_locale(None)
    assert (locale.locale, locale.language, locale.region) == ("ar-EG", "ar", "EG")
    assert (locale.currency, locale.timezone, locale.country_code) == ("EGP", "Africa/Cairo", "eg")
    assert services._query("سباك", "المعادي", locale).endswith("مصر")


def test_known_global_locales_keep_their_region_and_currency():
    assert resolve_locale("en_EG").currency == "EGP"
    assert resolve_locale("en-US").currency == "USD"
    assert resolve_locale("en-GB").currency == "GBP"
    assert services._query("plumber", "Boston", "en-US").endswith("United States")
    assert services._query("hotel", "London", "en-GB").endswith("United Kingdom")


def test_unknown_valid_locale_is_not_silently_changed_to_egypt():
    locale = resolve_locale("sw-KE")
    assert (locale.locale, locale.language, locale.region) == ("sw-KE", "sw", "KE")
    assert locale.currency == "XXX"
    assert locale.timezone == "UTC"
    assert locale.country_code == "ke"
    assert "مصر" not in services._query("hotel", "Nairobi", locale)


def test_invalid_locale_fails_safe_to_configured_default_shape():
    locale = resolve_locale("not a locale")
    assert (locale.locale, locale.region, locale.currency) == ("ar-EG", "EG", "EGP")


def test_osm_uses_locale_specific_country_and_language(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url, headers=None):
            captured["url"] = url
            return Response()

    monkeypatch.setattr(services.httpx, "AsyncClient", lambda **_: Client())
    asyncio.run(services._osm("plumber Boston United States", "en-US"))
    assert "countrycodes=us" in captured["url"]
    assert "accept-language=en" in captured["url"]


def test_direct_request_propagates_non_egypt_locale_to_discovery_and_storage(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    captured = {}

    async def discovery(text, area=None, locale_context=None):
        captured["locale"] = locale_context
        return []

    monkeypatch.setattr(main_module, "discover_businesses", discovery)
    client = TestClient(app)
    response = client.post(
        "/requests",
        data={"text": "Find a bicycle in Boston"},
        headers={"X-Customer-Ref": CUSTOMER, "Accept-Language": "en-US"},
    )
    assert response.status_code == 200
    assert (captured["locale"].region, captured["locale"].currency) == ("US", "USD")
    with SessionLocal() as db:
        row = db.query(Request).one()
        assert (row.locale, row.region, row.currency) == ("en-US", "US", "USD")


def test_payment_intent_uses_request_currency_not_model_default():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        request_row = Request(
            customer_ref=CUSTOMER, locale="en-US", region="US", currency="USD",
            raw_text="Find a bicycle", status="OFFER_FOUND",
        )
        business = Business(name="Bike store", source="test")
        db.add_all([request_row, business]); db.commit()
        offer = Offer(
            request_id=request_row.id, business_id=business.id,
            price=100, eta="tomorrow", status="VALID",
        )
        db.add(offer); db.commit()
        case = ExecutionCase(
            customer_ref=CUSTOMER, request_id=request_row.id, offer_id=offer.id,
        )
        db.add(case); db.commit()
        payment = main_module.create_payment_intent_for_case(db, case)
        assert payment.currency == "USD"
        assert db.query(PaymentIntent).one().currency == "USD"
