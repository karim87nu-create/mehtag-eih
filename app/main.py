import re
import hashlib
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, Request as FastAPIRequest, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timedelta
from .db import Base, engine, SessionLocal
from .models import Request, Business, MerchantLink, Offer, Event, ExecutionCase, CaseEvent, ExternalCase, MemoryFact, DetectedTransaction, TransactionEvent, FollowupRule, FollowupTask, LearnedPreference, BusinessPerformance, OfferAmendment, IssueRecord, CustomerRule, CapabilitySignal, ReachAttempt, BusinessActivation, IntegrationEndpoint, TransportDelivery, PaymentIntent, ConsentRecord, AuditRecord, MobileSourceEvent
from .services import understand_request, discover_businesses, build_reachability, new_token

Base.metadata.create_all(bind=engine)
app = FastAPI(title="Request Spike v0.2 - Gate A")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
templates = Jinja2Templates(directory="app/templates")

LINK_TTL_HOURS = 24

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

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
        r'(?:Ù…Ù†|Ù…ÙÙ†)\s+([A-Za-z\u0600-\u06FF][A-Za-z0-9\u0600-\u06FF .&_-]{1,40})',
        r'(?:Ø§Ø·Ù„Ø¨Ù„ÙŠ|Ù‡Ø§ØªÙ„ÙŠ|Ø¬ÙŠØ¨Ù„ÙŠ)\s+.+?\s+(?:Ù…Ù†|Ù…ÙÙ†)\s+([A-Za-z\u0600-\u06FF][A-Za-z0-9\u0600-\u06FF .&_-]{1,40})'
    ]
    merchant = None
    for p in directed_patterns:
        mm = re.search(p, raw, re.I)
        if mm:
            merchant = mm.group(1).strip(" .ØŒ")
            break

    remembered_words = ["Ø§Ù„Ù…Ø¹ØªØ§Ø¯", "Ø²ÙŠ ÙƒÙ„ Ù…Ø±Ø©", "Ù†ÙØ³ Ø§Ù„Ù„ÙŠ ÙØ§Øª", "Ù†ÙØ³Ù‡ ØªØ§Ù†ÙŠ", "ÙƒØ±Ø±"]
    if any(x in low for x in remembered_words):
        intent_type = "REMEMBERED"
    elif merchant:
        intent_type = "DIRECTED"
    else:
        intent_type = "OPEN"

    # Budget
    budget = None
    bm = re.search(r'(?:Ø­Ø¯ÙˆØ¯|Ù„Ø­Ø¯|Ù…ÙŠØ²Ø§Ù†ÙŠ(?:Ø©|ØªÙŠ)|Ø¨Ø­Ø¯ Ø£Ù‚ØµÙ‰|Ø¨Ø­Ø¯ Ø§Ù‚ØµÙ‰)\s*([0-9Ù -Ù©][0-9Ù -Ù©,\.]*)\s*(?:Ø¬|Ø¬Ù†ÙŠÙ‡|Ø§Ù„Ù|Ø£Ù„Ù|k)?', raw, re.I)
    if bm:
        digits = bm.group(1).translate(str.maketrans("Ù Ù¡Ù¢Ù£Ù¤Ù¥Ù¦Ù§Ù¨Ù©","0123456789")).replace(",","")
        try:
            budget=float(digits)
            tail=raw[bm.end()-8:bm.end()+8].lower()
            if "Ø§Ù„Ù" in tail or "Ø£Ù„Ù" in tail or "k" in tail:
                budget*=1000
        except: pass

    # Area and deadline/time are intentionally lightweight
    area = None
    am = re.search(r'(?:ÙŠÙˆØµÙ„|ØªÙˆØµÙŠÙ„|ÙÙŠ|Ù„Ù€|Ø§Ù„Ù‰|Ø¥Ù„Ù‰)\s+(Ù…Ø¯ÙŠÙ†Ø© Ù†ØµØ±|Ù…ØµØ± Ø§Ù„Ø¬Ø¯ÙŠØ¯Ø©|Ø§Ù„ØªØ¬Ù…Ø¹|Ø§Ù„Ù‚Ø§Ù‡Ø±Ø© Ø§Ù„Ø¬Ø¯ÙŠØ¯Ø©|Ù…Ø¯ÙŠÙ†ØªÙŠ|Ø§Ù„Ù…Ø¹Ø§Ø¯ÙŠ|Ø§Ù„Ø²Ù…Ø§Ù„Ùƒ|Ø§Ù„Ø¯Ù‚ÙŠ|Ø§Ù„Ù‡Ø±Ù…|Ø§Ù„Ø¬ÙŠØ²Ø©)', raw)
    if am: area=am.group(1)

    deadline = None
    dm = re.search(r'(Ø§Ù„ÙŠÙˆÙ…|Ø¨ÙƒØ±Ø©|ØºØ¯Ø§|ØºØ¯Ø§Ù‹|Ø§Ù„Ø£Ø­Ø¯|Ø§Ù„Ø§Ø­Ø¯|Ø§Ù„Ø§Ø«Ù†ÙŠÙ†|Ø§Ù„Ø¥Ø«Ù†ÙŠÙ†|Ø§Ù„Ø«Ù„Ø§Ø«Ø§Ø¡|Ø§Ù„Ø£Ø±Ø¨Ø¹Ø§Ø¡|Ø§Ù„Ø§Ø±Ø¨Ø¹Ø§Ø¡|Ø§Ù„Ø®Ù…ÙŠØ³|Ø§Ù„Ø¬Ù…Ø¹Ø©|Ø§Ù„Ø³Ø¨Øª)(?:\s+(?:Ø§Ù„Ø³Ø§Ø¹Ø©\s*)?\d{1,2}(?::\d{2})?\s*(?:Øµ|Ù…)?)?', raw)
    if dm: deadline=dm.group(0)

    return {
        "raw": raw, "intent_type": intent_type, "directed_merchant": merchant,
        "budget": budget, "area": area, "deadline": deadline
    }

def service_fee_for_request(req, offer):
    """Transparent prototype fee, per caseâ€”not per message.
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
    pi=PaymentIntent(execution_case_id=case.id,amount=float(offer.price)+fee,
                     service_fee=fee,provider="SIMULATION",status="CREATED")
    db.add(pi); db.commit(); db.refresh(pi); return pi


def audit(db, action, entity_type=None, entity_id=None, detail=None, actor="SYSTEM"):
    row=AuditRecord(actor=actor,action=action,entity_type=entity_type,
                    entity_id=str(entity_id) if entity_id is not None else None,
                    detail=detail)
    db.add(row); db.commit(); return row

def system_health(db):
    return {
        "db": "ok",
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


def mobile_event_hash(source, package_name, title, body):
    raw="|".join([source or "",package_name or "",title or "",body or ""]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def ingest_mobile_source(db, source, body, package_name=None, title=None):
    h=mobile_event_hash(source,package_name,title,body)
    existing=db.query(MobileSourceEvent).filter(MobileSourceEvent.event_hash==h).first()
    if existing:
        return existing, False
    row=MobileSourceEvent(source=source,package_name=package_name,title=title,body=body,event_hash=h,status="RECEIVED")
    db.add(row); db.commit(); db.refresh(row)
    audit(db,"MOBILE_SOURCE_RECEIVED","MobileSourceEvent",row.id,f"{source}:{package_name or ''}")
    return row, True

@app.get("/", response_class=HTMLResponse)
def home(request: FastAPIRequest, db: Session = Depends(get_db)):
    execution_cases = db.query(ExecutionCase).filter(ExecutionCase.status.notin_(["VERIFIED_OUTCOME"])).order_by(ExecutionCase.id.desc()).limit(5).all()
    external_cases = db.query(ExternalCase).filter(ExternalCase.status.notin_(["VERIFIED_OUTCOME"])).order_by(ExternalCase.id.desc()).limit(5).all()
    open_cases = []
    for c in execution_cases:
        req = db.query(Request).filter(Request.id == c.request_id).first()
        open_cases.append({"kind":"execution","title": req.raw_text[:60], "status":c.status, "url":f"/cases/{c.id}"})
    for c in external_cases:
        open_cases.append({"kind":"external","title":c.title, "status":c.status, "url":f"/external/{c.id}"})
    memories = db.query(MemoryFact).filter(
        MemoryFact.memory_type == "customer",
        MemoryFact.key == "verified_outcome"
    ).order_by(MemoryFact.id.desc()).limit(3).all()
    suggestions = db.query(DetectedTransaction).filter(
        DetectedTransaction.status == "SUGGESTED"
    ).order_by(DetectedTransaction.id.desc()).limit(5).all()
    due_tasks = db.query(FollowupTask).filter(
        FollowupTask.status == "PENDING"
    ).order_by(FollowupTask.due_at.asc()).limit(5).all()
    followups = []
    for task in due_tasks:
        c = db.query(ExternalCase).filter(ExternalCase.id == task.external_case_id).first()
        if c:
            followups.append({"task": task, "case": c})
    learned = preference_suggestions(db)
    return templates.TemplateResponse("home.html", {
        "request": request, "open_cases": open_cases, "memories": memories,
        "suggestions": suggestions, "followups": followups, "learned": learned
    })

@app.post("/requests", response_class=HTMLResponse)
async def create_request(text: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    parsed = understand_request(text)
    parsed, applied_preferences = apply_confirmed_preferences(db, parsed, text)
    row = Request(raw_text=text, item=parsed["item"], area=parsed["area"],
                  budget=parsed["budget"], deadline=parsed["deadline"], status="DISCOVERING")
    db.add(row); db.commit(); db.refresh(row)
    log_event(db, row.id, "REQUEST_CREATED", text)
    intent = understand_request_v2(text)
    log_event(db, row.id, "REQUEST_INTENT_CLASSIFIED",
              f"type={intent['intent_type']};merchant={intent.get('directed_merchant')};area={intent.get('area')};deadline={intent.get('deadline')}")
    if applied_preferences:
        log_event(db, row.id, "CONFIRMED_PREFERENCES_APPLIED", " | ".join(applied_preferences))

    discovered = await discover_businesses(text, parsed.get("area"))
    log_event(db, row.id, "DISCOVERY_DONE", str(len(discovered)))

    # Discovery is not outreach. Keep the internal state honest:
    # finding a business/website never means the merchant received the request.
    links = []
    discovered_count = len(discovered)
    sent_count = 0
    queued_count = 0

    for b in discovered:
        biz = db.query(Business).filter(Business.external_id == b.get("external_id")).first() if b.get("external_id") else None
        if not biz:
            biz = Business(external_id=b.get("external_id"), name=b["name"], website=b.get("website"),
                           phone=b.get("phone"), source=b.get("source", "discovery"))
            db.add(biz); db.commit(); db.refresh(biz)

        reach = build_reachability(b)
        log_event(db, row.id, "REACHABILITY_CHECK",
                  f"{biz.name}|reachable={reach['reachable']}|channel={reach.get('channel')}")

        # Keep a private response link available for configured/test transports,
        # but do not expose its existence as proof of contact.
        if reach["reachable"]:
            token = new_token()
            link = MerchantLink(token=token, request_id=row.id, business_id=biz.id, status="CREATED")
            db.add(link); db.commit()
            links.append((biz.name, token))
            log_event(db, row.id, "MERCHANT_LINK_CREATED_INTERNAL", biz.name)

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
    elif discovered_count:
        row.status = "SUPPLY_FOUND_NO_CHANNEL"
        log_event(db, row.id, "OUTREACH_NOT_CONFIRMED",
                  f"found={discovered_count};queued={queued_count};sent=0")
    else:
        row.status = "NO_REACHABLE_SUPPLY"
        log_event(db, row.id, "REQUEST_FAILED", "NO_REACHABLE_SUPPLY")
    db.commit()

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
        return HTMLResponse("<html dir='rtl'><body><h2>Ø§Ù†ØªÙ‡Øª ØµÙ„Ø§Ø­ÙŠØ© Ø§Ù„Ø·Ù„Ø¨.</h2></body></html>", status_code=410)

    req = db.query(Request).filter(Request.id == link.request_id).first()
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
    msg = "ØªÙ… Ø¥Ø±Ø³Ø§Ù„ Ø¹Ø±Ø¶Ùƒ Ø¨Ù†Ø¬Ø§Ø­." if is_valid else "ØªÙ… Ø§Ø³ØªÙ„Ø§Ù… Ø§Ù„Ø¹Ø±Ø¶ØŒ Ù„ÙƒÙ†Ù‡ Ø®Ø§Ø±Ø¬ Ø´Ø±ÙˆØ· Ø§Ù„Ø·Ù„Ø¨ Ø§Ù„Ø­Ø§Ù„ÙŠØ©."
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
            reasons.append("Ø¯Ø§Ø®Ù„ Ù…ÙŠØ²Ø§Ù†ÙŠØªÙƒ")
        else:
            over = (offer.price - req.budget) / max(req.budget, 1)
            score -= min(45, over * 100)
            reasons.append("Ø£Ø¹Ù„Ù‰ Ù…Ù† Ø§Ù„Ù…ÙŠØ²Ø§Ù†ÙŠØ©")

    perf = business_performance(db, offer.business_id)
    total = perf.verified_ok + perf.issues + perf.cancellations
    if total >= 3:
        success_rate = perf.verified_ok / max(total, 1)
        score += success_rate * 25
        reasons.append(f"Ø³Ø¬Ù„ Ù†ØªÙŠØ¬Ø© Ù†Ø§Ø¬Ø­Ø© {round(success_rate*100)}%")
    else:
        score -= 3
        reasons.append("Ø³Ø¬Ù„ Ø§Ù„ØªÙ†ÙÙŠØ° Ù„Ø³Ù‡ Ù…Ø­Ø¯ÙˆØ¯")

    score -= perf.cancellations * 3
    score -= perf.issues * 2
    score -= perf.late * 1.5
    score -= perf.price_changes * 2

    # Confirmed preferences: only transparent, customer-approved rules.
    prefs = confirmed_preferences(db)
    notes = (offer.notes or "").lower()
    for p in prefs:
        if p.preference_key == "warranty" and "Ø¶Ù…Ø§Ù†" in p.preference_value:
            if "Ø¶Ù…Ø§Ù† Ø±Ø³Ù…ÙŠ" in notes or "Ø¶Ù…Ø§Ù† Ø§Ù„ÙˆÙƒÙŠÙ„" in notes:
                score += 12
                reasons.append("Ù…Ø·Ø§Ø¨Ù‚ Ù„ØªÙØ¶ÙŠÙ„Ùƒ ÙÙŠ Ø§Ù„Ø¶Ù…Ø§Ù†")
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
    req = db.query(Request).filter(Request.id == request_id).first()
    if not req:
        raise HTTPException(404, "Request not found")
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
    req = db.query(Request).filter(Request.id == request_id).first()
    offer = db.query(Offer).filter(Offer.id == offer_id, Offer.request_id == request_id, Offer.status == "VALID").first()
    if not req or not offer:
        raise HTTPException(404, "Offer not found")
    req.status = "SELECTED"
    case = db.query(ExecutionCase).filter(ExecutionCase.request_id == req.id).first()
    if not case:
        case = ExecutionCase(request_id=req.id, offer_id=offer.id, status="AWAITING_PAYMENT",
                             payment_status="NOT_STARTED", outcome_status="OPEN", expected_at=offer.eta)
        db.add(case)
    db.commit(); db.refresh(case)
    log_event(db, req.id, "OFFER_SELECTED", f"offer_id={offer.id};price={offer.price}")
    db.add(CaseEvent(case_id=case.id, event_type="CASE_OPENED", detail="AWAITING_PAYMENT")); db.commit()
    return templates.TemplateResponse("selected.html", {"request": request, "req": req, "offer": offer, "case": case})

@app.get("/admin", response_class=HTMLResponse)
def admin(request: FastAPIRequest, db: Session = Depends(get_db)):
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
    case = db.query(ExecutionCase).filter(ExecutionCase.id == case_id).first()
    if not case: raise HTTPException(404, "Case not found")
    case.payment_status = "AUTHORIZED_DEMO"
    case.status = "CONFIRMED"
    req = db.query(Request).filter(Request.id == case.request_id).first()
    req.status = "CONFIRMED"
    db.add(CaseEvent(case_id=case.id, event_type="PAYMENT_AUTHORIZED_DEMO", detail="No real money moved"))
    db.commit()
    issues = db.query(IssueRecord).filter(IssueRecord.execution_case_id == case.id).order_by(IssueRecord.id.desc()).all()
    offer = get_case_offer(db, case)
    amendment = get_pending_amendment(db, offer.id) if offer else None
    return templates.TemplateResponse("case.html", {"request": request, "case": case, "req": req, "issues": issues, "offer": offer, "amendment": amendment})

@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_status(case_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    case = db.query(ExecutionCase).filter(ExecutionCase.id == case_id).first()
    if not case: raise HTTPException(404, "Case not found")
    req = db.query(Request).filter(Request.id == case.request_id).first()
    issues = db.query(IssueRecord).filter(IssueRecord.execution_case_id == case.id).order_by(IssueRecord.id.desc()).all()
    offer = get_case_offer(db, case)
    amendment = get_pending_amendment(db, offer.id) if offer else None
    return templates.TemplateResponse("case.html", {"request": request, "case": case, "req": req, "issues": issues, "offer": offer, "amendment": amendment})

@app.post("/cases/{case_id}/event", response_class=HTMLResponse)
def case_event(case_id: int, event_type: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    case = db.query(ExecutionCase).filter(ExecutionCase.id == case_id).first()
    if not case: raise HTTPException(404, "Case not found")
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
    if "Ø¶Ù…Ø§Ù† Ø±Ø³Ù…ÙŠ" in text or "Ø¶Ù…Ø§Ù† Ø§Ù„ÙˆÙƒÙŠÙ„" in text:
        candidates.append(("warranty", "ÙŠÙØ¶Ù„ Ø§Ù„Ø¶Ù…Ø§Ù† Ø§Ù„Ø±Ø³Ù…ÙŠ"))
    if "Ù…Ø³Ø§Ø¡" in text or "Ø¨Ø§Ù„Ù„ÙŠÙ„" in text:
        candidates.append(("time_window", "ÙŠÙØ¶Ù„ Ø§Ù„ØªÙ†ÙÙŠØ° Ù…Ø³Ø§Ø¡Ù‹"))
    if "Ø§Ù„ØµØ¨Ø­" in text or "ØµØ¨Ø§Ø­" in text:
        candidates.append(("time_window", "ÙŠÙØ¶Ù„ Ø§Ù„ØªÙ†ÙÙŠØ° ØµØ¨Ø§Ø­Ù‹Ø§"))

    for key, value in candidates:
        row = db.query(LearnedPreference).filter(
            LearnedPreference.preference_key == key,
            LearnedPreference.preference_value == value
        ).first()
        if row:
            row.evidence_count += 1
        else:
            row = LearnedPreference(preference_key=key, preference_value=value,
                                    evidence_count=1, confidence=0.25, status="OBSERVED")
            db.add(row)
        db.flush()
        row.confidence = min(0.95, 0.25 + max(0, row.evidence_count-1)*0.25)
        if row.evidence_count >= 3:
            row.status = "SUGGESTIBLE"
    db.commit()

def preference_suggestions(db):
    return db.query(LearnedPreference).filter(
        LearnedPreference.status == "SUGGESTIBLE"
    ).order_by(LearnedPreference.confidence.desc()).all()


def confirmed_preferences(db):
    return db.query(LearnedPreference).filter(
        LearnedPreference.status == "CONFIRMED"
    ).all()

def apply_confirmed_preferences(db, parsed, raw_text):
    """Use only customer-confirmed rules, and never override an explicit instruction."""
    applied = []
    text = raw_text.lower()
    for p in confirmed_preferences(db):
        if p.preference_key == "warranty":
            # Explicit contrary/alternative wording wins over memory.
            if not any(x in text for x in ["Ù…Ù† ØºÙŠØ± Ø¶Ù…Ø§Ù†", "Ø¨Ø¯ÙˆÙ† Ø¶Ù…Ø§Ù†", "Ù…Ø´ Ù…Ù‡Ù… Ø§Ù„Ø¶Ù…Ø§Ù†"]):
                parsed["confirmed_warranty_preference"] = p.preference_value
                applied.append(p.preference_value)
        elif p.preference_key == "time_window":
            # Don't inject a time preference if user already stated a time window.
            explicit_time = any(x in text for x in ["Ø§Ù„ØµØ¨Ø­", "ØµØ¨Ø§Ø­", "Ù…Ø³Ø§Ø¡", "Ø¨Ø§Ù„Ù„ÙŠÙ„", "Ø§Ù„Ø¸Ù‡Ø±"])
            if not explicit_time:
                parsed["confirmed_time_preference"] = p.preference_value
                applied.append(p.preference_value)
    return parsed, applied

@app.post("/cases/{case_id}/outcome", response_class=HTMLResponse)
def confirm_outcome(case_id: int, result: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    case = db.query(ExecutionCase).filter(ExecutionCase.id == case_id).first()
    if not case: raise HTTPException(404, "Case not found")
    req = db.query(Request).filter(Request.id == case.request_id).first()
    if result == "ok":
        update_business_outcome(db, case, True)
        offer = get_case_offer(db, case)
        if offer and req: mark_capability_completed(db, offer, req)
        case.outcome_status="VERIFIED"
        case.status="VERIFIED_OUTCOME"
        req.status="VERIFIED_OUTCOME"
        audit(db,"OUTCOME_VERIFIED","Request",req.id,req.raw_text)
        evt="OUTCOME_VERIFIED"
        learn_from_verified_outcome(db, req)
        existing_fact = db.query(MemoryFact).filter(MemoryFact.source_type=="request", MemoryFact.source_id==req.id, MemoryFact.key=="verified_outcome").first()
        if not existing_fact:
            db.add(MemoryFact(memory_type="customer", key="verified_outcome",
                              value=req.raw_text, source_type="request", source_id=req.id, confidence=1.0))
    else:
        update_business_outcome(db, case, False)
        case.outcome_status="ISSUE_OPEN"
        case.status="ISSUE_OPEN"
        req.status="ISSUE_OPEN"
        evt="OUTCOME_ISSUE_OPENED"
    db.add(CaseEvent(case_id=case.id,event_type=evt,detail=result))
    db.commit()
    issues = db.query(IssueRecord).filter(IssueRecord.execution_case_id == case.id).order_by(IssueRecord.id.desc()).all()
    offer = get_case_offer(db, case)
    amendment = get_pending_amendment(db, offer.id) if offer else None
    return templates.TemplateResponse("case.html", {"request": request, "case": case, "req": req, "issues": issues, "offer": offer, "amendment": amendment})


@app.get("/share", response_class=HTMLResponse)
def share_page(request: FastAPIRequest, text: str = ""):
    return templates.TemplateResponse("share.html", {"request": request, "shared_text": text})

@app.post("/share", response_class=HTMLResponse)
def create_external_case(source_text: str = Form(...), title: str = Form(""), expected_at: str = Form(""),
                         request: FastAPIRequest = None, db: Session = Depends(get_db)):
    clean_title = title.strip() or source_text[:60]
    case = ExternalCase(source_text=source_text, title=clean_title, expected_at=expected_at or None,
                        status="FOLLOWING", outcome_status="OPEN")
    db.add(case); db.commit(); db.refresh(case)
    return templates.TemplateResponse("external_case.html", {"request": request, "case": case})

@app.get("/external/{case_id}", response_class=HTMLResponse)
def external_case(case_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    case = db.query(ExternalCase).filter(ExternalCase.id == case_id).first()
    if not case: raise HTTPException(404, "External case not found")
    return templates.TemplateResponse("external_case.html", {"request": request, "case": case})

@app.post("/external/{case_id}/outcome", response_class=HTMLResponse)
def external_outcome(case_id: int, result: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    case = db.query(ExternalCase).filter(ExternalCase.id == case_id).first()
    if not case: raise HTTPException(404, "External case not found")
    if result == "ok":
        case.status="VERIFIED_OUTCOME"; case.outcome_status="VERIFIED"
    else:
        case.status="ISSUE_OPEN"; case.outcome_status="ISSUE_OPEN"
    db.commit()
    return templates.TemplateResponse("external_case.html", {"request": request, "case": case})

@app.get("/memory", response_class=HTMLResponse)
def memory_page(request: FastAPIRequest, db: Session = Depends(get_db)):
    facts = db.query(MemoryFact).order_by(MemoryFact.id.desc()).all()
    return templates.TemplateResponse("memory.html", {"request": request, "facts": facts})


@app.post("/memory/{memory_id}/repeat", response_class=HTMLResponse)
async def repeat_memory(memory_id: int, request: FastAPIRequest, db: Session = Depends(get_db)):
    fact = db.query(MemoryFact).filter(MemoryFact.id == memory_id,
                                      MemoryFact.memory_type == "customer",
                                      MemoryFact.key == "verified_outcome").first()
    if not fact:
        raise HTTPException(404, "Memory not found")
    parsed = understand_request(fact.value)
    parsed, applied_preferences = apply_confirmed_preferences(db, parsed, fact.value)
    row = Request(raw_text=fact.value, item=parsed["item"], area=parsed["area"],
                  budget=parsed["budget"], deadline=parsed["deadline"], status="DISCOVERING")
    db.add(row); db.commit(); db.refresh(row)
    log_event(db, row.id, "REQUEST_CREATED_FROM_MEMORY", f"memory_id={fact.id}")

    discovered = await discover_businesses(fact.value)
    log_event(db, row.id, "DISCOVERY_DONE", str(len(discovered)))
    links=[]; reachable=0
    for b in discovered:
        biz = db.query(Business).filter(Business.external_id == b.get("external_id")).first() if b.get("external_id") else None
        if not biz:
            biz=Business(external_id=b.get("external_id"), name=b["name"], website=b.get("website"),
                         phone=b.get("phone"), source=b.get("source","discovery"))
            db.add(biz); db.commit(); db.refresh(biz)
        reach=build_reachability(b)
        if reach["reachable"]:
            reachable += 1
            token=new_token()
            db.add(MerchantLink(token=token, request_id=row.id, business_id=biz.id, status="CREATED"))
            db.commit()
            links.append((biz.name,token))
    row.status="WAITING_OFFERS" if reachable else "NO_REACHABLE_SUPPLY"
    db.commit()
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
    completed = ["ØªÙ… Ø§Ù„ØªØ³Ù„ÙŠÙ… Ø¨Ù†Ø¬Ø§Ø­", "ØªÙ… Ø§Ù„Ø§Ø³ØªÙ„Ø§Ù… Ø¨Ù†Ø¬Ø§Ø­", "ØªÙ…Øª Ø§Ù„Ø¹Ù…Ù„ÙŠØ© Ø¨Ù†Ø¬Ø§Ø­",
                 "ØªÙ… Ø¥Ù„ØºØ§Ø¡ Ø§Ù„Ø·Ù„Ø¨", "ØªÙ… Ø§Ù„ØºØ§Ø¡ Ø§Ù„Ø·Ù„Ø¨"]
    if any(x in t for x in completed):
        return None

    patterns = [
        {
            "keys": ["Ø®Ø±Ø¬ Ù„Ù„ØªÙˆØµÙŠÙ„", "Ù‚ÙŠØ¯ Ø§Ù„ØªÙˆØµÙŠÙ„", "Ø³ÙŠØªÙ… Ø§Ù„ØªÙˆØµÙŠÙ„", "Ù…ÙˆØ¹Ø¯ Ø§Ù„ØªØ³Ù„ÙŠÙ…", "Ø´Ø­Ù†Ø©", "Ø§Ù„Ø´Ø­Ù†Ø©"],
            "type": "Ø·Ù„Ø¨ / Ø´Ø­Ù†Ø©",
            "action": "Ù…ØªØ§Ø¨Ø¹Ø© ÙˆØµÙˆÙ„ Ø§Ù„Ø·Ù„Ø¨ ÙˆØ§Ù„ØªØ¯Ø®Ù„ Ù„Ùˆ Ø§Ù„Ù…ÙˆØ¹Ø¯ Ø¹Ø¯Ù‰ Ø¨Ø¯ÙˆÙ† ØªØ³Ù„ÙŠÙ…",
            "watch_for": "DELIVERY",
        },
        {
            "keys": ["ØµÙŠØ§Ù†Ø©", "Ø§Ù„ÙÙ†ÙŠ", "Ø²ÙŠØ§Ø±Ø© ÙÙ†ÙŠ"],
            "type": "ØµÙŠØ§Ù†Ø©",
            "action": "Ù…ØªØ§Ø¨Ø¹Ø© Ø­Ø¶ÙˆØ± Ø§Ù„ÙÙ†ÙŠ Ø«Ù… Ø§Ù„ØªØ£ÙƒØ¯ Ø¥Ù† Ø§Ù„Ù…Ø´ÙƒÙ„Ø© Ø§ØªØ­Ù„Øª",
            "watch_for": "SERVICE_OUTCOME",
        },
        {
            "keys": ["ØªÙ… Ø§Ù„Ø­Ø¬Ø²", "ØªØ£ÙƒÙŠØ¯ Ø§Ù„Ø­Ø¬Ø²", "Ù…ÙˆØ¹Ø¯Ùƒ", "Ù…ÙˆØ¹Ø¯ "],
            "type": "Ø­Ø¬Ø² / Ù…ÙˆØ¹Ø¯",
            "action": "Ù…ØªØ§Ø¨Ø¹Ø© Ø§Ù„Ù…ÙˆØ¹Ø¯ ÙˆØ£ÙŠ ØªØºÙŠÙŠØ± Ø£Ùˆ Ø¥Ù„ØºØ§Ø¡ Ø«Ù… ØªØ£ÙƒÙŠØ¯ Ø§Ù„Ù†ØªÙŠØ¬Ø©",
            "watch_for": "APPOINTMENT",
        },
        {
            "keys": ["ØªÙ… Ø§Ù„ØªØ­ÙˆÙŠÙ„", "ØªØ­ÙˆÙŠÙ„ Ù…Ø¨Ù„Øº", "Ø­ÙˆØ§Ù„Ø©"],
            "type": "ØªØ­ÙˆÙŠÙ„",
            "action": "Ø§Ù„ØªØ£ÙƒØ¯ Ù…Ù† ÙˆØµÙˆÙ„ Ø§Ù„ØªØ­ÙˆÙŠÙ„ Ù„Ù„Ø·Ø±Ù Ø§Ù„Ù…Ù‚ØµÙˆØ¯",
            "watch_for": "RECEIPT_CONFIRMATION",
        },
        {
            "keys": ["ÙØ§ØªÙˆØ±Ø©", "Ù…Ø³ØªØ­Ù‚", "Ø§Ø³ØªØ­Ù‚Ø§Ù‚", "ÙŠØ±Ø¬Ù‰ Ø§Ù„Ø³Ø¯Ø§Ø¯"],
            "type": "ÙØ§ØªÙˆØ±Ø©",
            "action": "Ù…ØªØ§Ø¨Ø¹Ø© Ù…ÙˆØ¹Ø¯ Ø§Ù„Ø§Ø³ØªØ­Ù‚Ø§Ù‚ ÙˆØªØ£ÙƒÙŠØ¯ Ø§Ù„Ø³Ø¯Ø§Ø¯",
            "watch_for": "DUE_DATE",
        },
        {
            "keys": ["ØªÙ… ØªØ£ÙƒÙŠØ¯ Ø·Ù„Ø¨", "ØªÙ… Ø§Ø³ØªÙ„Ø§Ù… Ø·Ù„Ø¨Ùƒ", "Ø±Ù‚Ù… Ø§Ù„Ø·Ù„Ø¨"],
            "type": "Ø·Ù„Ø¨ Ø´Ø±Ø§Ø¡",
            "action": "Ù…ØªØ§Ø¨Ø¹Ø© Ø§Ù„Ø·Ù„Ø¨ Ù„Ø­Ø¯ Ø§Ù„ØªØ³Ù„ÙŠÙ… ÙˆØªØ£ÙƒÙŠØ¯ Ø§Ù„Ù†ØªÙŠØ¬Ø©",
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
        r'\b(?:Ø§Ù„Ø£Ø­Ø¯|Ø§Ù„Ø§Ø­Ø¯|Ø§Ù„Ø¥Ø«Ù†ÙŠÙ†|Ø§Ù„Ø§Ø«Ù†ÙŠÙ†|Ø§Ù„Ø«Ù„Ø§Ø«Ø§Ø¡|Ø§Ù„Ø£Ø±Ø¨Ø¹Ø§Ø¡|Ø§Ù„Ø§Ø±Ø¨Ø¹Ø§Ø¡|Ø§Ù„Ø®Ù…ÙŠØ³|Ø§Ù„Ø¬Ù…Ø¹Ø©|Ø§Ù„Ø³Ø¨Øª)\b',
        r'\b(?:Ø§Ù„ÙŠÙˆÙ…|ØºØ¯Ø§|ØºØ¯Ø§Ù‹|Ø¨ÙƒØ±Ø©)\b',
    ]
    time_patterns = [
        r'\b\d{1,2}:\d{2}\s*(?:Øµ|Ù…|am|pm)?\b',
        r'\bØ§Ù„Ø³Ø§Ø¹Ø©\s+\d{1,2}(?::\d{2})?\s*(?:Øµ|Ù…)?\b',
    ]
    ref_patterns = [
        r'(?:Ø±Ù‚Ù… Ø§Ù„Ø·Ù„Ø¨|order\s*#?|Ø·Ù„Ø¨ Ø±Ù‚Ù…)\s*[:#-]?\s*([A-Za-z0-9-]+)'
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
    if any(x in t for x in ["ØªÙ… Ø§Ù„ØªØ³Ù„ÙŠÙ…", "ØªÙ… Ø§Ù„Ø§Ø³ØªÙ„Ø§Ù…"]): return "DELIVERED"
    if any(x in t for x in ["Ø®Ø±Ø¬ Ù„Ù„ØªÙˆØµÙŠÙ„", "Ù‚ÙŠØ¯ Ø§Ù„ØªÙˆØµÙŠÙ„"]): return "OUT_FOR_DELIVERY"
    if any(x in t for x in ["ØªÙ… Ø§Ù„Ø´Ø­Ù†", "ØªÙ… Ø¥Ø±Ø³Ø§Ù„ Ø§Ù„Ø´Ø­Ù†Ø©"]): return "SHIPPED"
    if any(x in t for x in ["ØªÙ… ØªØ£ÙƒÙŠØ¯ Ø·Ù„Ø¨", "ØªÙ… Ø§Ø³ØªÙ„Ø§Ù… Ø·Ù„Ø¨Ùƒ"]): return "CONFIRMED"
    if any(x in t for x in ["ØªÙ… Ø¥Ù„ØºØ§Ø¡", "ØªÙ… Ø§Ù„ØºØ§Ø¡"]): return "CANCELLED"
    if any(x in t for x in ["Ø§Ù„ÙÙ†ÙŠ ÙÙŠ Ø§Ù„Ø·Ø±ÙŠÙ‚", "Ø§Ù„ÙÙ†ÙŠ Ù…ØªØ¬Ù‡"]): return "TECHNICIAN_EN_ROUTE"
    if any(x in t for x in ["ØªÙ…Øª Ø§Ù„ØµÙŠØ§Ù†Ø©", "ØªÙ… Ø§Ù„Ø¥ØµÙ„Ø§Ø­", "ØªÙ… Ø§Ù„Ø§ØµÙ„Ø§Ø­"]): return "SERVICE_COMPLETED"
    if any(x in t for x in ["ØªÙ… Ø§Ù„ØªØ­ÙˆÙŠÙ„"]): return "TRANSFER_SENT"
    if any(x in t for x in ["ØªÙ… Ø§Ø³ØªÙ„Ø§Ù… Ø§Ù„ØªØ­ÙˆÙŠÙ„", "ÙˆØµÙ„ Ø§Ù„ØªØ­ÙˆÙŠÙ„"]): return "TRANSFER_RECEIVED"
    return "UPDATE"

def find_existing_transaction(db, detected, raw_text):
    """Conservative prototype matching:
    1) exact external reference wins;
    2) otherwise only match latest same transaction type when text overlaps enough.
    """
    ref = detected.get("reference")
    if ref:
        row = db.query(DetectedTransaction).filter(
            DetectedTransaction.source_ref == ref
        ).order_by(DetectedTransaction.id.desc()).first()
        if row:
            return row

    candidates = db.query(DetectedTransaction).filter(
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
        ("Ø·Ù„Ø¨ / Ø´Ø­Ù†Ø©", "OUT_FOR_DELIVERY", "Ù„Ùˆ Ù…ÙÙŠØ´ ØªØ³Ù„ÙŠÙ… Ø¨Ø¹Ø¯ Ø§Ù„ÙˆÙ‚Øª Ø§Ù„Ù…ØªÙˆÙ‚Ø¹ØŒ Ø§Ø¹ØªØ¨Ø±Ù‡Ø§ Ù…ØªØ§Ø¨Ø¹Ø© Ù…ØªØ£Ø®Ø±Ø©", 180),
        ("Ø·Ù„Ø¨ / Ø´Ø­Ù†Ø©", "DELIVERED", "Ø§Ø³Ø£Ù„ Ø§Ù„Ø¹Ù…ÙŠÙ„ Ù‡Ù„ Ø§Ù„Ø§Ø³ØªÙ„Ø§Ù… ØªÙ… ÙƒÙˆÙŠØ³ ÙˆÙ‡Ù„ ÙÙŠÙ‡ Ù…Ø´ÙƒÙ„Ø©", 30),
        ("Ø·Ù„Ø¨ Ø´Ø±Ø§Ø¡", "DELIVERED", "Ø£ÙƒØ¯ Ø§Ù„Ù†ØªÙŠØ¬Ø© Ù…Ø¹ Ø§Ù„Ø¹Ù…ÙŠÙ„ Ø¨Ø¯Ù„ Ø¥ØºÙ„Ø§Ù‚ Ø§Ù„Ø·Ù„Ø¨ ØªÙ„Ù‚Ø§Ø¦ÙŠÙ‹Ø§", 30),
        ("ØµÙŠØ§Ù†Ø©", "TECHNICIAN_EN_ROUTE", "ØªØ§Ø¨Ø¹ Ø­Ø¶ÙˆØ± Ø§Ù„ÙÙ†ÙŠ", 120),
        ("ØµÙŠØ§Ù†Ø©", "SERVICE_COMPLETED", "Ø§Ø³Ø£Ù„ Ù‡Ù„ Ø§Ù„Ù…Ø´ÙƒÙ„Ø© Ø§ØªØ­Ù„Øª ÙØ¹Ù„Ù‹Ø§", 60),
        ("ØªØ­ÙˆÙŠÙ„", "TRANSFER_SENT", "ØªØ§Ø¨Ø¹ ØªØ£ÙƒÙŠØ¯ ÙˆØµÙˆÙ„ Ø§Ù„ØªØ­ÙˆÙŠÙ„", 120),
        ("Ø­Ø¬Ø² / Ù…ÙˆØ¹Ø¯", "CONFIRMED", "ØªØ§Ø¨Ø¹ Ø§Ù„Ù…ÙˆØ¹Ø¯ ÙˆØ£ÙŠ ØªØºÙŠÙŠØ±", 1440),
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
    return templates.TemplateResponse("scan.html", {"request": request})

@app.post("/inbox-scan", response_class=HTMLResponse)
def simulate_scan(source_text: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    detected = detect_followup_candidate(source_text)

    # Completed messages may still be updates to an existing case.
    if not detected:
        # Try extracting a reference from completion/update text.
        ref_match = re.search(r'(?:Ø±Ù‚Ù… Ø§Ù„Ø·Ù„Ø¨|order\s*#?|Ø·Ù„Ø¨ Ø±Ù‚Ù…)\s*[:#-]?\s*([A-Za-z0-9-]+)', source_text, re.I)
        existing = None
        if ref_match:
            existing = db.query(DetectedTransaction).filter(
                DetectedTransaction.source_ref == ref_match.group(1)
            ).order_by(DetectedTransaction.id.desc()).first()
        if existing:
            evt = apply_transaction_update(db, existing, source_text)
            return templates.TemplateResponse("scan_result.html",
                {"request": request, "detected": None, "updated": existing, "event_type": evt})
        return templates.TemplateResponse("scan_result.html",
            {"request": request, "detected": None, "updated": None, "event_type": None})

    existing = find_existing_transaction(db, detected, source_text)
    if existing:
        evt = apply_transaction_update(db, existing, source_text)
        if detected.get("expected_at"):
            existing.expected_at = detected["expected_at"]
            db.commit()
        return templates.TemplateResponse("scan_result.html",
            {"request": request, "detected": None, "updated": existing, "event_type": evt})

    created = DetectedTransaction(
        source="prototype_input", raw_text=source_text,
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
    s = db.query(DetectedTransaction).filter(DetectedTransaction.id == suggestion_id).first()
    if not s: raise HTTPException(404, "Suggestion not found")
    if s.status != "SUGGESTED": raise HTTPException(400, "Suggestion already handled")
    case = ExternalCase(source_text=s.raw_text, title=s.title,
                        expected_at=s.expected_at, status="FOLLOWING", outcome_status="OPEN")
    db.add(case)
    s.status="ACCEPTED"
    db.commit(); db.refresh(case)
    return templates.TemplateResponse("external_case.html", {"request": request, "case": case})

@app.post("/suggestions/{suggestion_id}/dismiss")
def dismiss_suggestion(suggestion_id: int, db: Session = Depends(get_db)):
    s = db.query(DetectedTransaction).filter(DetectedTransaction.id == suggestion_id).first()
    if not s: raise HTTPException(404, "Suggestion not found")
    s.status="DISMISSED"; db.commit()
    learn_capability_from_offer(db, offer, req)
    create_payment_intent_for_case(db, case)
    return RedirectResponse(url="/", status_code=303)


@app.post("/followups/{task_id}/done")
def followup_done(task_id: int, db: Session = Depends(get_db)):
    task = db.query(FollowupTask).filter(FollowupTask.id == task_id).first()
    if not task: raise HTTPException(404, "Follow-up not found")
    task.status="DONE"; db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/preferences/{pref_id}/confirm")
def confirm_preference(pref_id: int, db: Session = Depends(get_db)):
    p = db.query(LearnedPreference).filter(LearnedPreference.id == pref_id).first()
    if not p: raise HTTPException(404, "Preference not found")
    p.status="CONFIRMED"; p.confidence=1.0; db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/preferences/{pref_id}/reject")
def reject_preference(pref_id: int, db: Session = Depends(get_db)):
    p = db.query(LearnedPreference).filter(LearnedPreference.id == pref_id).first()
    if not p: raise HTTPException(404, "Preference not found")
    db.delete(p); db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/cases/{case_id}/issue", response_class=HTMLResponse)
def open_issue(case_id: int, issue_text: str = Form(...), request: FastAPIRequest = None, db: Session = Depends(get_db)):
    case = db.query(ExecutionCase).filter(ExecutionCase.id == case_id).first()
    if not case: raise HTTPException(404, "Case not found")
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
    issue=db.query(IssueRecord).filter(IssueRecord.id==issue_id).first()
    if not issue: raise HTTPException(404,"Issue not found")
    case=db.query(ExecutionCase).filter(ExecutionCase.id==issue.execution_case_id).first()
    if result=="ok":
        issue.status="VERIFIED_RESOLVED"
        case.status="VERIFIED_OUTCOME"; case.outcome_status="VERIFIED"
        req=db.query(Request).filter(Request.id==case.request_id).first()
        req.status="VERIFIED_OUTCOME"
        update_business_outcome(db,case,True)
        db.add(CaseEvent(case_id=case.id,event_type="ISSUE_RESOLUTION_VERIFIED",detail=issue.resolution_text))
    else:
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
def amendment_decision(amendment_id: int, decision: str = Form(...), db: Session = Depends(get_db)):
    a=db.query(OfferAmendment).filter(OfferAmendment.id==amendment_id).first()
    if not a: raise HTTPException(404,"Amendment not found")
    offer=db.query(Offer).filter(Offer.id==a.offer_id).first()
    case=db.query(ExecutionCase).filter(ExecutionCase.offer_id==offer.id).first()
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
def route_existing_request(request_id:int, db:Session=Depends(get_db)):
    req=db.query(Request).filter(Request.id==request_id).first()
    if not req: raise HTTPException(404,"Request not found")
    businesses=db.query(Business).all()
    for biz in businesses:
        exists=db.query(ReachAttempt).filter(
            ReachAttempt.request_id==req.id,ReachAttempt.business_id==biz.id
        ).first()
        if not exists: route_request_to_business(db,req,biz)
    return RedirectResponse(url="/supply",status_code=303)

@app.get("/businesses/{business_id}/capability", response_class=HTMLResponse)
def business_capability(business_id:int, request:FastAPIRequest, db:Session=Depends(get_db)):
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
    eps=db.query(IntegrationEndpoint).order_by(IntegrationEndpoint.id.desc()).all()
    pays=db.query(PaymentIntent).order_by(PaymentIntent.id.desc()).limit(50).all()
    return templates.TemplateResponse("integrations.html",{"request":request,"eps":eps,"pays":pays})


@app.get("/ops", response_class=HTMLResponse)
def ops_dashboard(request:FastAPIRequest, db:Session=Depends(get_db)):
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
def create_consent(subject_type:str=Form(...),subject_ref:str=Form(...),
                   consent_type:str=Form(...),source:str=Form("USER_ACTION"),
                   db:Session=Depends(get_db)):
    row=ConsentRecord(subject_type=subject_type,subject_ref=subject_ref,
                      consent_type=consent_type,status="GRANTED",source=source)
    db.add(row); db.commit()
    audit(db,"CONSENT_GRANTED","ConsentRecord",row.id,f"{subject_type}:{consent_type}")
    return RedirectResponse(url="/ops",status_code=303)

@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request:FastAPIRequest):
    return templates.TemplateResponse("privacy.html",{"request":request})


@app.exception_handler(404)
async def not_found_handler(request, exc):
    return templates.TemplateResponse("error.html",{"request":request,"title":"Ù…Ø´ Ù„Ø§Ù‚ÙŠ Ø§Ù„ØµÙØ­Ø©","message":"Ø§Ù„Ø±Ø§Ø¨Ø· Ø¯Ù‡ Ù…Ø´ Ù…ÙˆØ¬ÙˆØ¯ Ø£Ùˆ Ø§Ù†ØªÙ‡Ù‰."},status_code=404)

@app.exception_handler(500)
async def server_error_handler(request, exc):
    return templates.TemplateResponse("error.html",{"request":request,"title":"Ø­ØµÙ„Øª Ù…Ø´ÙƒÙ„Ø©","message":"Ø§Ù„Ø·Ù„Ø¨ Ù…Ø­ÙÙˆØ¸ Ù‚Ø¯Ø± Ø§Ù„Ø¥Ù…ÙƒØ§Ù†. Ø¬Ø±Ù‘Ø¨ ØªØ§Ù†ÙŠ Ø£Ùˆ Ø±Ø§Ø¬Ø¹ Ù„ÙˆØ­Ø© Ø§Ù„ØªØ´ØºÙŠÙ„."},status_code=500)


@app.post("/api/mobile/source")
def mobile_source(source:str=Form(...), body:str=Form(...),
                  package_name:str=Form(""), title:str=Form(""),
                  db:Session=Depends(get_db)):
    row,created=ingest_mobile_source(db,source,body,package_name or None,title or None)
    # Feed the same conservative transaction detector already used by prototype flows.
    # We deliberately return a suggestion candidate instead of auto-following.
    candidate=detect_followup_candidate(body)
    return {
        "ok": True, "created": created, "source_event_id": row.id,
        "followup_candidate": candidate
    }

@app.post("/api/mobile/share")
def mobile_share(text:str=Form(...), db:Session=Depends(get_db)):
    row,created=ingest_mobile_source(db,"SHARE",text)
    return {"ok":True,"created":created,"source_event_id":row.id,"open_url":"/share"}

@app.get("/mobile/setup", response_class=HTMLResponse)
def mobile_setup(request:FastAPIRequest):
    return templates.TemplateResponse("mobile_setup.html",{"request":request})

