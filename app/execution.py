"""Durable outbound execution. Queued, uncertain, accepted and responded differ."""
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError
from .db import SessionLocal
from .models import (Business, Request, ReachAttempt, MerchantLink, TransportDelivery,
                     ConversationThread, ConversationCaseLink, ConversationMessage, Offer, Event)
from .execution_models import VerifiedChannel, OutboundJob, ExecutionNotice
from .execution_transport import post_verified, TransportRejected
from .locale import resolve_locale
from .ownership import canonical_customer_ref


_worker_state_lock = threading.Lock()
_worker_state = {
    'started_monotonic': None,
    'started_at': None,
    'last_success_monotonic': None,
    'last_success_at': None,
    'last_error_monotonic': None,
    'last_error_at': None,
    'last_error': None,
}


def _utc_timestamp():
    """Return a stable, JSON-ready UTC timestamp without leaking local timezone state."""
    return datetime.utcnow().isoformat(timespec='seconds') + 'Z'


def worker_started(*, monotonic_now=None):
    """Reset the in-process worker heartbeat for a new application lifespan."""
    with _worker_state_lock:
        _worker_state.update(
            started_monotonic=time.monotonic() if monotonic_now is None else monotonic_now,
            started_at=_utc_timestamp(),
            last_success_monotonic=None,
            last_success_at=None,
            last_error_monotonic=None,
            last_error_at=None,
            last_error=None,
        )


def worker_succeeded(*, monotonic_now=None):
    """Record completion of a whole worker iteration, including notice refresh."""
    with _worker_state_lock:
        _worker_state['last_success_monotonic'] = time.monotonic() if monotonic_now is None else monotonic_now
        _worker_state['last_success_at'] = _utc_timestamp()


def worker_failed(error, *, monotonic_now=None):
    """Record the latest loop failure without exposing exception details publicly."""
    with _worker_state_lock:
        _worker_state['last_error_monotonic'] = time.monotonic() if monotonic_now is None else monotonic_now
        _worker_state['last_error_at'] = _utc_timestamp()
        _worker_state['last_error'] = type(error).__name__


def _positive_seconds(env_name, default):
    try:
        value = float(os.getenv(env_name, str(default)))
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else float(default)


def worker_health(*, monotonic_now=None):
    """Return whether the background executor is live, with a bounded startup grace."""
    now = time.monotonic() if monotonic_now is None else monotonic_now
    startup_grace = _positive_seconds('MAAK_EXECUTION_STARTUP_GRACE_SECONDS', 20)
    stale_after = _positive_seconds('MAAK_EXECUTION_STALE_AFTER_SECONDS', 15)
    with _worker_state_lock:
        state = dict(_worker_state)

    started = state.pop('started_monotonic')
    last_success = state.pop('last_success_monotonic')
    last_error = state.pop('last_error_monotonic')
    state.update(
        startup_grace_seconds=startup_grace,
        stale_after_seconds=stale_after,
        last_success_age_seconds=None if last_success is None else max(0.0, round(now - last_success, 3)),
    )
    if started is None:
        state.update(healthy=False, state='NOT_STARTED')
        return state

    in_grace = now - started <= startup_grace
    failed_since_success = last_error is not None and (last_success is None or last_error > last_success)
    if in_grace and last_success is None:
        state.update(healthy=True, state='STARTING')
    elif not in_grace and failed_since_success:
        state.update(healthy=False, state='FAILING')
    elif last_success is None or now - last_success > stale_after:
        state.update(healthy=False, state='STALE')
    else:
        state.update(healthy=True, state='HEALTHY')
    return state


def route_request_to_business(db, req, biz):
    if not canonical_customer_ref(getattr(req, 'customer_ref', None)):
        raise ValueError('REQUEST_OWNER_UNAVAILABLE')
    existing = db.query(ReachAttempt).filter_by(request_id=req.id, business_id=biz.id).first()
    if existing:
        return existing
    channel = db.query(VerifiedChannel).filter_by(business_id=biz.id, status='VERIFIED').first()
    attempt = ReachAttempt(request_id=req.id, business_id=biz.id,
        channel='WEBHOOK' if channel else 'NONE', endpoint=channel.endpoint if channel else None,
        status='PENDING' if channel else 'SKIPPED',
        reason='verified channel; not sent' if channel else 'no verified delivery channel')
    db.add(attempt); db.commit(); db.refresh(attempt)
    return attempt


def deliver_request(db, attempt):
    existing = db.query(OutboundJob).filter_by(request_id=attempt.request_id, business_id=attempt.business_id).first()
    if existing:
        return db.get(TransportDelivery, existing.delivery_id)
    req = db.get(Request, attempt.request_id)
    if not req or not canonical_customer_ref(getattr(req, 'customer_ref', None)):
        delivery = TransportDelivery(
            reach_attempt_id=attempt.id, transport='NONE', status='BLOCKED',
            error='REQUEST_OWNER_UNAVAILABLE',
        )
        attempt.status = 'BLOCKED'; attempt.reason = delivery.error
        db.add(delivery); db.commit(); db.refresh(delivery)
        return delivery
    channel = db.query(VerifiedChannel).filter_by(business_id=attempt.business_id, status='VERIFIED').first()
    delivery = TransportDelivery(reach_attempt_id=attempt.id, transport='WEBHOOK' if channel else 'NONE',
                                 status='QUEUED' if channel else 'FAILED',
                                 error=None if channel else 'NO_VERIFIED_CHANNEL')
    db.add(delivery); db.flush()
    if not channel:
        attempt.status='SKIPPED'; db.commit(); return delivery
    link = db.query(MerchantLink).filter_by(request_id=attempt.request_id, business_id=attempt.business_id).first()
    if not link:
        db.add(MerchantLink(token=uuid.uuid4().hex + uuid.uuid4().hex,
                           request_id=attempt.request_id, business_id=attempt.business_id))
    job = OutboundJob(request_id=attempt.request_id, business_id=attempt.business_id,
                     attempt_id=attempt.id, delivery_id=delivery.id, channel_id=channel.id,
                     endpoint=channel.endpoint, idempotency_key=uuid.uuid4().hex)
    db.add(job)
    attempt.status = 'PENDING'; attempt.reason = 'queued; not sent'
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(OutboundJob).filter_by(request_id=attempt.request_id, business_id=attempt.business_id).one()
        return db.get(TransportDelivery, existing.delivery_id)
    return delivery


def base_url():
    domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', '')
    return os.getenv('BASE_URL', f'https://{domain}' if domain else '').rstrip('/')


def publish(db, request_id, event_key, content):
    req = db.get(Request, request_id)
    owner = canonical_customer_ref(getattr(req, 'customer_ref', None)) if req else None
    if not owner:
        return
    for link in db.query(ConversationCaseLink).filter_by(case_type='REQUEST', case_id=request_id).all():
        thread = db.query(ConversationThread).filter_by(
            id=link.thread_id, customer_ref=owner,
        ).first()
        if not thread:
            continue
        if db.query(ExecutionNotice).filter_by(thread_id=link.thread_id, event_key=event_key).first():
            continue
        db.add(ExecutionNotice(thread_id=link.thread_id, request_id=request_id, event_key=event_key, content=content))
        db.add(ConversationMessage(thread_id=link.thread_id, role='ASSISTANT', content=content,
                                  intent='EXECUTION_UPDATE', provider_metadata='{"provider":"execution-evidence"}'))
        db.flush()


def refresh_notices(db):
    # Includes accepted jobs before a conversation link was committed and merchant form replies.
    for link in db.query(ConversationCaseLink).filter_by(case_type='REQUEST').all():
        req = db.get(Request, link.case_id)
        if not req or not canonical_customer_ref(getattr(req, 'customer_ref', None)):
            continue
        for job in db.query(OutboundJob).filter_by(request_id=req.id).all():
            biz = db.get(Business, job.business_id)
            if job.status == 'SENT':
                publish(db, req.id, f'sent:{job.id}', f'تم إرسال طلبك إلى {biz.name}، والقناة أكدت استلامه. لسه مستنيين العرض.')
            elif job.status in ('UNCERTAIN', 'FAILED', 'BLOCKED'):
                publish(db, req.id, f'outbound:{job.id}:{job.status}',
                        f'لم يتأكد إرسال طلبك إلى {biz.name}. مش هاعتبره تواصل تم، ومش هكرر الإرسال تلقائيًا.')
        for offer in db.query(Offer).filter_by(request_id=req.id).all():
            biz = db.get(Business, offer.business_id)
            digest = hashlib.sha256(json.dumps([offer.price, offer.eta, offer.notes, offer.status], ensure_ascii=False).encode()).hexdigest()[:24]
            constraint = 'داخل الميزانية' if offer.status == 'VALID' else 'خارج شروط الطلب'
            currency = req.currency or resolve_locale(req.locale).currency
            content = f'وصل عرض من {biz.name}: {offer.price:g} {currency}، الموعد: {offer.eta}. {constraint}.'
            if offer.notes:
                content += f' ملاحظات التاجر: {offer.notes}'
            publish(db, req.id, f'offer:{offer.id}:{digest}', content)
    db.commit()


def dispatch_one(job_id):
    with SessionLocal() as db:
        changed = db.query(OutboundJob).filter_by(id=job_id, status='QUEUED').update(
            {'status': 'SENDING', 'started_at': datetime.utcnow()}, synchronize_session=False)
        db.commit()
        if not changed:
            return
        job = db.get(OutboundJob, job_id)
        channel = db.get(VerifiedChannel, job.channel_id)
        req = db.get(Request, job.request_id)
        attempt = db.get(ReachAttempt, job.attempt_id)
        delivery = db.get(TransportDelivery, job.delivery_id)
        link = db.query(MerchantLink).filter_by(request_id=req.id, business_id=job.business_id).first()
        if (not channel or channel.status != 'VERIFIED' or channel.endpoint != job.endpoint
                or not base_url().startswith('https://') or not link
                or not canonical_customer_ref(getattr(req, 'customer_ref', None))
                or req.status not in ('DISCOVERING', 'SUPPLY_FOUND_NO_CHANNEL', 'WAITING_OFFERS', 'NO_REACHABLE_SUPPLY')
                or datetime.utcnow() > link.created_at + timedelta(hours=24)):
            job.status = 'BLOCKED'; job.error = 'CHANNEL_REQUEST_OR_CALLBACK_UNAVAILABLE'
        else:
            payload = {'type':'request.created', 'idempotency_key':job.idempotency_key,
                'request':{'id':req.id,'text':req.raw_text,'item':req.item,'area':req.area,
                           'budget':req.budget,'deadline':req.deadline,
                           'currency':getattr(req, 'currency', None) or resolve_locale(getattr(req, 'locale', None)).currency},
                'reply_url':f'{base_url()}/r/{link.token}',
                'callback_url':f'{base_url()}/api/execution/channels/{channel.id}/offers',
                'expires_at':(link.created_at+timedelta(hours=24)).isoformat()+'Z'}
            try:
                ack = post_verified(job.endpoint, payload, channel.secret, job.idempotency_key)
                if (ack.get('accepted') is not True or ack.get('idempotency_key') != job.idempotency_key
                        or not isinstance(ack.get('message_id'), str) or not ack['message_id'].strip()
                        or len(ack['message_id']) > 200):
                    raise TransportRejected('INVALID_ACK')
                job.status='SENT'; job.provider_ref=ack['message_id']
            except TransportRejected as exc:
                # A malformed or failed response cannot prove the merchant did not receive it.
                job.status='UNCERTAIN'; job.error=str(exc)
            except Exception as exc:
                job.status='UNCERTAIN'; job.error=type(exc).__name__
        job.finished_at=datetime.utcnow()
        delivery.status=job.status; delivery.error=job.error; delivery.provider_ref=job.provider_ref
        if attempt.status != 'RESPONDED':
            attempt.status=job.status; attempt.reason=job.error or 'merchant acknowledged receipt'
        db.refresh(req)  # A fast inbound offer may have advanced the lifecycle during HTTP.
        if job.status=='SENT' and req.status in ('DISCOVERING','SUPPLY_FOUND_NO_CHANNEL','NO_REACHABLE_SUPPLY'):
            req.status='WAITING_OFFERS'
        db.add(Event(request_id=req.id, event_type=f'OUTREACH_{job.status}', detail=f'job={job.id}'))
        db.commit()
        refresh_notices(db)


def recover_interrupted():
    with SessionLocal() as db:
        for job in db.query(OutboundJob).filter_by(status='SENDING').all():
            job.status='UNCERTAIN'; job.error='WORKER_INTERRUPTED'; job.finished_at=datetime.utcnow()
            delivery = db.get(TransportDelivery, job.delivery_id)
            attempt = db.get(ReachAttempt, job.attempt_id)
            if delivery:
                delivery.status='UNCERTAIN'; delivery.error='WORKER_INTERRUPTED'
            if attempt:
                attempt.status='UNCERTAIN'; attempt.reason='WORKER_INTERRUPTED'
        db.commit()


def worker_tick():
    with SessionLocal() as db:
        ids = [j.id for j in db.query(OutboundJob).filter_by(status='QUEUED').order_by(OutboundJob.id).limit(5)]
    for job_id in ids:
        dispatch_one(job_id)
    with SessionLocal() as db:
        refresh_notices(db)
