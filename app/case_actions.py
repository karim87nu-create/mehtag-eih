"""Durable, ownership-scoped case actions for conversation turns.

Conversation providers may propose actions, but this module is the final database
boundary.  It rechecks the persisted conversation link and returns EXECUTED only
after a commit that created an event/issue or changed business state.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from .conversation import ActionProposal, ActionType, ResponseStyle
from .models import (
    AuditRecord,
    CaseEvent,
    ConversationCaseLink,
    ConversationThread,
    Event,
    ExecutionCase,
    ExternalCase,
    IssueRecord,
    Offer,
    OfferAmendment,
    Request,
)


@dataclass
class ActionExecution:
    status: str
    reason: str
    text: str | None = None
    card: dict[str, Any] | None = None
    linked_case: dict[str, Any] | None = None


class CaseActionRejected(ValueError):
    """A safe policy/validation rejection, not a server failure."""


_AR_STATUS = {
    "DISCOVERING": "البحث شغال، ولسه مفيش تواصل مؤكد",
    "SUPPLY_FOUND_NO_CHANNEL": "اتلاقت جهات، لكن مفيش تواصل مؤكد",
    "NO_REACHABLE_SUPPLY": "مفيش جهة قابلة للتواصل لحد دلوقتي",
    "WAITING_OFFERS": "تم تواصل مؤكد ومستنيين عروض",
    "OFFER_FOUND": "وصل عرض فعلي",
    "SELECTED": "تم اختيار العرض، ولسه التنفيذ ما اتأكدش",
    "AWAITING_PAYMENT": "مستني خطوة الدفع؛ مفيش دفع تم",
    "CONFIRMED": "التنفيذ متأكد",
    "IN_PROGRESS": "التنفيذ شغال",
    "OUT_FOR_DELIVERY": "خرج للتسليم",
    "COMPLETED_PENDING_CONFIRMATION": "الجهة قالت إنه اكتمل ومستني تأكيدك",
    "VERIFIED_OUTCOME": "النتيجة اتأكدت منك",
    "ISSUE_OPEN": "في مشكلة مفتوحة",
    "RESOLVED_PENDING_CONFIRMATION": "في حل مقترح ومستني تأكيدك",
    "CANCELLED": "ملغي",
    "FOLLOWING": "تحت المتابعة من غير نتيجة جديدة مؤكدة",
}

_EN_STATUS = {
    "DISCOVERING": "discovery is in progress; no contact is confirmed",
    "SUPPLY_FOUND_NO_CHANNEL": "possible suppliers were found; no contact is confirmed",
    "NO_REACHABLE_SUPPLY": "no reachable supplier has been found yet",
    "WAITING_OFFERS": "contact is confirmed and offers are pending",
    "OFFER_FOUND": "an actual offer has arrived",
    "SELECTED": "the offer is selected; execution is not confirmed",
    "AWAITING_PAYMENT": "awaiting payment; no payment was made",
    "CONFIRMED": "execution is confirmed",
    "IN_PROGRESS": "execution is in progress",
    "OUT_FOR_DELIVERY": "out for delivery",
    "COMPLETED_PENDING_CONFIRMATION": "the provider reported completion; your confirmation is pending",
    "VERIFIED_OUTCOME": "you verified the outcome",
    "ISSUE_OPEN": "a problem is open",
    "RESOLVED_PENDING_CONFIRMATION": "a proposed resolution awaits your confirmation",
    "CANCELLED": "cancelled",
    "FOLLOWING": "being followed; there is no new confirmed outcome",
}


def case_status_card(case: dict[str, Any], style: ResponseStyle) -> dict[str, Any]:
    """Create a read-only card from persisted case context."""
    case_type = str(case.get("type") or "").upper()
    status = str(case.get("status") or "UNKNOWN").upper()
    title = str(case.get("title") or ("Case" if style.language == "en" else "الموضوع"))
    labels = _EN_STATUS if style.language == "en" else _AR_STATUS
    label = labels.get(status, status)
    paths = {"REQUEST": "requests", "EXECUTION": "cases", "EXTERNAL": "external"}
    return {
        "type": "status",
        "phase": "tracking",
        "title": title[:120],
        "status": status,
        "label": label,
        "case_type": case_type,
        "case_id": case.get("id"),
        "url": f"/{paths.get(case_type, 'cases')}/{case.get('id')}",
        "read_only": True,
    }


def _localized(style: ResponseStyle, ar: str, en: str, mixed: str | None = None) -> str:
    if style.language == "en":
        return en
    if style.language == "mixed" and mixed:
        return mixed
    return ar


def _linked_case(db: Session, thread_id: str, action: ActionProposal) -> ConversationCaseLink:
    if not isinstance(action.case_id, int) or not action.case_type:
        raise CaseActionRejected("An exact linked case is required")
    case_type = str(action.case_type).upper()
    links = db.query(ConversationCaseLink).filter(
        ConversationCaseLink.thread_id == thread_id,
        ConversationCaseLink.case_type == case_type,
        ConversationCaseLink.case_id == action.case_id,
    ).all()
    if len(links) != 1:
        raise CaseActionRejected("The exact case is not uniquely linked to this conversation")
    return links[0]


def _load_case(db: Session, case_type: str, case_id: int):
    models = {"REQUEST": Request, "EXECUTION": ExecutionCase, "EXTERNAL": ExternalCase}
    model = models.get(case_type)
    if model is None:
        raise CaseActionRejected("Unsupported case type")
    row = db.get(model, case_id)
    if row is None:
        raise CaseActionRejected("Linked case no longer exists")
    return row


def _case_title(db: Session, case_type: str, row) -> str:
    if case_type == "REQUEST":
        return row.raw_text[:120]
    if case_type == "EXECUTION":
        request_row = db.get(Request, row.request_id)
        return request_row.raw_text[:120] if request_row else "متابعة تنفيذ"
    return row.title[:120]


def _verify_case_owner(db: Session, case_type: str, row, customer_ref: str) -> None:
    """Verify the whole ownership chain, not only the directly linked row.

    Ownership columns were added to an existing database, so a malformed legacy
    child row must never be allowed to authorize access to another customer's
    parent request or offer.
    """
    if getattr(row, "customer_ref", None) != customer_ref:
        raise CaseActionRejected("The linked case is not owned by this customer")
    if case_type != "EXECUTION":
        return
    request_row = db.get(Request, row.request_id)
    if not request_row or request_row.customer_ref != customer_ref:
        raise CaseActionRejected("The execution case parent is not owned by this customer")
    offer = db.get(Offer, row.offer_id)
    if not offer or offer.request_id != request_row.id:
        raise CaseActionRejected("The execution case offer is not linked to its parent request")


def _persist_problem(
    db: Session,
    case_type: str,
    row,
    issue_text: str,
) -> tuple[str, int, str]:
    if case_type == "EXECUTION":
        issue = IssueRecord(execution_case_id=row.id, issue_text=issue_text, status="OPEN")
        db.add(issue)
        db.add(CaseEvent(case_id=row.id, event_type="ISSUE_OPENED_FROM_CONVERSATION", detail=issue_text))
        db.flush()
        return "IssueRecord", issue.id, row.status
    if case_type == "REQUEST":
        event = Event(request_id=row.id, event_type="ISSUE_OPENED_FROM_CONVERSATION", detail=issue_text)
        db.add(event)
        db.flush()
        return "Event", event.id, row.status
    event = AuditRecord(
        actor="CUSTOMER", action="EXTERNAL_CASE_ISSUE_OPENED",
        entity_type="ExternalCase", entity_id=str(row.id), detail=issue_text,
    )
    db.add(event)
    db.flush()
    return "AuditRecord", event.id, row.status


def _persist_followup(db: Session, case_type: str, row, detail: str) -> tuple[str, int]:
    if case_type == "REQUEST":
        event = Event(request_id=row.id, event_type="CUSTOMER_UPDATE_RECORDED", detail=detail)
    elif case_type == "EXECUTION":
        event = CaseEvent(case_id=row.id, event_type="CUSTOMER_UPDATE_RECORDED", detail=detail)
    else:
        event = AuditRecord(
            actor="CUSTOMER", action="EXTERNAL_CASE_UPDATE_RECORDED",
            entity_type="ExternalCase", entity_id=str(row.id), detail=detail,
        )
    db.add(event)
    db.flush()
    return type(event).__name__, event.id


def _cancel_case(db: Session, case_type: str, row, detail: str) -> tuple[str, int, str]:
    if str(row.status).upper() == "CANCELLED":
        raise CaseActionRejected("The case is already cancelled; no state change was made")
    if case_type == "REQUEST":
        row.status = "CANCELLED"
        event = Event(request_id=row.id, event_type="CANCELLED_BY_CUSTOMER", detail=detail)
    elif case_type == "EXECUTION":
        row.status = "CANCELLED"
        row.outcome_status = "CANCELLED"
        request_row = db.get(Request, row.request_id)
        if request_row:
            request_row.status = "CANCELLED"
        event = CaseEvent(case_id=row.id, event_type="CANCELLED_BY_CUSTOMER", detail=detail)
    else:
        row.status = "CANCELLED"
        row.outcome_status = "CANCELLED"
        event = AuditRecord(
            actor="CUSTOMER", action="EXTERNAL_CASE_CANCELLED",
            entity_type="ExternalCase", entity_id=str(row.id), detail=detail,
        )
    db.add(event)
    db.flush()
    return type(event).__name__, event.id, row.status


def _select_offer(
    db: Session,
    thread_id: str,
    case_type: str,
    row,
    offer_id: int,
) -> tuple[ExecutionCase, int]:
    if case_type != "REQUEST":
        raise CaseActionRejected("An offer may only be selected from its linked request")
    offer = db.query(Offer).filter(
        Offer.id == offer_id, Offer.request_id == row.id, Offer.status == "VALID",
    ).first()
    if not offer:
        raise CaseActionRejected("The exact valid offer is not linked to this request")
    existing = db.query(ExecutionCase).filter(ExecutionCase.request_id == row.id).first()
    if existing:
        raise CaseActionRejected("This request already has a selected offer; no state change was made")
    row.status = "SELECTED"
    execution = ExecutionCase(
        customer_ref=row.customer_ref, request_id=row.id, offer_id=offer.id, status="AWAITING_PAYMENT",
        payment_status="NOT_STARTED", outcome_status="OPEN", expected_at=offer.eta,
    )
    db.add(execution)
    db.flush()
    db.add(Event(request_id=row.id, event_type="OFFER_SELECTED_FROM_CONVERSATION", detail=f"offer_id={offer.id};price={offer.price}"))
    db.add(CaseEvent(case_id=execution.id, event_type="CASE_OPENED", detail="AWAITING_PAYMENT"))
    existing_link = db.query(ConversationCaseLink).filter(
        ConversationCaseLink.thread_id == thread_id,
        ConversationCaseLink.case_type == "EXECUTION",
        ConversationCaseLink.case_id == execution.id,
    ).first()
    if not existing_link:
        db.add(ConversationCaseLink(
            thread_id=thread_id, case_type="EXECUTION", case_id=execution.id,
            relationship="PRIMARY",
        ))
    db.flush()
    return execution, offer.id


def _reject_offer(db: Session, case_type: str, row, offer_id: int) -> Offer:
    if case_type != "REQUEST":
        raise CaseActionRejected("An offer may only be rejected from its linked request")
    offer = db.query(Offer).filter(
        Offer.id == offer_id, Offer.request_id == row.id, Offer.status == "VALID",
    ).first()
    if not offer:
        raise CaseActionRejected("The exact valid offer is not linked to this request")
    offer.status = "REJECTED"
    db.add(Event(request_id=row.id, event_type="OFFER_REJECTED_FROM_CONVERSATION", detail=f"offer_id={offer.id}"))
    db.flush()
    return offer


def _amendment_decision(
    db: Session,
    case_type: str,
    row,
    amendment_id: int,
    approve: bool,
) -> OfferAmendment:
    if case_type != "EXECUTION":
        raise CaseActionRejected("An amendment decision requires its linked execution case")
    amendment = db.query(OfferAmendment).filter(
        OfferAmendment.id == amendment_id, OfferAmendment.status == "PENDING",
    ).first()
    if not amendment:
        raise CaseActionRejected("The exact pending amendment was not found")
    offer = db.get(Offer, amendment.offer_id)
    if not offer or offer.id != row.offer_id:
        raise CaseActionRejected("The amendment is not linked to this execution case")
    if approve:
        if amendment.new_price is not None:
            offer.price = amendment.new_price
        if amendment.new_eta:
            offer.eta = amendment.new_eta
        amendment.status = "APPROVED"
        event_type = "OFFER_AMENDMENT_APPROVED"
    else:
        amendment.status = "REJECTED"
        event_type = "OFFER_AMENDMENT_REJECTED"
    db.add(CaseEvent(case_id=row.id, event_type=event_type, detail=amendment.reason))
    db.add(AuditRecord(
        actor="CUSTOMER", action=event_type,
        entity_type="OfferAmendment", entity_id=str(amendment.id), detail=amendment.reason,
    ))
    db.flush()
    return amendment


def execute_case_action(
    db: Session,
    thread_id: str,
    action: ActionProposal,
    message: str,
    style: ResponseStyle,
) -> ActionExecution:
    """Validate and durably apply one case action.

    Callers must still run the provider-independent gate first. This function is
    the database gate and intentionally treats stale/ambiguous references as
    BLOCKED, not as successful no-ops.
    """
    if not action.authorized:
        return ActionExecution("BLOCKED", "Customer authorization is not explicit")
    try:
        link = _linked_case(db, thread_id, action)
        case_type = str(link.case_type).upper()
        row = _load_case(db, case_type, link.case_id)
        thread = db.get(ConversationThread, thread_id)
        if not thread:
            raise CaseActionRejected("The conversation no longer exists")
        _verify_case_owner(db, case_type, row, thread.customer_ref)
        title = _case_title(db, case_type, row)
        linked_case = {"type": case_type, "id": row.id}

        if action.type == ActionType.RECORD_PROBLEM:
            issue_text = str(action.payload.get("issue_text") or message).strip()
            if len(issue_text) < 8:
                raise CaseActionRejected("A meaningful problem description is required")
            record_type, record_id, status = _persist_problem(db, case_type, row, issue_text)
            db.commit()
            text = _localized(
                style,
                "سجلت المشكلة كمشكلة مفتوحة، ومش هاعتبرها اتحلت غير بعد تأكيدك.",
                "I recorded the problem as open. It will not be marked resolved without your confirmation.",
                "سجلت الـproblem كـopen، ومش هتتعلم resolved غير بعد تأكيدك.",
            )
            card = {
                "type": "problem", "title": title, "status": "OPEN",
                "case_status": status, "case_type": case_type, "case_id": row.id,
                "record_type": record_type, "record_id": record_id,
                "label": _localized(style, "مشكلة مفتوحة وتحت المتابعة", "Open problem under follow-up", "Open problem تحت المتابعة"),
            }
            return ActionExecution("EXECUTED", "Problem record committed", text, card, linked_case)

        if action.type == ActionType.FOLLOW_CASE:
            record_type, record_id = _persist_followup(db, case_type, row, message)
            db.commit()
            text = _localized(
                style,
                "سجلت التحديث على الحالة من غير ما أغيّر وضعها أو أدّعي نتيجة جديدة.",
                "I recorded the update without changing the case status or claiming a new outcome.",
                "سجلت الـupdate من غير ما أغيّر الـstatus أو أدّعي نتيجة جديدة.",
            )
            card = {
                "type": "tracking", "title": title, "status": row.status,
                "case_type": case_type, "case_id": row.id,
                "record_type": record_type, "record_id": record_id,
                "label": _localized(style, "تم حفظ التحديث؛ الحالة كما هي", "Update saved; status unchanged", "الـupdate اتحفظ؛ الـstatus زي ما هو"),
            }
            return ActionExecution("EXECUTED", "Follow-up event committed", text, card, linked_case)

        if action.type != ActionType.APPLY_DECISION:
            raise CaseActionRejected("Unsupported durable case action")

        decision = str(action.payload.get("decision") or "").upper()
        if decision == "CANCEL_CASE":
            record_type, record_id, status = _cancel_case(db, case_type, row, message)
            db.commit()
            text = _localized(style, "ألغيت الحالة المحددة وسجلت الإلغاء.", "I cancelled the selected case and recorded it.", "لغيت الـcase المحددة وسجلت الإلغاء.")
            card = {
                "type": "decision", "decision": decision, "title": title,
                "status": status, "case_type": case_type, "case_id": row.id,
                "record_type": record_type, "record_id": record_id,
                "label": _localized(style, "تم الإلغاء", "Cancelled", "Cancelled"),
            }
            return ActionExecution("EXECUTED", "Cancellation committed", text, card, linked_case)

        if decision == "SELECT_OFFER":
            offer_id = action.payload.get("offer_id")
            if not isinstance(offer_id, int):
                raise CaseActionRejected("An exact offer id is required")
            execution, selected_offer_id = _select_offer(db, thread_id, case_type, row, offer_id)
            db.commit()
            text = _localized(
                style,
                "اخترت العرض المحدد وفتحت متابعة التنفيذ. مفيش دفع حصل.",
                "I selected the exact offer and opened execution tracking. No payment was made.",
                "اخترت الـoffer المحدد وفتحت execution tracking. مفيش payment حصل.",
            )
            card = {
                "type": "decision", "decision": decision, "title": title,
                "status": execution.status, "case_type": "EXECUTION", "case_id": execution.id,
                "offer_id": selected_offer_id,
                "label": _localized(style, "العرض مختار — مستني خطوة الدفع", "Offer selected — awaiting payment", "Offer selected — مستني payment"),
                "url": f"/cases/{execution.id}",
            }
            return ActionExecution(
                "EXECUTED", "Offer selection and execution case committed", text, card,
                {"type": "EXECUTION", "id": execution.id},
            )

        if decision == "REJECT_OFFER":
            offer_id = action.payload.get("offer_id")
            if not isinstance(offer_id, int):
                raise CaseActionRejected("An exact offer id is required")
            offer = _reject_offer(db, case_type, row, offer_id)
            db.commit()
            text = _localized(style, "رفضت العرض المحدد وسجلت القرار.", "I rejected the exact offer and recorded the decision.", "رفضت الـoffer المحدد وسجلت القرار.")
            card = {
                "type": "decision", "decision": decision, "title": title,
                "status": offer.status, "case_type": case_type, "case_id": row.id,
                "offer_id": offer.id, "label": _localized(style, "العرض مرفوض", "Offer rejected", "Offer rejected"),
            }
            return ActionExecution("EXECUTED", "Offer rejection committed", text, card, linked_case)

        if decision in {"APPROVE_AMENDMENT", "REJECT_AMENDMENT"}:
            amendment_id = action.payload.get("amendment_id")
            if not isinstance(amendment_id, int):
                raise CaseActionRejected("An exact amendment id is required")
            amendment = _amendment_decision(
                db, case_type, row, amendment_id, decision == "APPROVE_AMENDMENT",
            )
            db.commit()
            verb_ar = "وافقت على" if amendment.status == "APPROVED" else "رفضت"
            verb_en = "approved" if amendment.status == "APPROVED" else "rejected"
            text = _localized(style, f"{verb_ar} التعديل المحدد وسجلت القرار.", f"I {verb_en} the exact amendment and recorded the decision.")
            card = {
                "type": "decision", "decision": decision, "title": title,
                "status": amendment.status, "case_type": case_type, "case_id": row.id,
                "amendment_id": amendment.id,
                "label": _localized(style, f"التعديل {amendment.status}", f"Amendment {amendment.status.lower()}"),
            }
            return ActionExecution("EXECUTED", "Amendment decision committed", text, card, linked_case)

        raise CaseActionRejected("Unsupported decision")
    except CaseActionRejected as exc:
        db.rollback()
        text = _localized(
            style,
            "ما نفذتش حاجة: الربط أو التفاصيل مش محددين بشكل آمن.",
            "Nothing was applied: the case linkage or target is not exact enough.",
            "مافيش action اتنفذ: الـcase أو الـtarget مش محددين بأمان.",
        )
        return ActionExecution("BLOCKED", str(exc), text=text)
    except Exception as exc:
        db.rollback()
        text = _localized(
            style,
            "حصل عطل أثناء الحفظ، فاعتبرت العملية فاشلة ومش هقول إنها اتنفذت.",
            "Saving failed, so the action is marked failed and is not being reported as completed.",
            "حصل error أثناء الحفظ، فالـaction فشلت ومش هقول إنها اتنفذت.",
        )
        return ActionExecution(
            "FAILED", f"Case action failed safely: {type(exc).__name__}", text=text,
        )
