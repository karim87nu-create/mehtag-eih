"""Entry point loaded AFTER the existing Railway conversation patch.
The conversation provider, draft and database configuration remain owned by main.
"""
import asyncio
from contextvars import ContextVar
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import os
import secrets
import re
import time
from datetime import datetime, timedelta
from fastapi import Request as HTTPRequest, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.exc import IntegrityError
from . import main as core
from .db import Base, engine
from .models import Business, Request, Offer, MerchantLink, ReachAttempt, ConversationThread, ConversationCaseLink
from .execution_models import (VerifiedChannel, OutboundJob, ExecutionNotice, IncomingReceipt,
                               WhatsAppChannel, WhatsAppOutbound)
from . import execution
from .execution_discovery import discover_businesses as search_businesses
from .execution_transport import post_verified, signature, TransportRejected
from .whatsapp_transport import normalize_phone, verify_webhook_signature, parse_events

Base.metadata.create_all(bind=engine)
app = core.app
core.route_request_to_business = execution.route_request_to_business
core.deliver_request = execution.deliver_request
_search_error = ContextVar('execution_search_error', default=None)
async def discover_businesses(text, area=None, locale_context=None):
    try:
        return await search_businesses(text, area, locale_context)
    except Exception as exc:
        _search_error.set(type(exc).__name__)
        return []
core.discover_businesses = discover_businesses
_original_start = core.start_request_from_conversation
async def start_request_from_conversation(
    db, text, customer_ref, locale_context=None, source_turn_id=None,
):
    token = _search_error.set(None)
    try:
        row = await _original_start(
            db, text, customer_ref, locale_context, source_turn_id,
        )
        # Complete the execution envelope from the existing aggregated draft text.
        # Do not replace the draft or create another Request.
        if row.budget is None:
            amounts = re.findall(r'(?<![\d.])([0-9٠-٩]+(?:[,.][0-9٠-٩]+)?)\s*(ألف|الف|k|جنيه|EGP)\b', row.raw_text, re.I)
            if len(amounts) == 1:
                amount, unit = amounts[0]
                row.budget = float(amount.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩','0123456789')).replace(',', '')) * (1000 if unit.lower() in ('ألف','الف','k') else 1)
                db.commit()
        if _search_error.get():
            row.status = 'DISCOVERY_UNAVAILABLE'
            core.log_event(db, row.id, 'DISCOVERY_UNAVAILABLE', _search_error.get())
            db.commit()
        return row
    finally:
        _search_error.reset(token)
core.start_request_from_conversation = start_request_from_conversation
_original_card = core.honest_request_card
def honest_request_card(row):
    card = _original_card(row)
    if row.status == 'DISCOVERY_UNAVAILABLE':
        card.update(label='البحث غير متاح مؤقتًا؛ لم يتأكد العثور على جهات أو التواصل', phase='problem')
    return card
core.honest_request_card = honest_request_card

# Legacy administration could previously mark QUEUED as SENT. Real dispatch owns it now.
async def legacy_delivery(attempt_id: int, db=Depends(core.get_db)):
    attempt = db.get(ReachAttempt, attempt_id)
    if not attempt:
        raise HTTPException(404, 'Reach attempt not found')
    delivery = execution.deliver_request(db, attempt)
    return {'status': delivery.status, 'confirmed': delivery.status == 'SENT'}
for route in list(app.router.routes):
    if getattr(route, 'path', '') == '/reach/{attempt_id}/deliver':
        app.router.routes.remove(route)
app.post('/reach/{attempt_id}/deliver')(legacy_delivery)

ADMIN_PATHS = ('/integrations/business/', '/businesses/', '/reach/', '/api/execution/admin/')
@app.middleware('http')
async def execution_boundaries(request: HTTPRequest, call_next):
    path = request.url.path
    if request.method != 'GET' and (path.startswith(ADMIN_PATHS) or (path.startswith('/requests/') and path.endswith('/route'))):
        secret = os.getenv('MAAK_EXECUTION_ADMIN_TOKEN', '')
        provided = request.headers.get('authorization', '')
        if not secret or not hmac.compare_digest(provided, 'Bearer ' + secret):
            return JSONResponse({'detail':'Execution administrator authentication required'}, status_code=403)
    # Prevent old unverified integration forms from enabling a real channel.
    if request.method == 'POST' and (path.startswith('/integrations/business/') or path.endswith('/activate')):
        return JSONResponse({'detail':'Use verified channel registration'}, status_code=409)
    response = await call_next(request)
    if path == '/' and response.status_code == 200:
        body = b''.join([part async for part in response.body_iterator])
        if b'/static/execution.js' not in body:
            body = body.replace(b'</body>', b'<script src="/static/execution.js" defer></script></body>')
        headers = dict(response.headers); headers.pop('content-length', None)
        return HTMLResponse(body, headers=headers, status_code=response.status_code)
    return response

_original_lifespan = app.router.lifespan_context
@asynccontextmanager
async def execution_lifespan(application):
    async with _original_lifespan(application):
        execution.worker_started()
        try:
            execution.recover_interrupted()
        except Exception as exc:
            execution.worker_failed(exc)
            logging.exception('Execution recovery failed')
        async def run():
            while True:
                try:
                    await asyncio.to_thread(execution.worker_tick)
                except Exception as exc:
                    execution.worker_failed(exc)
                    logging.exception('Execution worker iteration failed')
                else:
                    execution.worker_succeeded()
                await asyncio.sleep(2)
        task = asyncio.create_task(run())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
app.router.lifespan_context = execution_lifespan

class ChannelInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    business_id: int
    endpoint: str = Field(min_length=10, max_length=2000)
    shared_secret: str = Field(min_length=32, max_length=512)
    consent_basis: str = Field(min_length=10, max_length=2000)

@app.post('/api/execution/admin/channels')
def register_channel(payload: ChannelInput, db=Depends(core.get_db)):
    if not db.get(Business, payload.business_id):
        raise HTTPException(404, 'Business not found')
    existing = db.query(VerifiedChannel).filter_by(business_id=payload.business_id).first()
    if existing:
        raise HTTPException(409, 'Channel already registered; revoke before replacing through an audited migration')
    challenge = secrets.token_urlsafe(32)
    try:
        ack = post_verified(payload.endpoint, {'type':'channel.verify','challenge':challenge},
                            payload.shared_secret, 'verify-' + challenge)
        if not hmac.compare_digest(str(ack.get('signature', '')), signature(payload.shared_secret, challenge.encode())):
            raise TransportRejected('CHALLENGE_FAILED')
    except Exception:
        raise HTTPException(422, 'Endpoint ownership/secret verification failed')
    row = VerifiedChannel(business_id=payload.business_id, endpoint=payload.endpoint,
                          secret=payload.shared_secret, consent_basis=payload.consent_basis)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409, 'Channel already registered')
    return {'id':row.id,'status':row.status,'business_id':row.business_id}

@app.post('/api/execution/admin/channels/{channel_id}/revoke')
def revoke_channel(channel_id:int, db=Depends(core.get_db)):
    row=db.get(VerifiedChannel, channel_id)
    if not row: raise HTTPException(404, 'Channel not found')
    row.status='REVOKED'; db.commit()
    return {'status':row.status}

class QueueInput(BaseModel):
    request_id: int
    business_id: int

class WhatsAppChannelInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    business_id: int
    recipient: str = Field(min_length=8,max_length=32)
    consent_basis: str = Field(min_length=10,max_length=2000)

@app.post('/api/execution/admin/whatsapp-channels')
def register_whatsapp_channel(payload:WhatsAppChannelInput, db=Depends(core.get_db)):
    if not db.get(Business,payload.business_id): raise HTTPException(404,'Business not found')
    if db.query(WhatsAppChannel).filter_by(business_id=payload.business_id).first():
        raise HTTPException(409,'WhatsApp channel already registered')
    try: recipient=normalize_phone(payload.recipient)
    except ValueError: raise HTTPException(422,'Invalid WhatsApp E.164 recipient')
    row=WhatsAppChannel(business_id=payload.business_id,recipient=recipient,
                        consent_basis=payload.consent_basis)
    db.add(row)
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409,'WhatsApp channel already registered')
    return {'id':row.id,'status':row.status,'business_id':row.business_id,
            'recipient_suffix':recipient[-4:]}

@app.post('/api/execution/admin/whatsapp-channels/{channel_id}/revoke')
def revoke_whatsapp_channel(channel_id:int, db=Depends(core.get_db)):
    row=db.get(WhatsAppChannel,channel_id)
    if not row: raise HTTPException(404,'Channel not found')
    row.status='REVOKED'; db.commit(); return {'status':row.status}

@app.post('/api/execution/admin/queue')
def queue_existing(payload:QueueInput, db=Depends(core.get_db)):
    req=db.get(Request,payload.request_id); biz=db.get(Business,payload.business_id)
    if not req or not biz: raise HTTPException(404, 'Request or business not found')
    # Only a lead already discovered for this request, not an arbitrary broadcast.
    attempt=db.query(ReachAttempt).filter_by(request_id=req.id,business_id=biz.id).first()
    if not attempt: raise HTTPException(409,'Business was not discovered for this request')
    if req.status not in ('SUPPLY_FOUND_NO_CHANNEL','WAITING_OFFERS','NO_REACHABLE_SUPPLY'):
        raise HTTPException(409,'Request is no longer seeking offers')
    delivery=execution.deliver_request(db,attempt)
    return {'status':delivery.status,'confirmed':delivery.status=='SENT'}

class IncomingOffer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: str = Field(min_length=1,max_length=200)
    idempotency_key: str = Field(min_length=1,max_length=200)
    price: float = Field(gt=0,allow_inf_nan=False,le=1e12)
    eta: str = Field(min_length=1,max_length=300)
    notes: str = Field(default='',max_length=2000)

@app.post('/api/execution/channels/{channel_id}/offers')
async def receive_offer(channel_id:int, request:HTTPRequest, db=Depends(core.get_db)):
    channel=db.get(VerifiedChannel,channel_id)
    if not channel or channel.status!='VERIFIED': raise HTTPException(403,'Invalid channel')
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>16384: raise HTTPException(413,'Payload too large')
    timestamp=request.headers.get('x-maak-timestamp','')
    try:
        if abs(time.time()-int(timestamp))>300: raise ValueError()
    except ValueError:
        raise HTTPException(401,'Expired or missing signature timestamp')
    expected=signature(channel.secret,timestamp.encode()+b'.'+bytes(raw))
    if not hmac.compare_digest(expected, request.headers.get('x-maak-signature','')):
        raise HTTPException(401,'Invalid signature')
    try:
        payload=IncomingOffer.model_validate_json(raw)
    except ValueError:
        raise HTTPException(422,'Invalid offer payload')
    digest=hashlib.sha256(raw).hexdigest()
    existing=db.query(IncomingReceipt).filter_by(channel_id=channel_id,event_id=payload.event_id).first()
    if existing:
        if existing.payload_hash!=digest: raise HTTPException(409,'Event ID reused with different content')
        return {'accepted':True,'duplicate':True}
    job=db.query(OutboundJob).filter_by(channel_id=channel_id,idempotency_key=payload.idempotency_key).first()
    if not job or job.status not in ('SENDING','SENT','UNCERTAIN'):
        raise HTTPException(409,'No dispatched request matches this reply')
    req=db.get(Request,job.request_id)
    if not req or not core.canonical_customer_ref(getattr(req, 'customer_ref', None)):
        raise HTTPException(409, 'Request owner is unavailable')
    link=db.query(MerchantLink).filter_by(request_id=req.id,business_id=job.business_id).first()
    if not link or datetime.utcnow()>link.created_at+timedelta(hours=24): raise HTTPException(410,'Request link expired')
    if req.status not in ('DISCOVERING','SUPPLY_FOUND_NO_CHANNEL','NO_REACHABLE_SUPPLY','WAITING_OFFERS','OFFER_FOUND'):
        raise HTTPException(409,'Request no longer accepts offers')
    offer=db.query(Offer).filter_by(request_id=req.id,business_id=job.business_id).first()
    if offer:
        raise HTTPException(409,'Offer already exists; use the existing amendment lifecycle')
    valid,status=core.valid_offer(req,payload.price)
    offer=Offer(request_id=req.id,business_id=job.business_id,price=payload.price,
                eta=payload.eta,notes=payload.notes,status=status)
    db.add(offer); db.add(IncomingReceipt(job_id=job.id,channel_id=channel_id,event_id=payload.event_id,payload_hash=digest))
    link.status='RESPONDED'; db.get(ReachAttempt,job.attempt_id).status='RESPONDED'
    if valid: req.status='OFFER_FOUND'
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        receipt=db.query(IncomingReceipt).filter_by(channel_id=channel_id,event_id=payload.event_id).first()
        if receipt and receipt.payload_hash==digest: return {'accepted':True,'duplicate':True}
        raise HTTPException(409,'Concurrent offer conflict')
    execution.refresh_notices(db)
    return {'accepted':True,'offer_id':offer.id,'status':status}

@app.get('/api/execution/health')
def execution_health(db=Depends(core.get_db)):
    worker = execution.worker_health()
    payload = {'version':'execution-v1','persistent_database':str(engine.url.database or '').startswith('/data/'),'verified_channels':db.query(VerifiedChannel).filter_by(status='VERIFIED').count(),
               'verified_whatsapp_channels':db.query(WhatsAppChannel).filter_by(status='VERIFIED').count(),
               'queued':db.query(OutboundJob).filter_by(status='QUEUED').count()+db.query(WhatsAppOutbound).filter_by(status='QUEUED').count(),
               'uncertain':db.query(OutboundJob).filter_by(status='UNCERTAIN').count()+db.query(WhatsAppOutbound).filter_by(status='UNCERTAIN').count(),
               'provider':core.conversation_provider.name,'degraded':core.conversation_provider.degraded,
               'worker':worker}
    if not worker['healthy']:
        return JSONResponse(payload, status_code=503)
    return payload

@app.get('/api/execution/whatsapp/webhook')
def verify_whatsapp_webhook(request:HTTPRequest):
    mode=request.query_params.get('hub.mode'); token=request.query_params.get('hub.verify_token')
    challenge=request.query_params.get('hub.challenge')
    if mode!='subscribe' or not os.getenv('MAAK_WHATSAPP_VERIFY_TOKEN') or not hmac.compare_digest(token or '',os.environ['MAAK_WHATSAPP_VERIFY_TOKEN']):
        raise HTTPException(403,'Webhook verification failed')
    return HTMLResponse(challenge or '',media_type='text/plain')

@app.post('/api/execution/whatsapp/webhook')
async def receive_whatsapp_webhook(request:HTTPRequest, db=Depends(core.get_db)):
    raw=await request.body()
    if len(raw)>262144: raise HTTPException(413,'Payload too large')
    if not verify_webhook_signature(raw,request.headers.get('x-hub-signature-256','')):
        raise HTTPException(401,'Invalid webhook signature')
    try: payload=json.loads(raw)
    except (ValueError,UnicodeError): raise HTTPException(422,'Invalid webhook payload')
    changed=0
    for event in parse_events(payload):
        if event['kind']=='status':
            job=db.query(WhatsAppOutbound).filter_by(provider_ref=event['message_id']).first()
            if not job: continue
            if event['status']=='delivered' and job.status=='PROVIDER_ACCEPTED':
                job.status='DELIVERED'; job.delivered_at=datetime.utcnow(); changed+=1
                delivery=db.get(core.TransportDelivery,job.delivery_id)
                attempt=db.get(ReachAttempt,job.attempt_id)
                if delivery: delivery.status='DELIVERED'
                if attempt: attempt.status='DELIVERED'; attempt.reason='channel delivered; supplier has not acknowledged'
            elif event['status']=='failed' and job.status in ('PROVIDER_ACCEPTED','DELIVERED'):
                job.status='UNCERTAIN'; job.error='WHATSAPP_DELIVERY_FAILED'; changed+=1
                delivery=db.get(core.TransportDelivery,job.delivery_id)
                attempt=db.get(ReachAttempt,job.attempt_id)
                if delivery: delivery.status='UNCERTAIN'; delivery.error=job.error
                if attempt: attempt.status='UNCERTAIN'; attempt.reason=job.error
        elif event['kind']=='reply':
            job=db.query(WhatsAppOutbound).filter_by(provider_ref=event['context_id']).first()
            if not job: continue
            channel=db.get(WhatsAppChannel,job.channel_id)
            try: sender=normalize_phone(event.get('from'))
            except ValueError: continue
            if not channel or sender!=channel.recipient: continue
            if job.status!='ACKNOWLEDGED':
                job.status='ACKNOWLEDGED'; job.acknowledged_at=datetime.utcnow(); changed+=1
                delivery=db.get(core.TransportDelivery,job.delivery_id); attempt=db.get(ReachAttempt,job.attempt_id)
                if delivery: delivery.status='ACKNOWLEDGED'
                if attempt: attempt.status='RESPONDED'; attempt.reason='supplier replied to the outbound WhatsApp message'
                db.add(core.Event(request_id=job.request_id,event_type='WHATSAPP_ACKNOWLEDGED',detail=f'job={job.id}'))
    db.commit()
    if changed: execution.refresh_notices(db)
    return {'accepted':True,'matched_events':changed}
