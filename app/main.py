import re
import hashlib
import json
import uuid
import os
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, Request as FastAPIRequest, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
from .db import Base, engine, SessionLocal
from .migrations import migrate_customer_ownership_schema
from .models import Request, Business, MerchantLink, Offer, Event, ExecutionCase, CaseEvent, ExternalCase, MemoryFact, DetectedTransaction, TransactionEvent, FollowupRule, FollowupTask, LearnedPreference, BusinessPerformance, OfferAmendment, IssueRecord, CustomerRule, CapabilitySignal, ReachAttempt, BusinessActivation, IntegrationEndpoint, TransportDelivery, PaymentIntent, ConsentRecord, AuditRecord, MobileSourceEvent, ConversationThread, ConversationMessage, ConversationTurn, ConversationCaseLink, ConversationAction
from .services import understand_request, discover_businesses, build_reachability, new_token
from .conversation import ActionType, TurnContext, build_provider, enforce_case_turn_policy, gate_action
from .case_actions import case_status_card, execute_case_action
from .execution_models import ExecutionNotice
from .locale import resolve_locale
from .ownership import (
    CUSTOMER_COOKIE,
    CUSTOMER_HEADER,
    canonical_customer_ref,
    customer_ref_from_request,
    is_execution_admin,
    normalize_source_type,
    require_customer_ref,
    source_consent_type,
)

Base.metadata.create_all(bind=engine)
migrate_customer_ownership_schema(engine)
app = FastAPI(title="معاك", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
templates = Jinja2Templates(directory="app/templates")

LINK_TTL_HOURS = 24
CONNECTED_SOURCE_TYPES = {"NOTIFICATION", "EMAIL_CONNECTOR"}
conversation_provider = build_provider()
CHAT_TURN_STALE_SECONDS = max(30, int(os.getenv("CHAT_TURN_STALE_SECONDS", "300")))


def _set_customer_cookie(response, customer_ref: str, request: FastAPIRequest) -> None:
    response.set_cookie(
        CUSTOMER_COOKIE,
        customer_ref,
        max_age=60 * 60 * 24 * 365,
        path="/",
        secure=request.url.scheme == "https" or os.getenv("APP_ENV", "").lower() == "production",
        httponly=True,
        samesite="lax",
    )


@app.middleware("http")
async def customer_capability_cookie(request: FastAPIRequest, call_next):
    """Upgrade a validated browser/header capability to a protected cookie.

    The first browser visit intentionally renders no private data.  The home
    client creates or reuses a cryptographically random UUID, syncs it once,
    then reloads.  Mobile clients keep using the same value as a bearer header.
    """
    try:
        customer_ref = customer_ref_from_request(request, required=False)
    except HTTPException:
        customer_ref = None
    if customer_ref:
        request.state.customer_ref = customer_ref
    response = await call_next(request)
    try:
        customer_ref = customer_ref_from_request(request, required=False)
    except HTTPException:
        customer_ref = None
    if customer_ref:
        _set_customer_cookie(response, customer_ref, request)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = "Cookie, X-Customer-Ref"
    return response

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _customer_context(request: FastAPIRequest, explicit: str | None = None) -> tuple[str, object]:
    customer_ref = customer_ref_from_request(request, explicit)
    request.state.customer_ref = customer_ref
    locale = resolve_locale(request.headers.get("accept-language", "ar-EG").split(",", 1)[0])
    return customer_ref, locale


def _require_execution_admin(request: FastAPIRequest) -> None:
    if not is_execution_admin(request):
        raise HTTPException(403, "Execution administrator authentication required")


def _owned_request(db: Session, request_id: int, customer_ref: str) -> Request:
    row = db.query(Request).filter(
        Request.id == request_id, Request.customer_ref == customer_ref,
    ).first()
    if not row:
        raise HTTPException(404, "Request not found")
    return row


def _owned_execution_case(db: Session, case_id: int, customer_ref: str) -> ExecutionCase:
    row = db.query(ExecutionCase).filter(
        ExecutionCase.id == case_id, ExecutionCase.customer_ref == customer_ref,
    ).first()
    if not row:
        raise HTTPException(404, "Case not found")
    # Defense in depth: both the case and its parent request must agree.
    parent = db.query(Request.id).filter(
        Request.id == row.request_id, Request.customer_ref == customer_ref,
    ).first()
    if not parent:
        raise HTTPException(404, "Case not found")
    return row


def _owned_external_case(db: Session, case_id: int, customer_ref: str) -> ExternalCase:
    row = db.query(ExternalCase).filter(
        ExternalCase.id == case_id, ExternalCase.customer_ref == customer_ref,
    ).first()
    if not row:
        raise HTTPException(404, "External case not found")
    return row


def _request_for_customer_or_admin(db: Session, request_id: int, request: FastAPIRequest) -> Request:
    if is_execution_admin(request):
        row = db.get(Request, request_id)
        if not row:
            raise HTTPException(404, "Request not found")
        return row
    return _owned_request(db, request_id, customer_ref_from_request(request))


def _case_for_customer_or_admin(db: Session, case_id: int, request: FastAPIRequest) -> ExecutionCase:
    if is_execution_admin(request):
        row = db.get(ExecutionCase, case_id)
        if not row:
            raise HTTPException(404, "Case not found")
        return row
    return _owned_execution_case(db, case_id, customer_ref_from_request(request))


def _active_source_consent(db: Session, customer_ref: str, source: str) -> bool:
    consent_type = source_consent_type(source)
    latest = db.query(ConsentRecord).filter(
        ConsentRecord.subject_type == "CUSTOMER",
        ConsentRecord.subject_ref == customer_ref,
        ConsentRecord.consent_type == consent_type,
    ).order_by(ConsentRecord.id.desc()).first()
    return bool(latest and latest.status == "GRANTED")


def _manual_event_consent(db: Session, customer_ref: str, event_hash: str, source: str) -> None:
    """Record explicit, one-event consent without granting background access."""
    consent_type = f"MANUAL_EVENT:{normalize_source_type(source)}:{event_hash}"
    existing = db.query(ConsentRecord).filter(
        ConsentRecord.subject_type == "CUSTOMER",
        ConsentRecord.subject_ref == customer_ref,
        ConsentRecord.consent_type == consent_type,
    ).first()
    if not existing:
        db.add(ConsentRecord(
            subject_type="CUSTOMER", subject_ref=customer_ref,
            consent_type=consent_type, status="GRANTED", source="EXPLICIT_EVENT_SUBMISSION",
        ))
        db.commit()

def log_event(db, request_id, event_type, detail=""):
    db.add(Event(request_id=request_id, event_type=event_type, detail=detail))
    db.commit()

def valid_offer(req, price):
    if req.budget is not None and price > req.budget:
        return False, "ABOVE_BUDGET"
    return True, "VALID"

def rank_offers(req, offers):
    # Gate A: valid first, then price. Time is displayed but not probabilistically interpreted.
    return sorted(offers, key=lambda o: (0 if o.status == "VALID" else 1, o.price))


def understand_request_v2(text: str):
    """Structured deterministic parser for Stage 1.
    It recognizes directed, remembered/open intent and extracts common constraints.
    """
    raw = " ".join(text.split())
    low = raw.lower()

    # Intent
    directed_patterns = [
        r'(?:من|مِن)\s+([A-Za-z\u0600-\u06FF][A-Za-z0-9\u0600-\u06FF .&_-]{1,40})',
        r'(?:اطلبلي|هاتلي|جيبلي)\s+.+?\s+(?:من|مِن)\s+([A-Za-z\u0600-\u06FF][A-Za-z0-9\u0600-\u06FF .&_-]{1,40})'
    ]
    merchant = None
    for p in directed_patterns:
        mm = re.search(p, raw, re.I)
        if mm:
            merchant = mm.group(1).strip(" .،")
            break

    remembered_words = ["المعتاد", "زي كل مرة", "نفس اللي فات", "نفسه تاني", "كرر"]
    if any(x in low for x in remembered_words):
        intent_type = "REMEMBERED"
    elif merchant:
        intent_type = "DIRECTED"
    else:
        intent_type = "OPEN"

    # Budget
    budget = None
    bm = re.search(r'(?:حدود|لحد|ميزاني(?:ة|تي)|بحد أقصى|بحد اقصى)\s*([0-9٠-٩][0-9٠-٩,\.]*)\s*(?:ج|جنيه|الف|ألف|k)?', raw, re.I)
    if bm:
        digits = bm.group(1).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩","0123456789")).replace(",","")
        try:
            budget=float(digits)
            tail=raw[bm.end()-8:bm.end()+8].lower()
            if "الف" in tail or "ألف" in tail or "k" in tail:
                budget*=1000
        except: pass

    # Area and deadline/time are intentionally lightweight
    area = None
    am = re.search(r'(?:يوصل|توصيل|في|لـ|الى|إلى)\s+(مدينة نصر|مصر الجديدة|التجمع|القاهرة الجديدة|مدينتي|المعادي|الزمالك|الدقي|الهرم|الجيزة)', raw)
    if am: area=am.group(1)

    deadline = None
    dm = re.search(r'(اليوم|بكرة|غدا|غداً|الأحد|الاحد|الاثنين|الإثنين|الثلاثاء|الأربعاء|الاربعاء|الخميس|الجمعة|السبت)(?:\s+(?:الساعة\s*)?\d{1,2}(?::\d{2})?\s*(?:ص|م)?)?', raw)
    if dm: deadline=dm.group(0)

    return {
        "raw": raw, "intent_type": intent_type, "directed_merchant": merchant,
        "budget": budget, "area": area, "deadline": deadline
    }

def service_fee_for_request(req, offer):
    """Transparent prototype fee, per case—not per message.
    Values are illustrative until willingness-to-pay validation.
    """
    price = float(offer.price or 0)
    if price <= 500:
        return 25.0
    if price <= 3000:
        return 45.0
    if price <= 15000:
        return 75.0
    return 120.0

def get_case_offer(db, case):
    return db.query(Offer).filter(Offer.id == case.offer_id).first()

def get_pending_amendment(db, offer_id):
    return db.query(OfferAmendment).filter(
        OfferAmendment.offer_id == offer_id,
        OfferAmendment.status == "PENDING"
    ).order_by(OfferAmendment.id.desc()).first()


def classify_reachability(biz):
    """Never invent a lawful first-contact path.
    DIRECT means prior explicit activation/consent.
    DISCOVERED_REACHABLE currently means an executable official web endpoint exists.
    Phone numbers alone are NOT treated as permission for automated outreach.
    """
    if getattr(biz, "activation_status", None) == "DIRECT":
        return "DIRECT", "DIRECT"
    website = getattr(biz, "website", None)
    if website and str(website).startswith(("http://","https://")):
        return "DISCOVERED_REACHABLE", "OFFICIAL_WEB"
    return "DISCOVERED_UNREACHABLE", None

def get_activation(db, business_id):
    return db.query(BusinessActivation).filter(BusinessActivation.business_id==business_id).first()

def route_request_to_business(db, req, biz):
    activation=get_activation(db,biz.id)
    if activation and activation.status=="DIRECT" and activation.endpoint:
        channel=activation.preferred_channel or "DIRECT"
        endpoint=activation.endpoint
        status="SENT"
        reason="prior explicit activation"
    else:
        state,channel=classify_reachability(biz)
        endpoint=getattr(biz,"website",None)
        if state=="DISCOVERED_REACHABLE":
            # Stage 2 proves routing eligibility and token creation.
            # It does NOT claim an arbitrary website can receive a machine request.
            status="PENDING"
            reason="official endpoint discovered; adapter required before real send"
        else:
            status="SKIPPED"
            reason="no permitted automated endpoint"
            channel="NONE"
    attempt=ReachAttempt(request_id=req.id,business_id=biz.id,channel=channel or "NONE",
                         endpoint=endpoint,status=status,reason=reason)
    db.add(attempt); db.commit(); db.refresh(attempt)
    return attempt

def learn_capability_from_offer(db, offer, req):
    capability=(req.raw_text or "")[:120]
    row=db.query(CapabilitySignal).filter(
        CapabilitySignal.business_id==offer.business_id,
        CapabilitySignal.capability==capability
    ).first()
    if not row:
        row=CapabilitySignal(business_id=offer.business_id,capability=capability,
                             area=None,accepted_count=0,completed_count=0)
        db.add(row)
    row.accepted_count += 1
    row.last_seen_at=datetime.utcnow()
    db.commit()

def mark_capability_completed(db, offer, req):
    capability=(req.raw_text or "")[:120]
    row=db.query(CapabilitySignal).filter(
        CapabilitySignal.business_id==offer.business_id,
        CapabilitySignal.capability==capability
    ).first()
    if row:
        row.completed_count += 1
        row.last_seen_at=datetime.utcnow()
        db.commit()


def configured_transport(db, business_id):
    return db.query(IntegrationEndpoint).filter(
        IntegrationEndpoint.business_id==business_id,
        IntegrationEndpoint.status=="ACTIVE"
    ).first()

def deliver_request(db, attempt):
    """Real adapter boundary. We only mark SENT if an ACTIVE configured endpoint exists.
    No fake cold WhatsApp delivery.
    """
    ep=configured_transport(db,attempt.business_id)
    if not ep:
        d=TransportDelivery(reach_attempt_id=attempt.id,transport="NONE",status="FAILED",
                            error="No ACTIVE permitted transport configured")
        db.add(d); db.commit(); return d
    # Webhook/API are represented as active adapters; network call intentionally delegated
    # to provider-specific implementation/config, not silently simulated as success.
    d=TransportDelivery(reach_attempt_id=attempt.id,transport=ep.kind,status="QUEUED")
    db.add(d); db.commit(); db.refresh(d)
    return d

def create_payment_intent_for_case(db, case):
    offer=get_case_offer(db,case)
    req=db.query(Request).filter(Request.id==case.request_id).first()
    if not offer or not req: return None
    fee=service_fee_for_request(req,offer)
    currency = req.currency or resolve_locale(req.locale).currency
    pi=PaymentIntent(execution_case_id=case.id,amount=float(offer.price)+fee,
                     service_fee=fee,currency=currency,
                     provider="SIMULATION",status="CREATED")
    db.add(pi); db.commit(); db.refresh(pi); return pi


def audit(db, action, entity_type=None, entity_id=None, detail=None, actor="SYSTEM"):
    row=AuditRecord(actor=actor,action=action,entity_type=entity_type,
                    entity_id=str(entity_id) if entity_id is not None else None,
                    detail=detail)
    db.add(row); db.commit(); return row

def system_health(db):
    return {
        "db": "ok",
        "conversation": {
            "provider": conversation_provider.name,
            "degraded": conversation_provider.degraded,
        },
        "requests": db.query(Request).count(),
        "open_cases": db.query(ExecutionCase).filter(
            ExecutionCase.status.notin_(["VERIFIED_OUTCOME","CANCELLED"])
        ).count(),
        "pending_followups": db.query(FollowupTask).filter(FollowupTask.status=="PENDING").count(),
        "active_integrations": db.query(IntegrationEndpoint).filter(IntegrationEndpoint.status=="ACTIVE").count(),
    }

def get_customer_case_cards(db):
    cases=db.query(ExecutionCase).order_by(ExecutionCase.id.desc()).limit(20).all()
    rows=[]
    for case in cases:
        req=db.query(Request).filter(Request.id==case.request_id).first()
        rows.append({"case":case,"req":req})
    return rows


def mobile_event_hash(customer_ref, source, package_name, title, body):
    raw="|".join([customer_ref or "",source or "",package_name or "",title or "",body or ""]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def ingest_mobile_source(db, customer_ref, source, body, package_name=None, title=None, locale=None):
    source = normalize_source_type(source)
    h=mobile_event_hash(customer_ref,source,package_name,title,body)
    existing=db.query(MobileSourceEvent).filter(
        MobileSourceEvent.customer_ref == customer_ref,
        MobileSourceEvent.event_hash==h,
    ).first()
    if existing:
        return existing, False
    locale = locale or resolve_locale(None)
    row=MobileSourceEvent(customer_ref=customer_ref, locale=locale.locale,
                          region=locale.region, currency=locale.currency,
                          source=source,package_name=package_name,title=title,
                          body=body,event_hash=h,status="RECEIVED")
    db.add(row); db.commit(); db.refresh(row)
    audit(db,"MOBILE_SOURCE_RECEIVED","MobileSourceEvent",row.id,f"{source}:{package_name or ''}")
    return row, True


@app.post("/api/customer/session")
def sync_customer_session(request: FastAPIRequest):
    """Exchange an anonymous bearer header for the same protected browser cookie."""
    # An invalid legacy cookie may be replaced, but a different *valid* cookie
    # is an existing customer namespace and must never be switched silently.
    customer_ref = require_customer_ref(request.headers.get(CUSTOMER_HEADER))
    cookie_raw = request.cookies.get(CUSTOMER_COOKIE)
    cookie_ref = canonical_customer_ref(cookie_raw)
    if cookie_ref and cookie_ref != customer_ref:
        raise HTTPException(403, "Conflicting customer capabilities")
    request.state.customer_ref = customer_ref
    response = JSONResponse({"ok": True})
    _set_customer_cookie(response, customer_ref, request)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/", response_class=HTMLResponse)
def home(request: FastAPIRequest, db: Session = Depends(get_db)):
    # The server never renders global customer data.  A valid UUID capability is
    # synced from the browser into a same-site cookie before private cards appear.
    customer_ref = customer_ref_from_request(request, required=False)
    if customer_ref:
        execution_cases = db.query(ExecutionCase).filter(
            ExecutionCase.customer_ref == customer_ref,
            ExecutionCase.status.notin_(["VERIFIED_OUTCOME", "CANCELLED"]),
        ).order_by(ExecutionCase.id.desc()).limit(5).all()
        external_cases = db.query(ExternalCase).filter(
            ExternalCase.customer_ref == customer_ref,
            ExternalCase.status.notin_(["VERIFIED_OUTCOME", "CANCELLED"]),
        ).order_by(ExternalCase.id.desc()).limit(5).all()
    else:
        execution_cases = []
        external_cases = []
    open_cases = []
    execution_request_ids = {c.request_id for c in execution_cases}
    active_requests = [] if not customer_ref else db.query(Request).filter(
        Request.customer_ref == customer_ref,
        Request.status.in_(["DISCOVERING", "DISCOVERY_UNAVAILABLE", "SUPPLY_FOUND_NO_CHANNEL", "WAITING_OFFERS", "OFFER_FOUND", "NO_REACHABLE_SUPPLY"])
    ).order_by(Request.id.desc()).limit(10).all()
    for req in active_requests:
        if req.id not in execution_request_ids:
            open_cases.append({"kind":"request","title":req.raw_text[:60], "status":req.status, "url":f"/requests/{req.id}"})
    for c in execution_cases:
        req = db.query(Request).filter(
            Request.id == c.request_id, Request.customer_ref == customer_ref,
        ).first()
        if req:
            open_cases.append({"kind":"execution","title": req.raw_text[:60], "status":c.status, "url":f"/cases/{c.id}"})
    for c in external_cases:
        open_cases.append({"kind":"external","title":c.title, "status":c.status, "url":f"/external/{c.id}"})
    memories = db.query(MemoryFact).filter(
        MemoryFact.customer_ref == customer_ref,
        MemoryFact.memory_type == "customer",
        MemoryFact.key == "verified_outcome"
    ).order_by(MemoryFact.id.desc()).limit(3).all() if customer_ref else []
    suggestions = db.query(DetectedTransaction).filter(
        DetectedTransaction.customer_ref == customer_ref,
        DetectedTransaction.status == "SUGGESTED"
    ).order_by(DetectedTransaction.id.desc()).limit(5).all() if customer_ref else []
    due_tasks = db.query(FollowupTask).join(
        ExternalCase, ExternalCase.id == FollowupTask.external_case_id,
    ).filter(
        FollowupTask.status == "PENDING",
        ExternalCase.customer_ref == customer_ref,
    ).order_by(FollowupTask.due_at.asc()).limit(5).all() if customer_ref else []
    followups = []
    for task in due_tasks:
        c = db.query(ExternalCase).filter(
            ExternalCase.id == task.external_case_id,
            ExternalCase.customer_ref == customer_ref,
        ).first()
        if c:
            followups.append({"task": task, "case": c})
    learned = preference_suggestions(db, customer_ref) if customer_ref else []
    response = templates.TemplateResponse("home.html", {
        "request": request, "open_cases": open_cases, "memories": memories,
        "suggestions": suggestions, "followups": followups, "learned": learned,
        "conversation_degraded": conversation_provider.degraded,
        "customer_ref_present": bool(customer_ref),
    })
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie, X-Customer-Ref"
    return response

@app.post("/requests", response_class=HTMLResponse)
async def create_request(text: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    customer_ref, locale = _customer_context(request)
    parsed = understand_request(text)
    parsed, applied_preferences = apply_confirmed_preferences(db, parsed, text, customer_ref)
    # Keep every entry point on the same honest discovery/outreach pipeline.
    # The helper distinguishes discovery, queued outreach and acknowledged
    # delivery instead of letting this legacy form invent WAITING_OFFERS.
    row = await start_request_from_conversation(db, text, customer_ref, locale)
    intent = understand_request_v2(text)
    log_event(db, row.id, "REQUEST_INTENT_CLASSIFIED",
              f"type={intent['intent_type']};merchant={intent.get('directed_merchant')};area={intent.get('area')};deadline={intent.get('deadline')}")
    attempts = db.query(ReachAttempt).filter(ReachAttempt.request_id == row.id).all()
    discovered_count = len(attempts)
    links = [
        (business.name, link.token)
        for link, business in db.query(MerchantLink, Business).join(
            Business, Business.id == MerchantLink.business_id,
        ).filter(MerchantLink.request_id == row.id).all()
    ]

    return templates.TemplateResponse("request_result.html",
        {"request": request, "row": row, "reachable_count": discovered_count, "links": links, "applied_preferences": applied_preferences})

@app.get("/r/{token}", response_class=HTMLResponse)
def merchant_page(token: str, request: FastAPIRequest, db: Session = Depends(get_db)):
    link = db.query(MerchantLink).filter(MerchantLink.token == token).first()
    if not link:
        raise HTTPException(404, "Invalid request link")

    if datetime.utcnow() > link.created_at + timedelta(hours=LINK_TTL_HOURS):
        link.status = "EXPIRED"; db.commit()
        log_event(db, link.request_id, "MERCHANT_LINK_EXPIRED", token[:8])
        return HTMLResponse("<html dir='rtl'><body><h2>انتهت صلاحية الطلب.</h2></body></html>", status_code=410)

    req = db.query(Request).filter(Request.id == link.request_id).first()
    if not req or not canonical_customer_ref(req.customer_ref):
        raise HTTPException(410, "Request is no longer available")
    biz = db.query(Business).filter(Business.id == link.business_id).first()
    existing = db.query(Offer).filter(Offer.request_id == req.id, Offer.business_id == biz.id).first()
    link.status = "OPENED"; db.commit()
    log_event(db, req.id, "MERCHANT_LINK_OPENED", biz.name)
    return templates.TemplateResponse("merchant.html",
        {"request": request, "req": req, "biz": biz, "token": token, "existing": existing})

@app.post("/r/{token}/offer", response_class=HTMLResponse)
def merchant_offer(token: str, price: float = Form(...), eta: str = Form(...),
                   notes: str = Form(""), db: Session = Depends(get_db)):
    link = db.query(MerchantLink).filter(MerchantLink.token == token).first()
    if not link:
        raise HTTPException(404, "Invalid request link")
    if datetime.utcnow() > link.created_at + timedelta(hours=LINK_TTL_HOURS):
        raise HTTPException(410, "Expired request link")

    req = db.query(Request).filter(Request.id == link.request_id).first()
    if not req or not canonical_customer_ref(req.customer_ref):
        raise HTTPException(410, "Request is no longer available")
    collecting_states = {
        "DISCOVERING", "SUPPLY_FOUND_NO_CHANNEL", "NO_REACHABLE_SUPPLY",
        "WAITING_OFFERS", "OFFER_FOUND",
    }
    if req.status not in collecting_states:
        raise HTTPException(409, "Request no longer accepts offers")
    existing = db.query(Offer).filter(Offer.request_id == req.id, Offer.business_id == link.business_id).first()
    is_valid, status = valid_offer(req, price)

    if existing:
        existing.price, existing.eta, existing.notes, existing.status = price, eta, notes, status
        offer = existing
        event = "OFFER_UPDATED"
    else:
        offer = Offer(request_id=req.id, business_id=link.business_id, price=price,
                      eta=eta, notes=notes, status=status)
        db.add(offer)
        event = "OFFER_RECEIVED"

    link.status = "RESPONDED"
    if is_valid:
        req.status = "OFFER_FOUND"
    db.commit()
    log_event(db, req.id, event, f"price={price};eta={eta};status={status}")
    msg = "تم إرسال عرضك بنجاح." if is_valid else "تم استلام العرض، لكنه خارج شروط الطلب الحالية."
    return HTMLResponse(f"<html dir='rtl'><body style='font-family:Arial;padding:40px'><h2>{msg}</h2></body></html>")


def business_performance(db, business_id):
    row = db.query(BusinessPerformance).filter(
        BusinessPerformance.business_id == business_id
    ).first()
    if not row:
        row = BusinessPerformance(business_id=business_id)
        db.add(row); db.commit(); db.refresh(row)
    return row

def offer_score(db, req, offer):
    """Deterministic transparent score for prototype ranking.
    Fit and outcome history matter; merchant payment never buys ranking.
    """
    score = 100.0
    reasons = []

    # Hard/near-hard request fit
    if req.budget is not None:
        if offer.price <= req.budget:
            score += 18
            reasons.append("داخل ميزانيتك")
        else:
            over = (offer.price - req.budget) / max(req.budget, 1)
            score -= min(45, over * 100)
            reasons.append("أعلى من الميزانية")

    perf = business_performance(db, offer.business_id)
    total = perf.verified_ok + perf.issues + perf.cancellations
    if total >= 3:
        success_rate = perf.verified_ok / max(total, 1)
        score += success_rate * 25
        reasons.append(f"سجل نتيجة ناجحة {round(success_rate*100)}%")
    else:
        score -= 3
        reasons.append("سجل التنفيذ لسه محدود")

    score -= perf.cancellations * 3
    score -= perf.issues * 2
    score -= perf.late * 1.5
    score -= perf.price_changes * 2

    # Confirmed preferences: only transparent, customer-approved rules.
    prefs = confirmed_preferences(db, req.customer_ref)
    notes = (offer.notes or "").lower()
    for p in prefs:
        if p.preference_key == "warranty" and "ضمان" in p.preference_value:
            if "ضمان رسمي" in notes or "ضمان الوكيل" in notes:
                score += 12
                reasons.append("مطابق لتفضيلك في الضمان")
    return round(score, 2), reasons

def ranked_offers_with_reasons(db, req, offers):
    ranked=[]
    for offer in offers:
        score,reasons=offer_score(db,req,offer)
        ranked.append((offer,score,reasons))
    ranked.sort(key=lambda x:(x[1], -x[0].price), reverse=True)
    return ranked

def update_business_outcome(db, case, ok):
    offer = db.query(Offer).filter(Offer.id == case.offer_id).first()
    if not offer:
        return
    perf = business_performance(db, offer.business_id)
    perf.completed += 1
    if ok:
        perf.verified_ok += 1
    else:
        perf.issues += 1
    perf.updated_at = datetime.utcnow()
    db.commit()

@app.get("/requests/{request_id}", response_class=HTMLResponse)
def customer_request_status(request_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    req = _request_for_customer_or_admin(db, request_id, request)
    offers = db.query(Offer).filter(Offer.request_id == req.id, Offer.status == "VALID").all()
    ranked = ranked_offers_with_reasons(db, req, offers)
    best = None; best_biz = None; best_score = None; best_reasons = []; fee = None
    if ranked:
        best, best_score, best_reasons = ranked[0]
        best_biz = db.query(Business).filter(Business.id == best.business_id).first()
        fee = service_fee_for_request(req, best)
    return templates.TemplateResponse("customer_status.html", {
        "request": request, "req": req, "best": best, "best_biz": best_biz,
        "best_score": best_score, "best_reasons": best_reasons, "service_fee": fee,
        "alternatives": max(0, len(ranked)-1)
    })

@app.post("/requests/{request_id}/select/{offer_id}", response_class=HTMLResponse)
def select_offer(request_id: int, offer_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    req = _request_for_customer_or_admin(db, request_id, request)
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.request_id == request_id, Offer.status == "VALID").first()
    if not offer:
        raise HTTPException(404, "Offer not found")
    case = db.query(ExecutionCase).filter(ExecutionCase.request_id == req.id).first()
    if case:
        if case.offer_id != offer.id:
            raise HTTPException(409, "A different offer is already selected")
        return templates.TemplateResponse("selected.html", {"request": request, "req": req, "offer": offer, "case": case})
    req.status = "SELECTED"
    case = ExecutionCase(customer_ref=req.customer_ref, request_id=req.id, offer_id=offer.id, status="AWAITING_PAYMENT",
                         payment_status="NOT_STARTED", outcome_status="OPEN", expected_at=offer.eta)
    db.add(case)
    try:
        db.flush()
        db.add(Event(request_id=req.id, event_type="OFFER_SELECTED", detail=f"offer_id={offer.id};price={offer.price}"))
        db.add(CaseEvent(case_id=case.id, event_type="CASE_OPENED", detail="AWAITING_PAYMENT"))
        db.commit()
    except IntegrityError:
        db.rollback()
        case = db.query(ExecutionCase).filter(ExecutionCase.request_id == req.id).first()
        if not case or case.offer_id != offer.id:
            raise HTTPException(409, "A different offer is already selected")
    db.refresh(case)
    return templates.TemplateResponse("selected.html", {"request": request, "req": req, "offer": offer, "case": case})

@app.get("/admin", response_class=HTMLResponse)
def admin(request: FastAPIRequest, db: Session = Depends(get_db)):
    _require_execution_admin(request)
    rows = db.query(Request).order_by(Request.id.desc()).all()
    data = []
    for r in rows:
        offers = db.query(Offer).filter(Offer.request_id == r.id).all()
        offers = rank_offers(r, offers)
        offer_rows = []
        for o in offers:
            biz = db.query(Business).filter(Business.id == o.business_id).first()
            offer_rows.append((o, biz))
        events = db.query(Event).filter(Event.request_id == r.id).order_by(Event.id.desc()).all()
        data.append((r, offer_rows, events))
    return templates.TemplateResponse("admin.html", {"request": request, "all_data": data})


@app.post("/cases/{case_id}/demo-payment", response_class=HTMLResponse)
def demo_payment(case_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    case = _case_for_customer_or_admin(db, case_id, request)
    req = db.query(Request).filter(Request.id == case.request_id).first()
    if case.payment_status != "AUTHORIZED_DEMO":
        if case.payment_status != "NOT_STARTED" or case.status != "AWAITING_PAYMENT":
            raise HTTPException(409, "Payment authorization is not valid in the current state")
        case.payment_status = "AUTHORIZED_DEMO"
        case.status = "CONFIRMED"
        req.status = "CONFIRMED"
        db.add(CaseEvent(case_id=case.id, event_type="PAYMENT_AUTHORIZED_DEMO", detail="No real money moved"))
        db.commit()
    issues = db.query(IssueRecord).filter(IssueRecord.execution_case_id == case.id).order_by(IssueRecord.id.desc()).all()
    offer = get_case_offer(db, case)
    amendment = get_pending_amendment(db, offer.id) if offer else None
    return templates.TemplateResponse("case.html", {"request": request, "case": case, "req": req, "issues": issues, "offer": offer, "amendment": amendment})

@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_status(case_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    case = _case_for_customer_or_admin(db, case_id, request)
    req = db.query(Request).filter(Request.id == case.request_id).first()
    issues = db.query(IssueRecord).filter(IssueRecord.execution_case_id == case.id).order_by(IssueRecord.id.desc()).all()
    offer = get_case_offer(db, case)
    amendment = get_pending_amendment(db, offer.id) if offer else None
    return templates.TemplateResponse("case.html", {"request": request, "case": case, "req": req, "issues": issues, "offer": offer, "amendment": amendment})

@app.post("/cases/{case_id}/event", response_class=HTMLResponse)
def case_event(case_id: int, event_type: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    if not is_execution_admin(request):
        raise HTTPException(403, "Execution administrator authentication required")
    case = db.query(ExecutionCase).filter(ExecutionCase.id == case_id).first()
    if not case:
        raise HTTPException(404, "Case not found")
    allowed = {
      "IN_PROGRESS":"IN_PROGRESS",
      "OUT_FOR_DELIVERY":"OUT_FOR_DELIVERY",
      "COMPLETED":"COMPLETED_PENDING_CONFIRMATION",
      "ISSUE":"ISSUE_OPEN"
    }
    if event_type not in allowed: raise HTTPException(400, "Invalid event")
    case.status = allowed[event_type]
    db.add(CaseEvent(case_id=case.id, event_type=event_type, detail=case.status))
    db.commit()
    req = db.query(Request).filter(Request.id == case.request_id).first()
    issues = db.query(IssueRecord).filter(IssueRecord.execution_case_id == case.id).order_by(IssueRecord.id.desc()).all()
    offer = get_case_offer(db, case)
    amendment = get_pending_amendment(db, offer.id) if offer else None
    return templates.TemplateResponse("case.html", {"request": request, "case": case, "req": req, "issues": issues, "offer": offer, "amendment": amendment})


def learn_from_verified_outcome(db, req):
    """Conservative learning: repeated explicit facts only.
    One transaction never becomes a personal preference.
    """
    text = req.raw_text.lower()
    candidates = []
    if "ضمان رسمي" in text or "ضمان الوكيل" in text:
        candidates.append(("warranty", "يفضل الضمان الرسمي"))
    if "مساء" in text or "بالليل" in text:
        candidates.append(("time_window", "يفضل التنفيذ مساءً"))
    if "الصبح" in text or "صباح" in text:
        candidates.append(("time_window", "يفضل التنفيذ صباحًا"))

    for key, value in candidates:
        row = db.query(LearnedPreference).filter(
            LearnedPreference.customer_ref == req.customer_ref,
            LearnedPreference.preference_key == key,
            LearnedPreference.preference_value == value
        ).first()
        if row:
            row.evidence_count += 1
        else:
            row = LearnedPreference(customer_ref=req.customer_ref, preference_key=key, preference_value=value,
                                    evidence_count=1, confidence=0.25, status="OBSERVED")
            db.add(row)
        db.flush()
        row.confidence = min(0.95, 0.25 + max(0, row.evidence_count-1)*0.25)
        if row.evidence_count >= 3:
            row.status = "SUGGESTIBLE"
    db.commit()

def preference_suggestions(db, customer_ref):
    if not customer_ref:
        return []
    return db.query(LearnedPreference).filter(
        LearnedPreference.customer_ref == customer_ref,
        LearnedPreference.status == "SUGGESTIBLE"
    ).order_by(LearnedPreference.confidence.desc()).all()


def confirmed_preferences(db, customer_ref):
    if not customer_ref:
        return []
    return db.query(LearnedPreference).filter(
        LearnedPreference.customer_ref == customer_ref,
        LearnedPreference.status == "CONFIRMED"
    ).all()

def apply_confirmed_preferences(db, parsed, raw_text, customer_ref):
    """Use only customer-confirmed rules, and never override an explicit instruction."""
    applied = []
    text = raw_text.lower()
    for p in confirmed_preferences(db, customer_ref):
        if p.preference_key == "warranty":
            # Explicit contrary/alternative wording wins over memory.
            if not any(x in text for x in ["من غير ضمان", "بدون ضمان", "مش مهم الضمان"]):
                parsed["confirmed_warranty_preference"] = p.preference_value
                applied.append(p.preference_value)
        elif p.preference_key == "time_window":
            # Don't inject a time preference if user already stated a time window.
            explicit_time = any(x in text for x in ["الصبح", "صباح", "مساء", "بالليل", "الظهر"])
            if not explicit_time:
                parsed["confirmed_time_preference"] = p.preference_value
                applied.append(p.preference_value)
    return parsed, applied

@app.post("/cases/{case_id}/outcome", response_class=HTMLResponse)
def confirm_outcome(case_id: int, result: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    case = _case_for_customer_or_admin(db, case_id, request)
    req = db.query(Request).filter(Request.id == case.request_id).first()
    if result not in {"ok", "issue"}:
        raise HTTPException(422, "Invalid outcome")
    already_applied = (
        result == "ok" and case.status == "VERIFIED_OUTCOME" and case.outcome_status == "VERIFIED"
    ) or (
        result == "issue" and case.status == "ISSUE_OPEN" and case.outcome_status == "ISSUE_OPEN"
    )
    if not already_applied and (case.status != "COMPLETED_PENDING_CONFIRMATION" or case.outcome_status != "OPEN"):
        raise HTTPException(409, "Outcome is not awaiting confirmation")
    if not already_applied and result == "ok":
        case.outcome_status="VERIFIED"
        case.status="VERIFIED_OUTCOME"
        req.status="VERIFIED_OUTCOME"
        # Commit helpers now see the terminal state too, so a retry cannot
        # increment outcome metrics a second time after a partial response.
        update_business_outcome(db, case, True)
        offer = get_case_offer(db, case)
        if offer and req: mark_capability_completed(db, offer, req)
        audit(db,"OUTCOME_VERIFIED","Request",req.id,req.raw_text)
        evt="OUTCOME_VERIFIED"
        learn_from_verified_outcome(db, req)
        existing_fact = db.query(MemoryFact).filter(
            MemoryFact.customer_ref == req.customer_ref,
            MemoryFact.source_type=="request", MemoryFact.source_id==req.id,
            MemoryFact.key=="verified_outcome",
        ).first()
        if not existing_fact:
            db.add(MemoryFact(customer_ref=req.customer_ref, memory_type="customer", key="verified_outcome",
                              value=req.raw_text, source_type="request", source_id=req.id, confidence=1.0))
    elif not already_applied:
        case.outcome_status="ISSUE_OPEN"
        case.status="ISSUE_OPEN"
        req.status="ISSUE_OPEN"
        update_business_outcome(db, case, False)
        evt="OUTCOME_ISSUE_OPENED"
    if not already_applied:
        db.add(CaseEvent(case_id=case.id,event_type=evt,detail=result))
        db.commit()
    issues = db.query(IssueRecord).filter(IssueRecord.execution_case_id == case.id).order_by(IssueRecord.id.desc()).all()
    offer = get_case_offer(db, case)
    amendment = get_pending_amendment(db, offer.id) if offer else None
    return templates.TemplateResponse("case.html", {"request": request, "case": case, "req": req, "issues": issues, "offer": offer, "amendment": amendment})


@app.get("/share", response_class=HTMLResponse)
def share_page(request: FastAPIRequest, text: str = ""):
    customer_ref_from_request(request)
    return templates.TemplateResponse("share.html", {"request": request, "shared_text": text})

@app.post("/share", response_class=HTMLResponse)
def create_external_case(source_text: str = Form(...), title: str = Form(""), expected_at: str = Form(""),
                         request: FastAPIRequest = None, db: Session = Depends(get_db)):
    customer_ref, locale = _customer_context(request)
    event_hash = mobile_event_hash(customer_ref, "MANUAL_SHARE", None, title, source_text)
    _manual_event_consent(db, customer_ref, event_hash, "MANUAL_SHARE")
    clean_title = title.strip() or source_text[:60]
    case = ExternalCase(customer_ref=customer_ref, locale=locale.locale, region=locale.region,
                        currency=locale.currency, source_text=source_text, title=clean_title, expected_at=expected_at or None,
                        status="FOLLOWING", outcome_status="OPEN")
    db.add(case); db.commit(); db.refresh(case)
    return templates.TemplateResponse("external_case.html", {"request": request, "case": case})

@app.get("/external/{case_id}", response_class=HTMLResponse)
def external_case(case_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    case = _owned_external_case(db, case_id, customer_ref)
    return templates.TemplateResponse("external_case.html", {"request": request, "case": case})

@app.post("/external/{case_id}/outcome", response_class=HTMLResponse)
def external_outcome(case_id: int, result: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    case = _owned_external_case(db, case_id, customer_ref)
    if result == "ok":
        case.status="VERIFIED_OUTCOME"; case.outcome_status="VERIFIED"
    else:
        case.status="ISSUE_OPEN"; case.outcome_status="ISSUE_OPEN"
    db.commit()
    return templates.TemplateResponse("external_case.html", {"request": request, "case": case})

@app.get("/memory", response_class=HTMLResponse)
def memory_page(request: FastAPIRequest, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    facts = db.query(MemoryFact).filter(
        MemoryFact.customer_ref == customer_ref,
    ).order_by(MemoryFact.id.desc()).all()
    return templates.TemplateResponse("memory.html", {"request": request, "facts": facts})


@app.post("/memory/{memory_id}/repeat", response_class=HTMLResponse)
async def repeat_memory(memory_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    customer_ref, locale = _customer_context(request)
    fact = db.query(MemoryFact).filter(MemoryFact.id == memory_id,
                                      MemoryFact.customer_ref == customer_ref,
                                      MemoryFact.memory_type == "customer",
                                      MemoryFact.key == "verified_outcome").first()
    if not fact:
        raise HTTPException(404, "Memory not found")
    parsed = understand_request(fact.value)
    _, applied_preferences = apply_confirmed_preferences(db, parsed, fact.value, customer_ref)
    row = await start_request_from_conversation(db, fact.value, customer_ref, locale)
    log_event(db, row.id, "REQUEST_CREATED_FROM_MEMORY", f"memory_id={fact.id}")
    attempts = db.query(ReachAttempt).filter(ReachAttempt.request_id == row.id).all()
    reachable = len(attempts)
    links = [
        (business.name, link.token)
        for link, business in db.query(MerchantLink, Business).join(
            Business, Business.id == MerchantLink.business_id,
        ).filter(MerchantLink.request_id == row.id).all()
    ]
    return templates.TemplateResponse("request_result.html",
        {"request":request,"row":row,"reachable_count":reachable,"links":links,"applied_preferences":applied_preferences})


def detect_followup_candidate(text: str):
    """Prototype deterministic detector.
    Returns structured follow-up only when there is a useful future/unfinished state.
    It deliberately avoids treating every transaction as follow-up-worthy.
    """
    raw = " ".join(text.split())
    t = raw.lower()

    # Completed/no-action messages should normally not create noise.
    completed = ["تم التسليم بنجاح", "تم الاستلام بنجاح", "تمت العملية بنجاح",
                 "تم إلغاء الطلب", "تم الغاء الطلب"]
    if any(x in t for x in completed):
        return None

    patterns = [
        {
            "keys": ["خرج للتوصيل", "قيد التوصيل", "سيتم التوصيل", "موعد التسليم", "شحنة", "الشحنة"],
            "type": "طلب / شحنة",
            "action": "متابعة وصول الطلب والتدخل لو الموعد عدى بدون تسليم",
            "watch_for": "DELIVERY",
        },
        {
            "keys": ["صيانة", "الفني", "زيارة فني"],
            "type": "صيانة",
            "action": "متابعة حضور الفني ثم التأكد إن المشكلة اتحلت",
            "watch_for": "SERVICE_OUTCOME",
        },
        {
            "keys": ["تم الحجز", "تأكيد الحجز", "موعدك", "موعد "],
            "type": "حجز / موعد",
            "action": "متابعة الموعد وأي تغيير أو إلغاء ثم تأكيد النتيجة",
            "watch_for": "APPOINTMENT",
        },
        {
            "keys": ["تم التحويل", "تحويل مبلغ", "حوالة"],
            "type": "تحويل",
            "action": "التأكد من وصول التحويل للطرف المقصود",
            "watch_for": "RECEIPT_CONFIRMATION",
        },
        {
            "keys": ["فاتورة", "مستحق", "استحقاق", "يرجى السداد"],
            "type": "فاتورة",
            "action": "متابعة موعد الاستحقاق وتأكيد السداد",
            "watch_for": "DUE_DATE",
        },
        {
            "keys": ["تم تأكيد طلب", "تم استلام طلبك", "رقم الطلب"],
            "type": "طلب شراء",
            "action": "متابعة الطلب لحد التسليم وتأكيد النتيجة",
            "watch_for": "ORDER_OUTCOME",
        },
    ]

    found=None
    for p in patterns:
        if any(k in t for k in p["keys"]):
            found=p; break
    if not found:
        return None

    # Extract lightweight dates/times/order refs from Arabic/English transactional text.
    date_patterns = [
        r'\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b',
        r'\b(?:الأحد|الاحد|الإثنين|الاثنين|الثلاثاء|الأربعاء|الاربعاء|الخميس|الجمعة|السبت)\b',
        r'\b(?:اليوم|غدا|غداً|بكرة)\b',
    ]
    time_patterns = [
        r'\b\d{1,2}:\d{2}\s*(?:ص|م|am|pm)?\b',
        r'\bالساعة\s+\d{1,2}(?::\d{2})?\s*(?:ص|م)?\b',
    ]
    ref_patterns = [
        r'(?:رقم الطلب|order\s*#?|طلب رقم)\s*[:#-]?\s*([A-Za-z0-9-]+)'
    ]
    expected = next((m.group(0) for p in date_patterns for m in [re.search(p, raw, re.I)] if m), None)
    time_value = next((m.group(0) for p in time_patterns for m in [re.search(p, raw, re.I)] if m), None)
    reference = next((m.group(1) for p in ref_patterns for m in [re.search(p, raw, re.I)] if m), None)

    if expected and time_value:
        expected = expected + " " + time_value
    elif not expected:
        expected = time_value

    return {
        "type": found["type"],
        "action": found["action"],
        "watch_for": found["watch_for"],
        "expected_at": expected,
        "reference": reference,
    }


def transaction_event_type(text: str):
    t = text.lower()
    if any(x in t for x in ["تم التسليم", "تم الاستلام"]): return "DELIVERED"
    if any(x in t for x in ["خرج للتوصيل", "قيد التوصيل"]): return "OUT_FOR_DELIVERY"
    if any(x in t for x in ["تم الشحن", "تم إرسال الشحنة"]): return "SHIPPED"
    if any(x in t for x in ["تم تأكيد طلب", "تم استلام طلبك"]): return "CONFIRMED"
    if any(x in t for x in ["تم إلغاء", "تم الغاء"]): return "CANCELLED"
    if any(x in t for x in ["الفني في الطريق", "الفني متجه"]): return "TECHNICIAN_EN_ROUTE"
    if any(x in t for x in ["تمت الصيانة", "تم الإصلاح", "تم الاصلاح"]): return "SERVICE_COMPLETED"
    if any(x in t for x in ["تم التحويل"]): return "TRANSFER_SENT"
    if any(x in t for x in ["تم استلام التحويل", "وصل التحويل"]): return "TRANSFER_RECEIVED"
    return "UPDATE"

def find_existing_transaction(db, customer_ref, detected, raw_text):
    """Conservative prototype matching:
    1) exact external reference wins;
    2) otherwise only match latest same transaction type when text overlaps enough.
    """
    ref = detected.get("reference")
    if ref:
        row = db.query(DetectedTransaction).filter(
            DetectedTransaction.customer_ref == customer_ref,
            DetectedTransaction.source_ref == ref
        ).order_by(DetectedTransaction.id.desc()).first()
        if row:
            return row

    candidates = db.query(DetectedTransaction).filter(
        DetectedTransaction.customer_ref == customer_ref,
        DetectedTransaction.transaction_type == detected["type"],
        DetectedTransaction.status.in_(["SUGGESTED", "ACCEPTED"])
    ).order_by(DetectedTransaction.id.desc()).limit(5).all()

    words = {w for w in re.findall(r'[\w\u0600-\u06FF]+', raw_text.lower()) if len(w) > 3}
    for c in candidates:
        old_words = {w for w in re.findall(r'[\w\u0600-\u06FF]+', c.raw_text.lower()) if len(w) > 3}
        overlap = len(words & old_words)
        if overlap >= 3:
            return c
    return None


def ensure_default_followup_rules(db):
    if db.query(FollowupRule).count() > 0:
        return
    defaults = [
        ("طلب / شحنة", "OUT_FOR_DELIVERY", "لو مفيش تسليم بعد الوقت المتوقع، اعتبرها متابعة متأخرة", 180),
        ("طلب / شحنة", "DELIVERED", "اسأل العميل هل الاستلام تم كويس وهل فيه مشكلة", 30),
        ("طلب شراء", "DELIVERED", "أكد النتيجة مع العميل بدل إغلاق الطلب تلقائيًا", 30),
        ("صيانة", "TECHNICIAN_EN_ROUTE", "تابع حضور الفني", 120),
        ("صيانة", "SERVICE_COMPLETED", "اسأل هل المشكلة اتحلت فعلًا", 60),
        ("تحويل", "TRANSFER_SENT", "تابع تأكيد وصول التحويل", 120),
        ("حجز / موعد", "CONFIRMED", "تابع الموعد وأي تغيير", 1440),
    ]
    for typ, evt, action, delay in defaults:
        db.add(FollowupRule(transaction_type=typ, trigger_event=evt, action=action,
                            delay_minutes=delay, enabled=True))
    db.commit()

def schedule_followup_for_case(db, tx, case, event_type):
    ensure_default_followup_rules(db)
    rule = db.query(FollowupRule).filter(
        FollowupRule.transaction_type == tx.transaction_type,
        FollowupRule.trigger_event == event_type,
        FollowupRule.enabled == True
    ).first()
    if not rule:
        return None
    existing = db.query(FollowupTask).filter(
        FollowupTask.external_case_id == case.id,
        FollowupTask.trigger_event == event_type,
        FollowupTask.status == "PENDING"
    ).first()
    if existing:
        return existing
    task = FollowupTask(external_case_id=case.id, trigger_event=event_type,
                        action=rule.action,
                        due_at=datetime.utcnow() + timedelta(minutes=rule.delay_minutes),
                        status="PENDING")
    db.add(task); db.commit(); db.refresh(task)
    return task

def apply_transaction_update(db, tx, raw_text):
    evt = transaction_event_type(raw_text)
    db.add(TransactionEvent(detected_transaction_id=tx.id, event_type=evt, raw_text=raw_text))

    # Update existing accepted external case when possible.
    if tx.status == "ACCEPTED":
        cases = db.query(ExternalCase).filter(
            ExternalCase.customer_ref == tx.customer_ref,
            ExternalCase.source_text == tx.raw_text
        ).order_by(ExternalCase.id.desc()).all()
        case = cases[0] if cases else None
        if case:
            if evt in ["OUT_FOR_DELIVERY", "SHIPPED", "TECHNICIAN_EN_ROUTE"]:
                case.status = evt
            elif evt in ["DELIVERED", "SERVICE_COMPLETED", "TRANSFER_RECEIVED"]:
                case.status = "COMPLETED_PENDING_CONFIRMATION"
            elif evt == "CANCELLED":
                case.status = "CANCELLED"
                case.outcome_status = "CANCELLED"
            schedule_followup_for_case(db, tx, case, evt)
    db.commit()
    return evt

@app.get("/inbox-scan", response_class=HTMLResponse)
def inbox_scan_page(request: FastAPIRequest):
    customer_ref_from_request(request)
    return templates.TemplateResponse("scan.html", {"request": request})

@app.post("/inbox-scan", response_class=HTMLResponse)
def simulate_scan(source_text: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    customer_ref, locale = _customer_context(request)
    event_hash = mobile_event_hash(customer_ref, "MANUAL_SCAN", None, None, source_text)
    _manual_event_consent(db, customer_ref, event_hash, "MANUAL_SCAN")
    detected = detect_followup_candidate(source_text)

    # Completed messages may still be updates to an existing case.
    if not detected:
        # Try extracting a reference from completion/update text.
        ref_match = re.search(r'(?:رقم الطلب|order\s*#?|طلب رقم)\s*[:#-]?\s*([A-Za-z0-9-]+)', source_text, re.I)
        existing = None
        if ref_match:
            existing = db.query(DetectedTransaction).filter(
                DetectedTransaction.customer_ref == customer_ref,
                DetectedTransaction.source_ref == ref_match.group(1)
            ).order_by(DetectedTransaction.id.desc()).first()
        if existing:
            evt = apply_transaction_update(db, existing, source_text)
            return templates.TemplateResponse("scan_result.html",
                {"request": request, "detected": None, "updated": existing, "event_type": evt})
        return templates.TemplateResponse("scan_result.html",
            {"request": request, "detected": None, "updated": None, "event_type": None})

    existing = find_existing_transaction(db, customer_ref, detected, source_text)
    if existing:
        evt = apply_transaction_update(db, existing, source_text)
        if detected.get("expected_at"):
            existing.expected_at = detected["expected_at"]
            db.commit()
        return templates.TemplateResponse("scan_result.html",
            {"request": request, "detected": None, "updated": existing, "event_type": evt})

    created = DetectedTransaction(
        customer_ref=customer_ref, locale=locale.locale, region=locale.region,
        currency=locale.currency, source="prototype_input", raw_text=source_text,
        title=detected["type"], transaction_type=detected["type"],
        expected_at=detected.get("expected_at"),
        suggested_action=detected["action"],
        source_ref=detected.get("reference"),
        status="SUGGESTED"
    )
    db.add(created); db.commit(); db.refresh(created)
    db.add(TransactionEvent(detected_transaction_id=created.id,
                            event_type=transaction_event_type(source_text),
                            raw_text=source_text))
    db.commit()
    return templates.TemplateResponse("scan_result.html",
        {"request": request, "detected": created, "updated": None, "event_type": None})

@app.post("/suggestions/{suggestion_id}/accept", response_class=HTMLResponse)
def accept_suggestion(suggestion_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    s = db.query(DetectedTransaction).filter(
        DetectedTransaction.id == suggestion_id,
        DetectedTransaction.customer_ref == customer_ref,
    ).first()
    if not s: raise HTTPException(404, "Suggestion not found")
    if s.status != "SUGGESTED": raise HTTPException(400, "Suggestion already handled")
    case = ExternalCase(customer_ref=customer_ref, locale=s.locale, region=s.region,
                        currency=s.currency, source_text=s.raw_text, title=s.title,
                        expected_at=s.expected_at, status="FOLLOWING", outcome_status="OPEN")
    db.add(case)
    s.status="ACCEPTED"
    db.commit(); db.refresh(case)
    return templates.TemplateResponse("external_case.html", {"request": request, "case": case})

@app.post("/suggestions/{suggestion_id}/dismiss")
def dismiss_suggestion(suggestion_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    s = db.query(DetectedTransaction).filter(
        DetectedTransaction.id == suggestion_id,
        DetectedTransaction.customer_ref == customer_ref,
    ).first()
    if not s: raise HTTPException(404, "Suggestion not found")
    s.status="DISMISSED"; db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/followups/{task_id}/done")
def followup_done(task_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    task = db.query(FollowupTask).join(
        ExternalCase, ExternalCase.id == FollowupTask.external_case_id,
    ).filter(
        FollowupTask.id == task_id,
        ExternalCase.customer_ref == customer_ref,
    ).first()
    if not task: raise HTTPException(404, "Follow-up not found")
    task.status="DONE"; db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/preferences/{pref_id}/confirm")
def confirm_preference(pref_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    p = db.query(LearnedPreference).filter(
        LearnedPreference.id == pref_id,
        LearnedPreference.customer_ref == customer_ref,
    ).first()
    if not p: raise HTTPException(404, "Preference not found")
    p.status="CONFIRMED"; p.confidence=1.0; db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/preferences/{pref_id}/reject")
def reject_preference(pref_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    p = db.query(LearnedPreference).filter(
        LearnedPreference.id == pref_id,
        LearnedPreference.customer_ref == customer_ref,
    ).first()
    if not p: raise HTTPException(404, "Preference not found")
    db.delete(p); db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/cases/{case_id}/issue", response_class=HTMLResponse)
def open_issue(case_id: int, issue_text: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    case = _case_for_customer_or_admin(db, case_id, request)
    issue = IssueRecord(execution_case_id=case.id, issue_text=issue_text, status="OPEN")
    db.add(issue)
    case.status="ISSUE_OPEN"; case.outcome_status="ISSUE_OPEN"
    db.add(CaseEvent(case_id=case.id,event_type="ISSUE_OPENED",detail=issue_text))
    db.commit()
    req=db.query(Request).filter(Request.id==case.request_id).first()
    issues=db.query(IssueRecord).filter(IssueRecord.execution_case_id==case.id).order_by(IssueRecord.id.desc()).all()
    offer=get_case_offer(db,case)
    amendment=get_pending_amendment(db, offer.id) if offer else None
    return templates.TemplateResponse("case.html", {"request":request,"case":case,"req":req,"issues":issues,"offer":offer,"amendment":amendment})

@app.post("/issues/{issue_id}/resolve", response_class=HTMLResponse)
def resolve_issue(issue_id: int, resolution_text: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    if not is_execution_admin(request):
        raise HTTPException(403, "Execution administrator authentication required")
    issue=db.query(IssueRecord).filter(IssueRecord.id==issue_id).first()
    if not issue: raise HTTPException(404,"Issue not found")
    issue.status="RESOLVED_PENDING_CONFIRMATION"; issue.resolution_text=resolution_text
    case=db.query(ExecutionCase).filter(ExecutionCase.id==issue.execution_case_id).first()
    case.status="RESOLVED_PENDING_CONFIRMATION"
    db.add(CaseEvent(case_id=case.id,event_type="ISSUE_RESOLUTION_PROPOSED",detail=resolution_text))
    db.commit()
    req=db.query(Request).filter(Request.id==case.request_id).first()
    issues=db.query(IssueRecord).filter(IssueRecord.execution_case_id==case.id).order_by(IssueRecord.id.desc()).all()
    offer=get_case_offer(db,case); amendment=get_pending_amendment(db,offer.id) if offer else None
    return templates.TemplateResponse("case.html", {"request":request,"case":case,"req":req,"issues":issues,"offer":offer,"amendment":amendment})

@app.post("/issues/{issue_id}/confirm", response_class=HTMLResponse)
def confirm_issue_resolution(issue_id: int, result: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    issue=db.query(IssueRecord).join(
        ExecutionCase, ExecutionCase.id == IssueRecord.execution_case_id,
    ).filter(
        IssueRecord.id==issue_id,
        ExecutionCase.customer_ref == customer_ref,
    ).first()
    if not issue: raise HTTPException(404,"Issue not found")
    case=db.query(ExecutionCase).filter(ExecutionCase.id==issue.execution_case_id).first()
    _owned_execution_case(db, case.id, customer_ref)
    if result not in {"ok", "no"}:
        raise HTTPException(422, "Invalid resolution decision")
    already_applied = (
        result == "ok" and issue.status == "VERIFIED_RESOLVED" and case.status == "VERIFIED_OUTCOME"
    ) or (
        result == "no" and issue.status == "OPEN" and case.status == "ISSUE_OPEN"
    )
    if not already_applied and (
        issue.status != "RESOLVED_PENDING_CONFIRMATION" or case.status != "RESOLVED_PENDING_CONFIRMATION"
    ):
        raise HTTPException(409, "Issue resolution is not awaiting confirmation")
    if not already_applied and result=="ok":
        issue.status="VERIFIED_RESOLVED"
        case.status="VERIFIED_OUTCOME"; case.outcome_status="VERIFIED"
        req=db.query(Request).filter(Request.id==case.request_id).first()
        req.status="VERIFIED_OUTCOME"
        update_business_outcome(db,case,True)
        db.add(CaseEvent(case_id=case.id,event_type="ISSUE_RESOLUTION_VERIFIED",detail=issue.resolution_text))
    elif not already_applied:
        issue.status="OPEN"; case.status="ISSUE_OPEN"; case.outcome_status="ISSUE_OPEN"
        db.add(CaseEvent(case_id=case.id,event_type="ISSUE_REOPENED",detail="customer_not_satisfied"))
    db.commit()
    req=db.query(Request).filter(Request.id==case.request_id).first()
    issues=db.query(IssueRecord).filter(IssueRecord.execution_case_id==case.id).order_by(IssueRecord.id.desc()).all()
    offer=get_case_offer(db,case); amendment=get_pending_amendment(db,offer.id) if offer else None
    return templates.TemplateResponse("case.html", {"request":request,"case":case,"req":req,"issues":issues,"offer":offer,"amendment":amendment})

@app.post("/offers/{offer_id}/amend", response_class=HTMLResponse)
def propose_amendment(offer_id: int, new_price: float = Form(None), new_eta: str = Form(""),
                      reason: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    if not is_execution_admin(request):
        raise HTTPException(403, "Execution administrator authentication required")
    offer=db.query(Offer).filter(Offer.id==offer_id).first()
    if not offer: raise HTTPException(404,"Offer not found")
    case=db.query(ExecutionCase).filter(ExecutionCase.offer_id==offer.id).first()
    if not case: raise HTTPException(400,"Offer not selected")
    old=get_pending_amendment(db,offer.id)
    if old: raise HTTPException(400,"Pending amendment already exists")
    a=OfferAmendment(offer_id=offer.id,new_price=new_price,new_eta=new_eta or None,reason=reason,status="PENDING")
    db.add(a); db.add(CaseEvent(case_id=case.id,event_type="OFFER_AMENDMENT_PROPOSED",detail=reason)); db.commit()
    return RedirectResponse(url=f"/cases/{case.id}",status_code=303)

@app.post("/amendments/{amendment_id}/decision")
def amendment_decision(amendment_id: int, decision: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    a=db.query(OfferAmendment).join(Offer, Offer.id == OfferAmendment.offer_id).join(
        ExecutionCase, ExecutionCase.offer_id == Offer.id,
    ).filter(
        OfferAmendment.id==amendment_id,
        ExecutionCase.customer_ref == customer_ref,
    ).first()
    if not a: raise HTTPException(404,"Amendment not found")
    offer=db.query(Offer).filter(Offer.id==a.offer_id).first()
    case=db.query(ExecutionCase).filter(ExecutionCase.offer_id==offer.id).first()
    _owned_execution_case(db, case.id, customer_ref)
    if decision not in {"approve", "reject"}:
        raise HTTPException(422, "Invalid amendment decision")
    wanted_status = "APPROVED" if decision == "approve" else "REJECTED"
    if a.status == wanted_status:
        return RedirectResponse(url=f"/cases/{case.id}",status_code=303)
    if a.status != "PENDING":
        raise HTTPException(409, "A different amendment decision was already recorded")
    if decision=="approve":
        if a.new_price is not None: offer.price=a.new_price
        if a.new_eta: offer.eta=a.new_eta
        a.status="APPROVED"
        audit(db,"OFFER_AMENDMENT_APPROVED","OfferAmendment",a.id,a.reason)
        perf=business_performance(db,offer.business_id); perf.price_changes += 1
        db.add(CaseEvent(case_id=case.id,event_type="OFFER_AMENDMENT_APPROVED",detail=a.reason))
    else:
        a.status="REJECTED"
        audit(db,"OFFER_AMENDMENT_REJECTED","OfferAmendment",a.id,a.reason)
        db.add(CaseEvent(case_id=case.id,event_type="OFFER_AMENDMENT_REJECTED",detail=a.reason))
    db.commit()
    return RedirectResponse(url=f"/cases/{case.id}",status_code=303)


@app.get("/supply", response_class=HTMLResponse)
def supply_dashboard(request: FastAPIRequest, db: Session=Depends(get_db)):
    _require_execution_admin(request)
    attempts=db.query(ReachAttempt).order_by(ReachAttempt.id.desc()).limit(100).all()
    rows=[]
    for a in attempts:
        biz=db.query(Business).filter(Business.id==a.business_id).first()
        req=db.query(Request).filter(Request.id==a.request_id).first()
        rows.append({"attempt":a,"biz":biz,"req":req})
    return templates.TemplateResponse("supply.html",{"request":request,"rows":rows})

@app.post("/businesses/{business_id}/activate")
def activate_business(business_id:int, channel:str=Form(...), endpoint:str=Form(...),
                      db:Session=Depends(get_db)):
    biz=db.query(Business).filter(Business.id==business_id).first()
    if not biz: raise HTTPException(404,"Business not found")
    row=get_activation(db,business_id)
    if not row:
        row=BusinessActivation(business_id=business_id)
        db.add(row)
    row.status="DIRECT"; row.preferred_channel=channel; row.endpoint=endpoint
    row.consent_at=datetime.utcnow()
    db.commit()
    return RedirectResponse(url="/supply",status_code=303)

@app.post("/requests/{request_id}/route")
def route_existing_request(request_id:int, request:FastAPIRequest, db:Session=Depends(get_db)):
    _require_execution_admin(request)
    req=db.query(Request).filter(Request.id==request_id).first()
    if not req: raise HTTPException(404,"Request not found")
    if not canonical_customer_ref(req.customer_ref):
        raise HTTPException(409, "Request owner is unavailable")
    businesses=db.query(Business).all()
    for biz in businesses:
        exists=db.query(ReachAttempt).filter(
            ReachAttempt.request_id==req.id,ReachAttempt.business_id==biz.id
        ).first()
        if not exists: route_request_to_business(db,req,biz)
    return RedirectResponse(url="/supply",status_code=303)

@app.get("/businesses/{business_id}/capability", response_class=HTMLResponse)
def business_capability(business_id:int, request:FastAPIRequest, db:Session=Depends(get_db)):
    _require_execution_admin(request)
    biz=db.query(Business).filter(Business.id==business_id).first()
    if not biz: raise HTTPException(404,"Business not found")
    caps=db.query(CapabilitySignal).filter(CapabilitySignal.business_id==business_id).all()
    perf=business_performance(db,business_id)
    activation=get_activation(db,business_id)
    return templates.TemplateResponse("capability.html",{
        "request":request,"biz":biz,"caps":caps,"perf":perf,"activation":activation
    })


@app.post("/integrations/business/{business_id}")
def add_integration(business_id:int, kind:str=Form(...), endpoint:str=Form(...),
                    consent_basis:str=Form(""), db:Session=Depends(get_db)):
    biz=db.query(Business).filter(Business.id==business_id).first()
    if not biz: raise HTTPException(404,"Business not found")
    ep=IntegrationEndpoint(business_id=business_id,kind=kind,endpoint=endpoint,
                           status="ACTIVE",consent_basis=consent_basis or None)
    db.add(ep); db.commit()
    return RedirectResponse(url="/supply",status_code=303)

@app.post("/reach/{attempt_id}/deliver")
def deliver_attempt(attempt_id:int, db:Session=Depends(get_db)):
    a=db.query(ReachAttempt).filter(ReachAttempt.id==attempt_id).first()
    if not a: raise HTTPException(404,"Reach attempt not found")
    d=deliver_request(db,a)
    if d.status=="QUEUED":
        a.status="SENT"; a.reason=f"queued via {d.transport}"
    else:
        a.status="FAILED"; a.reason=d.error
    db.commit()
    return RedirectResponse(url="/supply",status_code=303)

@app.get("/integrations", response_class=HTMLResponse)
def integrations_page(request:FastAPIRequest, db:Session=Depends(get_db)):
    _require_execution_admin(request)
    eps=db.query(IntegrationEndpoint).order_by(IntegrationEndpoint.id.desc()).all()
    pays=db.query(PaymentIntent).order_by(PaymentIntent.id.desc()).limit(50).all()
    return templates.TemplateResponse("integrations.html",{"request":request,"eps":eps,"pays":pays})


@app.get("/ops", response_class=HTMLResponse)
def ops_dashboard(request:FastAPIRequest, db:Session=Depends(get_db)):
    _require_execution_admin(request)
    health=system_health(db)
    requests_=db.query(Request).order_by(Request.id.desc()).limit(25).all()
    cases=get_customer_case_cards(db)
    followups=db.query(FollowupTask).filter(FollowupTask.status=="PENDING").order_by(FollowupTask.due_at.asc()).limit(25).all()
    audits=db.query(AuditRecord).order_by(AuditRecord.id.desc()).limit(40).all()
    return templates.TemplateResponse("ops.html",{
        "request":request,"health":health,"requests_":requests_,"cases":cases,
        "followups":followups,"audits":audits
    })

@app.get("/health")
def health(db:Session=Depends(get_db)):
    return system_health(db)

@app.post("/consent")
def create_consent(request: FastAPIRequest, source_type:str=Form(...),
                   decision:str=Form("grant"), db:Session=Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    source_type = normalize_source_type(source_type)
    if source_type not in CONNECTED_SOURCE_TYPES:
        raise HTTPException(422, "Unsupported connected source type")
    if decision not in {"grant", "revoke"}:
        raise HTTPException(422, "Consent decision must be grant or revoke")
    status = "GRANTED" if decision == "grant" else "REVOKED"
    consent_type = source_consent_type(source_type)
    row=ConsentRecord(subject_type="CUSTOMER",subject_ref=customer_ref,
                      consent_type=consent_type,status=status,source="USER_ACTION")
    db.add(row); db.commit()
    audit(db,f"CONSENT_{status}","ConsentRecord",row.id,consent_type,
          actor=f"CUSTOMER:{customer_ref[:8]}")
    return RedirectResponse(url="/privacy",status_code=303)

@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request:FastAPIRequest, db:Session=Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    enabled = {
        source: _active_source_consent(db, customer_ref, source)
        for source in sorted(CONNECTED_SOURCE_TYPES)
    }
    return templates.TemplateResponse("privacy.html",{"request":request,"enabled_sources":enabled})


@app.exception_handler(404)
async def not_found_handler(request, exc):
    return templates.TemplateResponse("error.html",{"request":request,"title":"مش لاقي الصفحة","message":"الرابط ده مش موجود أو انتهى."},status_code=404)

@app.exception_handler(500)
async def server_error_handler(request, exc):
    return templates.TemplateResponse("error.html",{"request":request,"title":"حصلت مشكلة","message":"الطلب محفوظ قدر الإمكان. جرّب تاني أو راجع لوحة التشغيل."},status_code=500)


@app.post("/api/mobile/source")
def mobile_source(request:FastAPIRequest, source:str=Form(...), body:str=Form(...),
                  package_name:str=Form(""), title:str=Form(""),
                  db:Session=Depends(get_db)):
    customer_ref, locale = _customer_context(request)
    source = normalize_source_type(source)
    if source not in CONNECTED_SOURCE_TYPES:
        raise HTTPException(422, "Unsupported connected source type")
    if not _active_source_consent(db, customer_ref, source):
        raise HTTPException(403, "Explicit consent for this source is required")
    row,created=ingest_mobile_source(
        db,customer_ref,source,body,package_name or None,title or None,locale,
    )
    # Feed the same conservative transaction detector already used by prototype flows.
    # We deliberately return a suggestion candidate instead of auto-following.
    candidate=detect_followup_candidate(body)
    return {
        "ok": True, "created": created, "source_event_id": row.id,
        "followup_candidate": candidate
    }

@app.post("/api/mobile/share")
def mobile_share(request:FastAPIRequest, text:str=Form(...), db:Session=Depends(get_db)):
    customer_ref, locale = _customer_context(request)
    event_hash = mobile_event_hash(customer_ref, "SHARE", None, None, text)
    _manual_event_consent(db, customer_ref, event_hash, "SHARE")
    row,created=ingest_mobile_source(db,customer_ref,"SHARE",text,locale=locale)
    return {"ok":True,"created":created,"source_event_id":row.id,"open_url":"/share"}

@app.get("/mobile/setup", response_class=HTMLResponse)
def mobile_setup(request:FastAPIRequest):
    return templates.TemplateResponse("mobile_setup.html",{"request":request})


class ChatTurnInput(BaseModel):
    message: str = Field(min_length=1, max_length=6000)
    thread_id: str | None = None
    # Compatibility for a previously deployed browser. New web clients use the
    # protected cookie and native clients use X-Customer-Ref.
    customer_ref: str | None = Field(default=None, max_length=120)
    locale: str = Field(default="ar-EG", max_length=20)
    # UUID generated once per send and reused by the client on network retry.
    # Optional so already-deployed/native clients remain compatible.
    client_turn_id: uuid.UUID | None = None


def linked_case_context(db: Session, thread_id: str, customer_ref: str | None = None):
    thread = db.query(ConversationThread).filter(ConversationThread.id == thread_id).first()
    if not thread:
        return []
    owner = customer_ref or canonical_customer_ref(thread.customer_ref)
    if not owner or thread.customer_ref != owner:
        return []
    links = db.query(ConversationCaseLink).filter(
        ConversationCaseLink.thread_id == thread_id
    ).order_by(ConversationCaseLink.id.desc()).all()
    context = []
    for link in links:
        item = {"id": link.case_id, "type": link.case_type, "status": "UNKNOWN", "title": ""}
        if link.case_type == "REQUEST":
            row = db.query(Request).filter(
                Request.id == link.case_id, Request.customer_ref == owner,
            ).first()
            if row:
                item.update(status=row.status, title=row.raw_text[:120])
        elif link.case_type == "EXECUTION":
            row = db.query(ExecutionCase).filter(
                ExecutionCase.id == link.case_id,
                ExecutionCase.customer_ref == owner,
            ).first()
            if row:
                req = db.query(Request).filter(
                    Request.id == row.request_id, Request.customer_ref == owner,
                ).first()
                if not req:
                    continue
                item.update(status=row.status, title=(req.raw_text[:120] if req else "متابعة تنفيذ"))
        elif link.case_type == "EXTERNAL":
            row = db.query(ExternalCase).filter(
                ExternalCase.id == link.case_id,
                ExternalCase.customer_ref == owner,
            ).first()
            if row:
                item.update(status=row.status, title=row.title)
        if item["status"] != "UNKNOWN":
            context.append(item)
    return context


def honest_request_card(row: Request):
    states = {
        "DISCOVERING": ("بدور على جهات مناسبة", "discovery"),
        "SUPPLY_FOUND_NO_CHANNEL": ("لقيت جهات، لكن مفيش تواصل مؤكد لسه", "discovery"),
        "WAITING_OFFERS": ("اتواصلنا فعلًا ومستنيين عروض", "outreach"),
        "OFFER_FOUND": ("وصل عرض فعلي", "offer"),
        "NO_REACHABLE_SUPPLY": ("ملقتش جهة قابلة للتواصل دلوقتي", "problem"),
    }
    label, phase = states.get(row.status, ("الموضوع مفتوح وبتابعه", "tracking"))
    return {
        "type": "status",
        "phase": phase,
        "title": row.raw_text[:100],
        "status": row.status,
        "label": label,
        "url": f"/requests/{row.id}",
    }


async def start_request_from_conversation(
    db: Session, text: str, customer_ref: str, locale_context=None,
    source_turn_id: str | None = None,
):
    """Runs the existing discovery/outreach pipeline with explicit truth states."""
    customer_ref = require_customer_ref(customer_ref)
    locale = locale_context or resolve_locale(None)
    if not hasattr(locale, "locale"):
        locale = resolve_locale(locale)
    if source_turn_id:
        existing = db.query(Request).filter(
            Request.customer_ref == customer_ref,
            Request.source_turn_id == source_turn_id,
        ).first()
        if existing:
            return existing
    parsed = understand_request(text)
    parsed, applied_preferences = apply_confirmed_preferences(db, parsed, text, customer_ref)
    row = Request(
        customer_ref=customer_ref, locale=locale.locale, region=locale.region,
        currency=locale.currency, source_turn_id=source_turn_id,
        raw_text=text, item=parsed["item"], area=parsed["area"], budget=parsed["budget"],
        deadline=parsed["deadline"], status="DISCOVERING",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if source_turn_id:
            existing = db.query(Request).filter(
                Request.customer_ref == customer_ref,
                Request.source_turn_id == source_turn_id,
            ).first()
            if existing:
                return existing
        raise
    db.refresh(row)
    log_event(db, row.id, "REQUEST_CREATED_FROM_CONVERSATION", text)
    if applied_preferences:
        log_event(db, row.id, "CONFIRMED_PREFERENCES_APPLIED", " | ".join(applied_preferences))

    discovered = await discover_businesses(text, parsed.get("area"), locale)
    log_event(db, row.id, "DISCOVERY_DONE", str(len(discovered)))
    sent_count = 0
    queued_count = 0
    for candidate in discovered:
        biz = None
        if candidate.get("external_id"):
            biz = db.query(Business).filter(Business.external_id == candidate["external_id"]).first()
        if not biz:
            biz = Business(
                external_id=candidate.get("external_id"), name=candidate["name"],
                website=candidate.get("website"), phone=candidate.get("phone"),
                source=candidate.get("source", "discovery"),
            )
            db.add(biz); db.commit(); db.refresh(biz)
        reach = build_reachability(candidate)
        log_event(db, row.id, "REACHABILITY_CHECK", f"{biz.name}|reachable={reach['reachable']}|channel={reach.get('channel')}")
        if reach["reachable"]:
            db.add(MerchantLink(token=new_token(), request_id=row.id, business_id=biz.id, status="CREATED"))
            db.commit()
        attempt = route_request_to_business(db, row, biz)
        if attempt.status in ("SENT", "PENDING"):
            delivery = deliver_request(db, attempt)
            if delivery.status == "SENT":
                sent_count += 1
                log_event(db, row.id, "OUTREACH_SENT_CONFIRMED", biz.name)
            elif delivery.status == "QUEUED":
                queued_count += 1
                log_event(db, row.id, "OUTREACH_QUEUED", biz.name)

    if sent_count:
        row.status = "WAITING_OFFERS"
        log_event(db, row.id, "WAITING_FOR_REAL_OFFERS", f"sent={sent_count}")
    elif discovered:
        row.status = "SUPPLY_FOUND_NO_CHANNEL"
        log_event(db, row.id, "OUTREACH_NOT_CONFIRMED", f"found={len(discovered)};queued={queued_count};sent=0")
    else:
        row.status = "NO_REACHABLE_SUPPLY"
        log_event(db, row.id, "REQUEST_FAILED", "NO_REACHABLE_SUPPLY")
    db.commit(); db.refresh(row)
    return row


def _chat_turn_fingerprint(text: str, locale: str) -> str:
    canonical = json.dumps({"message": text, "locale": locale}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _chat_turn_busy() -> HTTPException:
    return HTTPException(409, "This chat turn is still processing", headers={"Retry-After": "2"})


@app.post("/api/chat")
async def chat_turn(payload: ChatTurnInput, request: FastAPIRequest, db: Session = Depends(get_db)):
    text = " ".join(payload.message.split())
    if not text:
        raise HTTPException(422, "Message cannot be empty")
    customer_ref = customer_ref_from_request(request, payload.customer_ref)
    request.state.customer_ref = customer_ref
    client_turn_id = str(payload.client_turn_id) if payload.client_turn_id else None
    locale = resolve_locale(payload.locale)
    fingerprint = _chat_turn_fingerprint(text, locale.locale)
    turn = None
    lease_token = None

    # A customer-scoped lookup also makes the very first message retryable even
    # when the original response (and therefore its new thread id) was lost.
    if client_turn_id:
        turn = db.query(ConversationTurn).filter(
            ConversationTurn.customer_ref == customer_ref,
            ConversationTurn.client_turn_id == client_turn_id,
        ).first()
        if turn:
            if turn.request_fingerprint != fingerprint:
                raise HTTPException(409, "client_turn_id was already used for different content")
            if payload.thread_id and payload.thread_id != turn.thread_id:
                raise HTTPException(409, "client_turn_id belongs to a different conversation")
            if turn.status == "COMPLETED" and turn.response_envelope:
                try:
                    return json.loads(turn.response_envelope)
                except (TypeError, ValueError):
                    turn.status = "FAILED"
                    db.commit()
            cutoff = datetime.utcnow() - timedelta(seconds=CHAT_TURN_STALE_SECONDS)
            if turn.status == "PROCESSING" and turn.updated_at and turn.updated_at > cutoff:
                raise _chat_turn_busy()
            previous_lease = turn.lease_token
            lease_token = str(uuid.uuid4())
            claimed = db.query(ConversationTurn).filter(
                ConversationTurn.id == turn.id,
                ConversationTurn.status == turn.status,
                ConversationTurn.lease_token == previous_lease,
            ).update({
                ConversationTurn.status: "PROCESSING",
                ConversationTurn.lease_token: lease_token,
                ConversationTurn.attempt_count: ConversationTurn.attempt_count + 1,
                ConversationTurn.updated_at: datetime.utcnow(),
            }, synchronize_session=False)
            db.commit()
            if claimed != 1:
                raise _chat_turn_busy()
            turn = db.get(ConversationTurn, turn.id)

    thread = None
    if turn:
        thread = db.query(ConversationThread).filter(
            ConversationThread.id == turn.thread_id,
            ConversationThread.customer_ref == customer_ref,
        ).first()
        if not thread:
            raise HTTPException(409, "The persisted conversation for this turn is unavailable")
    elif payload.thread_id:
        thread = db.query(ConversationThread).filter(
            ConversationThread.id == payload.thread_id,
            ConversationThread.customer_ref == customer_ref,
        ).first()
        if not thread:
            raise HTTPException(404, "Conversation not found")
    if not thread:
        thread = ConversationThread(
            id=str(uuid.uuid4()), customer_ref=customer_ref, locale=locale.locale,
            language=locale.language, region=locale.region, currency=locale.currency,
        )
        db.add(thread)
        db.flush()

    if client_turn_id and not turn:
        lease_token = str(uuid.uuid4())
        turn = ConversationTurn(
            thread_id=thread.id, customer_ref=customer_ref,
            client_turn_id=client_turn_id, request_fingerprint=fingerprint,
            status="PROCESSING", lease_token=lease_token,
        )
        db.add(turn)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            # Another worker owns the same turn. It will either publish the
            # durable envelope or become reclaimable after the lease expires.
            existing = db.query(ConversationTurn).filter(
                ConversationTurn.customer_ref == customer_ref,
                ConversationTurn.client_turn_id == client_turn_id,
            ).first()
            if existing and existing.status == "COMPLETED" and existing.response_envelope:
                return json.loads(existing.response_envelope)
            raise _chat_turn_busy()
        db.refresh(turn)
        db.refresh(thread)
    elif not turn:
        db.commit()
        db.refresh(thread)

    if turn and turn.user_message_id:
        user_message = db.get(ConversationMessage, turn.user_message_id)
        if not user_message or user_message.thread_id != thread.id or user_message.content != text:
            raise HTTPException(409, "Persisted chat turn is inconsistent")
    else:
        user_message = ConversationMessage(thread_id=thread.id, role="USER", content=text)
        db.add(user_message)
        db.flush()
        if turn:
            turn.user_message_id = user_message.id
            turn.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(user_message)

    history_rows = db.query(ConversationMessage).filter(
        ConversationMessage.thread_id == thread.id,
        ConversationMessage.id != user_message.id,
    ).order_by(ConversationMessage.id.desc()).limit(20).all()
    history = [{"role": row.role.lower(), "content": row.content} for row in reversed(history_rows)]
    active_cases = linked_case_context(db, thread.id, customer_ref)
    turn_context = TurnContext(text, history, thread.locale, active_cases)
    try:
        reply = await conversation_provider.respond(turn_context)
    except Exception:
        db.rollback()
        if turn:
            db.refresh(turn)
            if turn.lease_token == lease_token and turn.status == "PROCESSING":
                turn.status = "FAILED"
                turn.updated_at = datetime.utcnow()
                db.commit()
        raise

    # A slow superseded worker must never cross the business-action boundary.
    if turn:
        db.refresh(turn)
        if turn.status != "PROCESSING" or turn.lease_token != lease_token:
            raise _chat_turn_busy()

    reply = enforce_case_turn_policy(reply, turn_context)
    decision = gate_action(reply, active_cases)
    action_status = "PROPOSED"
    card = None
    action_reason = decision.reason
    linked_case = None

    if reply.action.type == ActionType.NONE:
        action_status = "BLOCKED"
        if reply.intent.value == "CONTINUATION" and len(active_cases) == 1:
            card = case_status_card(active_cases[0], reply.style)
            linked_case = {"type": active_cases[0]["type"], "id": active_cases[0]["id"]}
    elif decision.allowed and reply.action.type == ActionType.CREATE_REQUEST:
        try:
            request_text = str(reply.action.payload.get("text") or text).strip()
            request_row = await start_request_from_conversation(
                db, request_text, customer_ref, resolve_locale(thread.locale), client_turn_id,
            )
            link = db.query(ConversationCaseLink).filter_by(
                thread_id=thread.id, case_type="REQUEST", case_id=request_row.id,
            ).first()
            if not link:
                db.add(ConversationCaseLink(
                    thread_id=thread.id, case_type="REQUEST", case_id=request_row.id,
                ))
                db.commit()
            linked_case = {"type": "REQUEST", "id": request_row.id}
            card = honest_request_card(request_row)
            action_status = "EXECUTED"
        except Exception as exc:
            db.rollback()
            action_status = "FAILED"
            action_reason = f"Request pipeline failed safely: {type(exc).__name__}"
    elif decision.allowed:
        execution = execute_case_action(db, thread.id, reply.action, text, reply.style)
        action_status = execution.status
        action_reason = execution.reason
        linked_case = execution.linked_case
        card = execution.card
        if execution.text:
            reply.text = execution.text
    else:
        action_status = "BLOCKED"

    style_json = json.dumps(reply.style.__dict__, ensure_ascii=False)
    provider_json = json.dumps({"provider": reply.provider, "model": reply.model, "degraded": reply.degraded}, ensure_ascii=False)
    assistant_message = ConversationMessage(
        thread_id=thread.id, role="ASSISTANT", content=reply.text, intent=reply.intent.value,
        style_metadata=style_json, provider_metadata=provider_json,
    )
    db.add(assistant_message)
    db.flush()
    db.add(ConversationAction(
        thread_id=thread.id, message_id=user_message.id, action_type=reply.action.type.value,
        status=action_status, reason=action_reason,
        payload=json.dumps(reply.action.payload, ensure_ascii=False),
    ))
    thread.provider = reply.provider
    thread.degraded_mode = reply.degraded
    thread.language = reply.style.language
    thread.updated_at = datetime.utcnow()
    envelope = {
        "thread_id": thread.id,
        "client_turn_id": client_turn_id,
        "message": {"id": assistant_message.id, "role": "assistant", "content": reply.text},
        "intent": reply.intent.value,
        "style": reply.style.__dict__,
        "provider": {"name": reply.provider, "model": reply.model, "degraded": reply.degraded},
        "action": {"type": reply.action.type.value, "status": action_status, "reason": action_reason},
        "linked_case": linked_case,
        "card": card,
    }
    if turn:
        db.refresh(turn)
        if turn.status != "PROCESSING" or turn.lease_token != lease_token:
            db.rollback()
            raise _chat_turn_busy()
        turn.response_envelope = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        turn.status = "COMPLETED"
        turn.completed_at = datetime.utcnow()
        turn.updated_at = turn.completed_at
    db.commit()
    return envelope


@app.get("/api/conversations/{thread_id}")
def conversation_history(thread_id: str, request: FastAPIRequest, db: Session = Depends(get_db)):
    customer_ref = customer_ref_from_request(request)
    thread = db.query(ConversationThread).filter(
        ConversationThread.id == thread_id,
        ConversationThread.customer_ref == customer_ref,
    ).first()
    if not thread:
        raise HTTPException(404, "Conversation not found")
    rows = db.query(ConversationMessage).filter(
        ConversationMessage.thread_id == thread.id
    ).order_by(ConversationMessage.id.asc()).all()
    return {
        "thread_id": thread.id,
        "degraded": thread.degraded_mode,
        "messages": [
            {"id": row.id, "role": row.role.lower(), "content": row.content, "intent": row.intent}
            for row in rows
        ],
        "cases": linked_case_context(db, thread.id, customer_ref),
    }


@app.get("/api/execution/conversations/{thread_id}")
def conversation_execution_updates(thread_id: str, request: FastAPIRequest, db: Session = Depends(get_db)):
    """Return only execution evidence owned by this persisted conversation."""
    customer_ref = customer_ref_from_request(request)
    thread = db.query(ConversationThread).filter(
        ConversationThread.id == thread_id,
        ConversationThread.customer_ref == customer_ref,
    ).first()
    if not thread:
        raise HTTPException(404, "Conversation not found")

    notices = db.query(ExecutionNotice).join(
        Request, Request.id == ExecutionNotice.request_id,
    ).filter(
        ExecutionNotice.thread_id == thread_id,
        Request.customer_ref == customer_ref,
    ).order_by(ExecutionNotice.id).all()
    cards = []
    links = db.query(ConversationCaseLink).filter_by(thread_id=thread_id, case_type="REQUEST").all()
    for link in links:
        request_row = db.query(Request).filter(
            Request.id == link.case_id,
            Request.customer_ref == customer_ref,
        ).first()
        if not request_row:
            continue
        leads = []
        attempts = db.query(ReachAttempt).filter_by(request_id=request_row.id).all()
        for attempt in attempts:
            business = db.get(Business, attempt.business_id)
            if business:
                leads.append({
                    "name": business.name,
                    "source": business.source,
                    "website": business.website,
                    "status": attempt.status,
                })
        cards.append({
            "request_id": request_row.id,
            "status": request_row.status,
            "card": honest_request_card(request_row),
            "leads": leads,
        })
    return {
        "notices": [{"id": notice.id, "content": notice.content} for notice in notices],
        "requests": cards,
        "attribution": "© OpenStreetMap contributors · ODbL; search leads are not availability or price guarantees",
    }


# Load the verified execution extension only after every core route and helper is
# defined.  This keeps ``uvicorn app.main:app`` as the single production entry
# point while activating its authenticated channels, offer intake, recovery and
# background delivery worker.
from . import execution_app as _execution_app  # noqa: E402,F401
