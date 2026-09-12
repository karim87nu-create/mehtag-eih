from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

import app.main as main_module
from app.case_actions import execute_case_action
from app.conversation import ActionProposal, ActionType, FallbackProvider, ResponseStyle
from app.db import Base, engine
from app.main import app
from app.migrations import migrate_customer_ownership_schema
from app.models import (
    Business,
    CaseEvent,
    ConversationAction,
    ConversationCaseLink,
    ConversationThread,
    Event,
    ExecutionCase,
    ExternalCase,
    IssueRecord,
    Offer,
    Request,
)


client = TestClient(app)
CUSTOMER = "8dd92227-b656-42a7-b180-f61e1397d9fb"
OTHER_CUSTOMER = "f5a39a1e-d629-4027-857e-c978861dc95d"


def reset():
    client.cookies.clear()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def make_thread_and_request(db, *, status="DISCOVERING"):
    thread = ConversationThread(id="c9299666-a039-46ef-afd7-74a89b537abb", customer_ref=CUSTOMER)
    request_row = Request(
        customer_ref=CUSTOMER, locale="ar-EG", region="EG", currency="EGP",
        raw_text="تصليح تكييف البيت", item="تصليح تكييف", status=status,
    )
    db.add_all([thread, request_row])
    db.commit()
    db.add(ConversationCaseLink(
        thread_id=thread.id, case_type="REQUEST", case_id=request_row.id,
    ))
    db.commit()
    return thread, request_row


def make_execution(db, *, status="IN_PROGRESS"):
    thread, request_row = make_thread_and_request(db, status="SELECTED")
    business = Business(name="شركة صيانة", source="test")
    db.add(business)
    db.commit()
    offer = Offer(
        request_id=request_row.id, business_id=business.id,
        price=900, eta="بكرة", status="VALID",
    )
    db.add(offer)
    db.commit()
    execution = ExecutionCase(
        customer_ref=CUSTOMER, request_id=request_row.id, offer_id=offer.id,
        status=status, payment_status="NOT_STARTED", outcome_status="OPEN",
    )
    db.add(execution)
    db.commit()
    db.add(ConversationCaseLink(
        thread_id=thread.id, case_type="EXECUTION", case_id=execution.id,
    ))
    # Keep one exact active case for mutation tests.
    db.query(ConversationCaseLink).filter_by(
        thread_id=thread.id, case_type="REQUEST",
    ).delete()
    db.commit()
    return thread, request_row, offer, execution


def chat(thread_id, message, *, locale="ar-EG"):
    return client.post("/api/chat", json={
        "thread_id": thread_id,
        "customer_ref": CUSTOMER,
        "locale": locale,
        "message": message,
    })


def test_status_question_reads_persisted_status_without_business_action(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    with main_module.SessionLocal() as db:
        thread, request_row = make_thread_and_request(db, status="WAITING_OFFERS")
        thread_id = thread.id
        request_id = request_row.id

    response = chat(thread_id, "برجاء، حالة الطلب وصلت لفين؟")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "CONTINUATION"
    assert body["style"]["formality"] == "formal"
    assert body["action"]["type"] == "NONE"
    assert body["action"]["status"] == "BLOCKED"
    assert "تواصل مؤكد" in body["message"]["content"]
    assert body["card"]["status"] == "WAITING_OFFERS"
    assert body["card"]["read_only"] is True

    with main_module.SessionLocal() as db:
        assert db.get(Request, request_id).status == "WAITING_OFFERS"
        assert db.query(Event).count() == 0
        action = db.query(ConversationAction).one()
        assert action.action_type == "NONE"
        assert action.status != "EXECUTED"


def test_problem_report_does_not_mutate_without_explicit_record_request(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    with main_module.SessionLocal() as db:
        thread, _, _, execution = make_execution(db)
        thread_id = thread.id
        execution_id = execution.id

    response = chat(thread_id, "الطلب متأخر وأنا متضايق")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "PROBLEM"
    assert body["style"]["mood"] == "upset"
    assert body["action"]["type"] == "RECORD_PROBLEM"
    assert body["action"]["status"] == "BLOCKED"

    with main_module.SessionLocal() as db:
        assert db.query(IssueRecord).count() == 0
        assert db.query(CaseEvent).count() == 0
        assert db.get(ExecutionCase, execution_id).status == "IN_PROGRESS"


def test_explicit_problem_is_durably_recorded_on_exact_execution_case(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    with main_module.SessionLocal() as db:
        thread, request_row, _, execution = make_execution(db)
        thread_id = thread.id
        request_id = request_row.id
        execution_id = execution.id

    response = chat(thread_id, "سجل المشكلة: الطلب وصل متأخر ومتخبط")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"]["type"] == "RECORD_PROBLEM"
    assert body["action"]["status"] == "EXECUTED"
    assert body["card"]["type"] == "problem"
    assert body["card"]["status"] == "OPEN"
    assert body["card"]["record_type"] == "IssueRecord"
    assert "مش هاعتبرها اتحلت" in body["message"]["content"]

    with main_module.SessionLocal() as db:
        issue = db.query(IssueRecord).one()
        assert issue.execution_case_id == execution_id
        assert issue.status == "OPEN"
        event = db.query(CaseEvent).one()
        assert event.event_type == "ISSUE_OPENED_FROM_CONVERSATION"
        # A problem is a durable overlay. It must not erase whether the
        # underlying request/execution is selected, in progress, delivered, etc.
        assert db.get(ExecutionCase, execution_id).status == "IN_PROGRESS"
        assert db.get(Request, request_id).status == "SELECTED"
        assert db.query(ConversationAction).one().status == "EXECUTED"


def test_case_action_boundary_rejects_execution_with_cross_owner_parent():
    reset()
    with main_module.SessionLocal() as db:
        thread, request_row, _, execution = make_execution(db)
        request_row.customer_ref = OTHER_CUSTOMER
        db.commit()
        result = execute_case_action(
            db,
            thread.id,
            ActionProposal(
                ActionType.RECORD_PROBLEM,
                True,
                .99,
                case_id=execution.id,
                case_type="EXECUTION",
                payload={"issue_text": "الطلب وصل متأخر ومتخبط"},
            ),
            "سجل المشكلة: الطلب وصل متأخر ومتخبط",
            ResponseStyle(),
        )
        assert result.status == "BLOCKED"
        assert "parent" in result.reason
        assert db.query(IssueRecord).count() == 0
        assert db.query(CaseEvent).count() == 0
        assert db.get(ExecutionCase, execution.id).status == "IN_PROGRESS"
        assert db.get(Request, request_row.id).status == "SELECTED"


def test_request_problem_overlay_preserves_outreach_lifecycle(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    with main_module.SessionLocal() as db:
        thread, request_row = make_thread_and_request(db, status="WAITING_OFFERS")
        thread_id, request_id = thread.id, request_row.id

    response = chat(thread_id, "سجل المشكلة: المورد اتأخر جدًا ومردش عليا")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"]["status"] == "EXECUTED"
    assert body["card"]["status"] == "OPEN"
    assert body["card"]["case_status"] == "WAITING_OFFERS"
    with main_module.SessionLocal() as db:
        assert db.get(Request, request_id).status == "WAITING_OFFERS"
        assert db.query(Event).filter_by(
            request_id=request_id,
            event_type="ISSUE_OPENED_FROM_CONVERSATION",
        ).count() == 1


def test_external_problem_overlay_preserves_following_lifecycle(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    with main_module.SessionLocal() as db:
        thread = ConversationThread(
            id="d7e78d36-356d-4f0d-9996-c3bf41b036ee",
            customer_ref=CUSTOMER,
        )
        external = ExternalCase(
            customer_ref=CUSTOMER,
            source_text="شحنة خارجية",
            title="متابعة الشحنة",
            status="FOLLOWING",
            outcome_status="OPEN",
        )
        db.add_all([thread, external])
        db.commit()
        db.add(ConversationCaseLink(
            thread_id=thread.id,
            case_type="EXTERNAL",
            case_id=external.id,
        ))
        db.commit()
        thread_id, external_id = thread.id, external.id

    response = chat(thread_id, "سجل المشكلة: تحديث الشحنة متأخر بقاله أسبوع")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"]["status"] == "EXECUTED"
    assert body["card"]["case_status"] == "FOLLOWING"
    with main_module.SessionLocal() as db:
        external = db.get(ExternalCase, external_id)
        assert external.status == "FOLLOWING"
        assert external.outcome_status == "OPEN"


def test_offer_identity_is_unique_for_fresh_database():
    reset()
    with main_module.SessionLocal() as db:
        _, request_row = make_thread_and_request(db, status="OFFER_FOUND")
        business = Business(name="مورد واحد", source="test")
        db.add(business)
        db.commit()
        db.add(Offer(
            request_id=request_row.id, business_id=business.id,
            price=1000, eta="غدًا", status="VALID",
        ))
        db.commit()
        db.add(Offer(
            request_id=request_row.id, business_id=business.id,
            price=1100, eta="بعد غد", status="VALID",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.query(Offer).count() == 1


def test_offer_identity_unique_index_is_added_to_clean_legacy_database(tmp_path):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-offers.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE offers (
                id INTEGER PRIMARY KEY,
                request_id INTEGER NOT NULL,
                business_id INTEGER NOT NULL
            )
        """))
        connection.execute(text(
            "INSERT INTO offers(id, request_id, business_id) VALUES (1, 10, 20)"
        ))

    migrate_customer_ownership_schema(legacy_engine)
    migrate_customer_ownership_schema(legacy_engine)

    with pytest.raises(IntegrityError):
        with legacy_engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO offers(id, request_id, business_id) VALUES (2, 10, 20)"
            ))


def test_problem_is_blocked_when_more_than_one_linked_case_is_ambiguous(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    with main_module.SessionLocal() as db:
        thread, first = make_thread_and_request(db)
        second = Request(
            customer_ref=CUSTOMER, raw_text="شحنة الموبايل", item="موبايل", status="WAITING_OFFERS",
        )
        db.add(second)
        db.commit()
        db.add(ConversationCaseLink(thread_id=thread.id, case_type="REQUEST", case_id=second.id))
        db.commit()
        thread_id = thread.id
        first_id = first.id

    response = chat(thread_id, "سجل المشكلة: الطلب اتأخر جدًا")
    assert response.status_code == 200, response.text
    assert response.json()["action"]["status"] == "BLOCKED"
    with main_module.SessionLocal() as db:
        assert db.query(Event).count() == 0
        assert db.get(Request, first_id).status == "DISCOVERING"


def test_exact_offer_decision_creates_durable_execution_case_without_payment(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    with main_module.SessionLocal() as db:
        thread, request_row = make_thread_and_request(db, status="OFFER_FOUND")
        business = Business(name="مورد", source="test")
        db.add(business)
        db.commit()
        offer = Offer(
            request_id=request_row.id, business_id=business.id,
            price=1200, eta="غدًا", status="VALID",
        )
        db.add(offer)
        db.commit()
        thread_id, request_id, offer_id = thread.id, request_row.id, offer.id

    response = chat(thread_id, f"اختار العرض {offer_id} حالًا")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"] == "DECISION"
    assert body["style"]["urgency"] == "urgent"
    assert body["action"]["type"] == "APPLY_DECISION"
    assert body["action"]["status"] == "EXECUTED"
    assert body["card"]["offer_id"] == offer_id
    assert "مفيش دفع حصل" in body["message"]["content"]

    with main_module.SessionLocal() as db:
        execution = db.query(ExecutionCase).one()
        assert execution.request_id == request_id
        assert execution.offer_id == offer_id
        assert execution.customer_ref == CUSTOMER
        assert execution.payment_status == "NOT_STARTED"
        assert db.get(Request, request_id).status == "SELECTED"
        assert db.query(CaseEvent).filter_by(event_type="CASE_OPENED").count() == 1
        assert db.query(ConversationAction).one().status == "EXECUTED"


def test_decision_with_offer_from_another_request_is_blocked_not_executed(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    with main_module.SessionLocal() as db:
        thread, request_row = make_thread_and_request(db, status="OFFER_FOUND")
        business = Business(name="مورد", source="test")
        other_request = Request(customer_ref=CUSTOMER, raw_text="طلب آخر", status="OFFER_FOUND")
        db.add_all([business, other_request])
        db.commit()
        alien_offer = Offer(
            request_id=other_request.id, business_id=business.id,
            price=100, eta="اليوم", status="VALID",
        )
        db.add(alien_offer)
        db.commit()
        thread_id, request_id, offer_id = thread.id, request_row.id, alien_offer.id

    response = chat(thread_id, f"I approve offer {offer_id}", locale="en-EG")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["style"]["language"] == "en"
    assert body["action"]["status"] == "BLOCKED"
    assert "Nothing was applied" in body["message"]["content"]

    with main_module.SessionLocal() as db:
        assert db.query(ExecutionCase).count() == 0
        assert db.get(Request, request_id).status == "OFFER_FOUND"
        assert db.query(ConversationAction).one().status == "BLOCKED"


def test_repeat_cancellation_is_blocked_instead_of_reporting_a_noop_as_executed(monkeypatch):
    reset()
    monkeypatch.setattr(main_module, "conversation_provider", FallbackProvider())
    with main_module.SessionLocal() as db:
        thread, request_row, _, execution = make_execution(db)
        thread_id, request_id, execution_id = thread.id, request_row.id, execution.id

    first = chat(thread_id, "الغي الحالة")
    assert first.status_code == 200, first.text
    assert first.json()["action"]["status"] == "EXECUTED"

    second = chat(thread_id, "الغي الحالة")
    assert second.status_code == 200, second.text
    assert second.json()["action"]["status"] == "BLOCKED"
    assert "ما نفذتش" in second.json()["message"]["content"]

    with main_module.SessionLocal() as db:
        assert db.get(ExecutionCase, execution_id).status == "CANCELLED"
        assert db.get(Request, request_id).status == "CANCELLED"
        assert db.query(CaseEvent).filter_by(event_type="CANCELLED_BY_CUSTOMER").count() == 1
        assert [row.status for row in db.query(ConversationAction).order_by(ConversationAction.id)] == [
            "EXECUTED", "BLOCKED",
        ]
