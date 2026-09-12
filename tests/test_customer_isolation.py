from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

import app.main as main_module
from app import execution
from app.db import Base, engine, SessionLocal
from app.execution_models import ExecutionNotice
from app.main import app
from app.migrations import migrate_customer_ownership_schema
from app.models import (
    Business,
    ConsentRecord,
    ConversationCaseLink,
    ConversationThread,
    DetectedTransaction,
    ExecutionCase,
    ExternalCase,
    FollowupTask,
    IssueRecord,
    LearnedPreference,
    MemoryFact,
    MobileSourceEvent,
    Offer,
    OfferAmendment,
    Request,
)


CUSTOMER_A = "b0cd3325-c1ae-46da-b37a-3610a70b8dd7"
CUSTOMER_B = "1c861c42-6e68-4ed0-9dd4-11496e3a0cbc"
HEADERS_A = {"X-Customer-Ref": CUSTOMER_A}
HEADERS_B = {"X-Customer-Ref": CUSTOMER_B}


@pytest.fixture(autouse=True)
def reset_database(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setenv("MAAK_EXECUTION_ADMIN_TOKEN", "isolation-admin")
    monkeypatch.setenv("APP_ENV", "test")


def seed_two_customers():
    with SessionLocal() as db:
        business = Business(name="جهة مشتركة", source="test")
        request_a = Request(
            customer_ref=CUSTOMER_A, raw_text="سر عميل ألف: صيانة التكييف",
            item="تكييف", status="DISCOVERING",
        )
        request_b = Request(
            customer_ref=CUSTOMER_B, raw_text="سر عميل باء: شحنة الموبايل",
            item="موبايل", status="DISCOVERING",
        )
        external_a = ExternalCase(
            customer_ref=CUSTOMER_A, source_text="فاتورة ألف الخاصة",
            title="متابعة ألف", status="FOLLOWING", outcome_status="OPEN",
        )
        external_b = ExternalCase(
            customer_ref=CUSTOMER_B, source_text="فاتورة باء الخاصة",
            title="متابعة باء", status="FOLLOWING", outcome_status="OPEN",
        )
        suggestion_a = DetectedTransaction(
            customer_ref=CUSTOMER_A, source="test", raw_text="اقتراح ألف الخاص",
            title="شحنة ألف", transaction_type="طلب / شحنة", status="SUGGESTED",
        )
        suggestion_b = DetectedTransaction(
            customer_ref=CUSTOMER_B, source="test", raw_text="اقتراح باء الخاص",
            title="شحنة باء", transaction_type="طلب / شحنة", status="SUGGESTED",
        )
        memory_a = MemoryFact(
            customer_ref=CUSTOMER_A, memory_type="customer", key="verified_outcome",
            value="ذاكرة ألف الخاصة", source_type="request", confidence=1,
        )
        memory_b = MemoryFact(
            customer_ref=CUSTOMER_B, memory_type="customer", key="verified_outcome",
            value="ذاكرة باء الخاصة", source_type="request", confidence=1,
        )
        preference_a = LearnedPreference(
            customer_ref=CUSTOMER_A, preference_key="time_window",
            preference_value="تفضيل ألف الخاص", evidence_count=3,
            confidence=.8, status="SUGGESTIBLE",
        )
        preference_b = LearnedPreference(
            customer_ref=CUSTOMER_B, preference_key="time_window",
            preference_value="تفضيل باء الخاص", evidence_count=3,
            confidence=.8, status="SUGGESTIBLE",
        )
        db.add_all([
            business, request_a, request_b, external_a, external_b,
            suggestion_a, suggestion_b, memory_a, memory_b,
            preference_a, preference_b,
        ])
        db.commit()
        offer_a = Offer(
            request_id=request_a.id, business_id=business.id,
            price=1000, eta="غدًا", status="VALID",
        )
        offer_b = Offer(
            request_id=request_b.id, business_id=business.id,
            price=1200, eta="غدًا", status="VALID",
        )
        db.add_all([offer_a, offer_b])
        db.commit()
        case_a = ExecutionCase(
            customer_ref=CUSTOMER_A, request_id=request_a.id, offer_id=offer_a.id,
            status="IN_PROGRESS", payment_status="NOT_STARTED", outcome_status="OPEN",
        )
        case_b = ExecutionCase(
            customer_ref=CUSTOMER_B, request_id=request_b.id, offer_id=offer_b.id,
            status="IN_PROGRESS", payment_status="NOT_STARTED", outcome_status="OPEN",
        )
        db.add_all([case_a, case_b])
        db.commit()
        followup_a = FollowupTask(
            external_case_id=external_a.id, trigger_event="DELIVERED",
            action="متابعة ألف المطلوبة", due_at=datetime.utcnow(), status="PENDING",
        )
        followup_b = FollowupTask(
            external_case_id=external_b.id, trigger_event="DELIVERED",
            action="متابعة باء المطلوبة", due_at=datetime.utcnow(), status="PENDING",
        )
        issue_a = IssueRecord(
            execution_case_id=case_a.id, issue_text="مشكلة ألف الخاصة", status="OPEN",
        )
        amendment_a = OfferAmendment(
            offer_id=offer_a.id, new_price=900, reason="تعديل ألف الخاص", status="PENDING",
        )
        db.add_all([followup_a, followup_b, issue_a, amendment_a])
        db.commit()
        return {
            "business": business.id,
            "request_a": request_a.id,
            "offer_a": offer_a.id,
            "case_a": case_a.id,
            "external_a": external_a.id,
            "suggestion_a": suggestion_a.id,
            "memory_a": memory_a.id,
            "preference_a": preference_a.id,
            "followup_a": followup_a.id,
            "issue_a": issue_a.id,
            "amendment_a": amendment_a.id,
        }


def test_home_and_cookie_are_isolated_between_two_anonymous_customers():
    seed_two_customers()
    a = TestClient(app)
    b = TestClient(app)

    anonymous = TestClient(app).get("/")
    assert anonymous.status_code == 200
    assert "سر عميل ألف" not in anonymous.text
    assert "سر عميل باء" not in anonymous.text

    page_a = a.get("/", headers=HEADERS_A)
    assert page_a.status_code == 200
    assert "سر عميل ألف" in page_a.text
    assert "سر عميل باء" not in page_a.text
    assert "اقتراح ألف" in page_a.text
    assert "اقتراح باء" not in page_a.text
    cookie = page_a.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert page_a.headers["cache-control"] == "private, no-store"

    # The protected cookie carries the same anonymous namespace on later pages.
    same_a = a.get("/")
    assert "سر عميل ألف" in same_a.text and "سر عميل باء" not in same_a.text
    page_b = b.get("/", headers=HEADERS_B)
    assert "سر عميل باء" in page_b.text and "سر عميل ألف" not in page_b.text

    # A conflicting bearer and cookie is rejected instead of changing identity.
    assert a.get("/", headers=HEADERS_B).status_code == 403
    assert a.post("/api/customer/session", headers=HEADERS_B).status_code == 403

    # Obsolete, invalid cookies can be safely replaced by a valid new capability.
    recovered = TestClient(app)
    recovered.cookies.set(
        "maak_customer_ref_v1", "anonymous", domain="testserver.local", path="/",
    )
    synced = recovered.post("/api/customer/session", headers=HEADERS_A)
    assert synced.status_code == 200
    assert recovered.cookies.get("maak_customer_ref_v1") == CUSTOMER_A


def test_customer_idor_reads_and_mutations_are_404_and_leave_rows_unchanged():
    ids = seed_two_customers()
    attacker = TestClient(app)

    for path in (
        f"/requests/{ids['request_a']}",
        f"/cases/{ids['case_a']}",
        f"/external/{ids['external_a']}",
    ):
        assert attacker.get(path, headers=HEADERS_B).status_code == 404

    attacks = (
        (f"/requests/{ids['request_a']}/select/{ids['offer_a']}", {}),
        (f"/cases/{ids['case_a']}/outcome", {"result": "ok"}),
        (f"/cases/{ids['case_a']}/issue", {"issue_text": "هجوم على حالة الغير"}),
        (f"/external/{ids['external_a']}/outcome", {"result": "ok"}),
        (f"/suggestions/{ids['suggestion_a']}/dismiss", {}),
        (f"/followups/{ids['followup_a']}/done", {}),
        (f"/preferences/{ids['preference_a']}/confirm", {}),
        (f"/issues/{ids['issue_a']}/confirm", {"result": "ok"}),
        (f"/amendments/{ids['amendment_a']}/decision", {"decision": "approve"}),
    )
    for path, data in attacks:
        assert attacker.post(path, data=data, headers=HEADERS_B, follow_redirects=False).status_code == 404

    memory_b = attacker.get("/memory", headers=HEADERS_B)
    assert "ذاكرة ألف" not in memory_b.text
    assert "ذاكرة باء" in memory_b.text
    with SessionLocal() as db:
        assert db.get(Request, ids["request_a"]).status == "DISCOVERING"
        assert db.get(ExecutionCase, ids["case_a"]).status == "IN_PROGRESS"
        assert db.get(ExternalCase, ids["external_a"]).status == "FOLLOWING"
        assert db.get(DetectedTransaction, ids["suggestion_a"]).status == "SUGGESTED"
        assert db.get(FollowupTask, ids["followup_a"]).status == "PENDING"
        assert db.get(LearnedPreference, ids["preference_a"]).status == "SUGGESTIBLE"
        assert db.get(IssueRecord, ids["issue_a"]).status == "OPEN"
        assert db.get(OfferAmendment, ids["amendment_a"]).status == "PENDING"
        assert db.query(IssueRecord).filter(IssueRecord.issue_text == "هجوم على حالة الغير").count() == 0


def test_conversation_links_cannot_bridge_customer_namespaces():
    with SessionLocal() as db:
        thread_b = ConversationThread(id="thread-b", customer_ref=CUSTOMER_B)
        request_a = Request(
            customer_ref=CUSTOMER_A, raw_text="طلب ألف السري", status="DISCOVERING",
        )
        db.add_all([thread_b, request_a])
        db.commit()
        db.add(ConversationCaseLink(
            thread_id=thread_b.id, case_type="REQUEST", case_id=request_a.id,
        ))
        db.add(ExecutionNotice(
            thread_id=thread_b.id, request_id=request_a.id,
            event_key="cross-owner", content="إشعار ألف السري",
        ))
        business = Business(name="جهة ألف", source="test")
        db.add(business)
        db.flush()
        db.add(Offer(
            request_id=request_a.id, business_id=business.id, price=333,
            eta="بكرة", notes="تفاصيل ألف السرية", status="VALID",
        ))
        db.commit()
        execution.refresh_notices(db)
        assert db.query(main_module.ConversationMessage).count() == 0

    customer_b = TestClient(app)
    history = customer_b.get("/api/conversations/thread-b", headers=HEADERS_B)
    assert history.status_code == 200
    assert history.json()["cases"] == []
    assert history.json()["messages"] == []
    execution_response = customer_b.get("/api/execution/conversations/thread-b", headers=HEADERS_B)
    assert execution_response.status_code == 200
    assert execution_response.json()["requests"] == []
    assert execution_response.json()["notices"] == []

    # Query parameters are no longer accepted as bearer capabilities.
    assert TestClient(app).get(
        f"/api/conversations/thread-b?customer_ref={CUSTOMER_B}"
    ).status_code == 401


def test_mobile_sources_require_per_customer_consent_and_manual_share_is_one_event():
    a = TestClient(app)
    b = TestClient(app)
    notification = {
        "source": "NOTIFICATION", "package_name": "com.shop",
        "title": "طلبك", "body": "الشحنة خرجت للتوصيل",
    }
    assert a.post("/api/mobile/source", data=notification, headers=HEADERS_A).status_code == 403

    granted = a.post(
        "/consent",
        data={"source_type": "NOTIFICATION", "decision": "grant", "subject_ref": CUSTOMER_B},
        headers=HEADERS_A,
        follow_redirects=False,
    )
    assert granted.status_code == 303
    assert a.post("/api/mobile/source", data=notification, headers=HEADERS_A).status_code == 200
    assert b.post("/api/mobile/source", data=notification, headers=HEADERS_B).status_code == 403
    assert b.post(
        "/consent", data={"source_type": "NOTIFICATION", "decision": "grant"},
        headers=HEADERS_B, follow_redirects=False,
    ).status_code == 303
    assert b.post("/api/mobile/source", data=notification, headers=HEADERS_B).status_code == 200

    assert a.post(
        "/consent", data={"source_type": "NOTIFICATION", "decision": "revoke"},
        headers=HEADERS_A, follow_redirects=False,
    ).status_code == 303
    assert a.post("/api/mobile/source", data=notification, headers=HEADERS_A).status_code == 403
    shared = a.post("/api/mobile/share", data={"text": "حجز يدوي خاص"}, headers=HEADERS_A)
    assert shared.status_code == 200

    with SessionLocal() as db:
        events = db.query(MobileSourceEvent).order_by(MobileSourceEvent.id).all()
        assert [row.customer_ref for row in events] == [CUSTOMER_A, CUSTOMER_B, CUSTOMER_A]
        assert events[0].event_hash != events[1].event_hash
        forged = db.query(ConsentRecord).filter(
            ConsentRecord.consent_type == "EXTERNAL_SOURCE:NOTIFICATION",
            ConsentRecord.status == "GRANTED",
        ).order_by(ConsentRecord.id).first()
        assert forged.subject_ref == CUSTOMER_A
        assert db.query(ConsentRecord).filter(
            ConsentRecord.subject_ref == CUSTOMER_A,
            ConsentRecord.consent_type.like("MANUAL_EVENT:SHARE:%"),
        ).count() == 1


def test_all_operator_dashboards_require_admin_authentication():
    ids = seed_two_customers()
    visitor = TestClient(app)
    paths = (
        "/admin", "/ops", "/supply", "/integrations",
        f"/businesses/{ids['business']}/capability",
    )
    for path in paths:
        assert visitor.get(path).status_code == 403
        assert visitor.get(
            path, headers={"Authorization": "Bearer isolation-admin"},
        ).status_code == 200


def test_legacy_sqlite_migration_backfills_only_unambiguous_valid_owners(tmp_path):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE conversation_threads (
                id VARCHAR PRIMARY KEY, customer_ref VARCHAR NOT NULL,
                locale VARCHAR, region VARCHAR, currency VARCHAR
            )
        """))
        connection.execute(text("""
            CREATE TABLE conversation_case_links (
                id INTEGER PRIMARY KEY, thread_id VARCHAR NOT NULL,
                case_type VARCHAR NOT NULL, case_id INTEGER NOT NULL
            )
        """))
        connection.execute(text("CREATE TABLE requests (id INTEGER PRIMARY KEY, raw_text TEXT NOT NULL)"))
        connection.execute(text("CREATE TABLE execution_cases (id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL)"))
        connection.execute(text("CREATE TABLE external_cases (id INTEGER PRIMARY KEY, source_text TEXT NOT NULL, title VARCHAR NOT NULL)"))
        connection.execute(text("""
            CREATE TABLE memory_facts (
                id INTEGER PRIMARY KEY, memory_type VARCHAR NOT NULL,
                key VARCHAR NOT NULL, value TEXT NOT NULL,
                source_type VARCHAR NOT NULL, source_id INTEGER
            )
        """))
        connection.execute(text("CREATE TABLE mobile_source_events (id INTEGER PRIMARY KEY, event_hash VARCHAR NOT NULL)"))
        connection.execute(text("""
            INSERT INTO conversation_threads(id, customer_ref, locale, region, currency)
            VALUES ('a', :a, 'ar-EG', 'EG', 'EGP'),
                   ('b', :b, 'en-US', 'US', 'USD'),
                   ('legacy', 'anonymous', 'ar-EG', 'EG', 'EGP')
        """), {"a": CUSTOMER_A, "b": CUSTOMER_B})
        connection.execute(text("""
            INSERT INTO requests(id, raw_text) VALUES
                (1, 'owned'), (2, 'unlinked'), (3, 'ambiguous'), (4, 'old anonymous')
        """))
        connection.execute(text("""
            INSERT INTO conversation_case_links(id, thread_id, case_type, case_id) VALUES
                (1, 'a', 'REQUEST', 1),
                (2, 'a', 'REQUEST', 3), (3, 'b', 'REQUEST', 3),
                (4, 'legacy', 'REQUEST', 4), (5, 'a', 'EXTERNAL', 10)
        """))
        connection.execute(text("INSERT INTO execution_cases(id, request_id) VALUES (20, 1)"))
        connection.execute(text("INSERT INTO external_cases(id, source_text, title) VALUES (10, 'x', 'external')"))
        connection.execute(text("""
            INSERT INTO memory_facts(id, memory_type, key, value, source_type, source_id)
            VALUES (30, 'customer', 'verified_outcome', 'memory', 'request', 1)
        """))

    migrate_customer_ownership_schema(legacy_engine)
    migrate_customer_ownership_schema(legacy_engine)  # idempotent on restart

    with legacy_engine.connect() as connection:
        owners = dict(connection.execute(text("SELECT id, customer_ref FROM requests")).tuples().all())
        assert owners == {1: CUSTOMER_A, 2: None, 3: None, 4: None}
        assert connection.execute(text("SELECT customer_ref FROM execution_cases WHERE id=20")).scalar_one() == CUSTOMER_A
        assert connection.execute(text("SELECT customer_ref FROM external_cases WHERE id=10")).scalar_one() == CUSTOMER_A
        assert connection.execute(text("SELECT customer_ref FROM memory_facts WHERE id=30")).scalar_one() == CUSTOMER_A
        assert {column["name"] for column in inspect(legacy_engine).get_columns("mobile_source_events")} >= {
            "customer_ref", "locale", "region", "currency",
        }
        assert "ix_requests_customer_ref" in {
            index["name"] for index in inspect(legacy_engine).get_indexes("requests")
        }
