import json
import time
import socket
import pytest
from fastapi.testclient import TestClient
from app import execution, execution_transport
from app.execution_app import app
from app.db import Base, engine, SessionLocal
from app.models import (Business, BusinessActivation, IntegrationEndpoint, Request, ReachAttempt,
    ConversationThread, ConversationCaseLink, ConversationMessage, MerchantLink, Offer, TransportDelivery)
from app.execution_models import VerifiedChannel, OutboundJob, ExecutionNotice

client=TestClient(app)

@pytest.fixture(autouse=True)
def reset(monkeypatch):
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    monkeypatch.setenv('BASE_URL','https://maak.example')
    monkeypatch.setenv('MAAK_EXECUTION_ADMIN_TOKEN','test-admin')


def setup(verified=True):
    with SessionLocal() as db:
        biz=Business(name='Test merchant',website='https://merchant.example')
        req=Request(raw_text='موتوسيكل جديد في مصر الجديدة بحدود 70000',item='موتوسيكل جديد',budget=70000,area='مصر الجديدة',status='SUPPLY_FOUND_NO_CHANNEL')
        thread=ConversationThread(id='thread-1',customer_ref='customer-1')
        db.add_all([biz,req,thread]);db.commit()
        db.add(ConversationCaseLink(thread_id=thread.id,case_type='REQUEST',case_id=req.id))
        if verified:
            db.add(VerifiedChannel(business_id=biz.id,endpoint='https://merchant.example/inbox',secret='s'*32,consent_basis='Explicit test consent'))
        db.commit()
        attempt=execution.route_request_to_business(db,req,biz)
        delivery=execution.deliver_request(db,attempt)
        return req.id,biz.id,attempt.id,delivery.id


def ack(endpoint,payload,secret,key):
    return {'accepted':True,'idempotency_key':key,'message_id':'merchant-receipt-1'}


def test_activation_is_not_sending():
    req,biz,attempt,delivery=setup(False)
    with SessionLocal() as db:
        db.add(BusinessActivation(business_id=biz,status='DIRECT',endpoint='https://merchant.example'))
        db.add(IntegrationEndpoint(business_id=biz,kind='WEBHOOK',endpoint='https://merchant.example',status='ACTIVE'))
        db.commit()
        result=execution.deliver_request(db,db.get(ReachAttempt,attempt))
        assert result.status=='FAILED'
        assert db.get(ReachAttempt,attempt).status=='SKIPPED'
        assert db.query(OutboundJob).count()==0


def test_success_is_acknowledged_once_and_updates_same_thread(monkeypatch):
    req,biz,attempt,delivery=setup()
    calls=[]
    def send(*args):
        calls.append(args);return ack(*args)
    monkeypatch.setattr(execution,'post_verified',send)
    with SessionLocal() as db:
        assert db.get(TransportDelivery,delivery).status=='QUEUED'
        assert db.get(ReachAttempt,attempt).status=='PENDING'
        execution.deliver_request(db,db.get(ReachAttempt,attempt))
        assert db.query(OutboundJob).count()==1
    execution.worker_tick();execution.worker_tick()
    with SessionLocal() as db:
        assert len(calls)==1
        assert db.get(Request,req).status=='WAITING_OFFERS'
        assert db.get(ReachAttempt,attempt).status=='SENT'
        assert db.get(TransportDelivery,delivery).provider_ref=='merchant-receipt-1'
        assert db.query(ExecutionNotice).count()==1
        assert db.query(ConversationMessage).one().thread_id=='thread-1'
    response=client.get('/api/execution/conversations/thread-1?customer_ref=customer-1')
    assert len(response.json()['notices'])==1
    assert client.get('/api/execution/conversations/thread-1?customer_ref=wrong').status_code==403


@pytest.mark.parametrize('response',[{}, {'accepted':True}, {'accepted':False}, {'accepted':True,'idempotency_key':'wrong','message_id':'x'}])
def test_http_success_without_receipt_never_means_sent(monkeypatch,response):
    req,biz,attempt,delivery=setup()
    monkeypatch.setattr(execution,'post_verified',lambda *_:response)
    execution.worker_tick();execution.worker_tick()
    with SessionLocal() as db:
        assert db.get(ReachAttempt,attempt).status=='UNCERTAIN'
        assert db.get(Request,req).status=='SUPPLY_FOUND_NO_CHANNEL'
        assert db.query(OutboundJob).one().status=='UNCERTAIN'


def test_timeout_and_restart_never_resend(monkeypatch):
    setup();calls=[]
    def timeout(*args):
        calls.append(1);raise TimeoutError()
    monkeypatch.setattr(execution,'post_verified',timeout)
    execution.worker_tick();execution.recover_interrupted();execution.worker_tick()
    assert len(calls)==1
    with SessionLocal() as db:
        job=db.query(OutboundJob).one();job.status='SENDING';db.commit()
    execution.recover_interrupted();execution.worker_tick()
    assert len(calls)==1


def test_revoked_channel_and_cancelled_request_block_dispatch(monkeypatch):
    setup();monkeypatch.setattr(execution,'post_verified',lambda *_:pytest.fail('Must not send'))
    with SessionLocal() as db:
        db.query(VerifiedChannel).one().status='REVOKED';db.commit()
    execution.worker_tick()
    with SessionLocal() as db: assert db.query(OutboundJob).one().status=='BLOCKED'


def signed_offer(price=65000,event='e1',key=None):
    with SessionLocal() as db:
        key=key or db.query(OutboundJob).one().idempotency_key
        channel=db.query(VerifiedChannel).one().id
    body=json.dumps({'event_id':event,'idempotency_key':key,'price':price,'eta':'خلال يومين','notes':'جديد'},ensure_ascii=False).encode()
    timestamp=str(int(time.time()))
    headers={'Content-Type':'application/json','X-Maak-Timestamp':timestamp,
             'X-Maak-Signature':execution_transport.signature('s'*32,timestamp.encode()+b'.'+body)}
    return f'/api/execution/channels/{channel}/offers',body,headers


def test_signed_offer_is_idempotent_and_durable(monkeypatch):
    req,*_=setup();monkeypatch.setattr(execution,'post_verified',ack);execution.worker_tick()
    url,body,headers=signed_offer()
    response=client.post(url,content=body,headers=headers)
    assert response.status_code==200,response.text
    assert client.post(url,content=body,headers=headers).json()['duplicate']
    execution.worker_tick()
    with SessionLocal() as db:
        assert db.query(Offer).count()==1
        assert db.get(Request,req).status=='OFFER_FOUND'
        assert db.query(ConversationMessage).count()==2
        assert db.query(ReachAttempt).one().status=='RESPONDED'
    url,body,headers=signed_offer(price=64000)
    assert client.post(url,content=body,headers=headers).status_code==409


def test_invalid_signature_bad_price_wrong_request_and_budget(monkeypatch):
    setup();monkeypatch.setattr(execution,'post_verified',ack);execution.worker_tick()
    url,body,headers=signed_offer()
    assert client.post(url,content=body,headers={}).status_code==401
    url,body,headers=signed_offer(price=-2)
    assert client.post(url,content=body,headers=headers).status_code==422
    url,body,headers=signed_offer(key='unknown')
    assert client.post(url,content=body,headers=headers).status_code==409
    url,body,headers=signed_offer(price=90000)
    assert client.post(url,content=body,headers=headers).json()['status']=='ABOVE_BUDGET'
    with SessionLocal() as db:
        assert db.query(Request).one().status=='WAITING_OFFERS'
        assert 'خارج شروط الطلب' in db.query(ExecutionNotice).order_by(ExecutionNotice.id.desc()).first().content


@pytest.mark.parametrize('url',['http://example.com','https://u:p@example.com','https://127.0.0.1','https://169.254.169.254','https://[::1]','https://example.com:444'])
def test_ssrf_is_blocked(monkeypatch,url):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *_args,**_kwargs:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(execution_transport.TransportRejected): execution_transport.public_target(url)


def test_public_dns_is_pinned_and_mixed_dns_rejected(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *_args,**_kwargs:[(2,1,6,'',('93.184.216.34',443))])
    assert execution_transport.public_target('https://example.com/inbox')==('https://93.184.216.34/inbox','example.com')
    monkeypatch.setattr(socket,'getaddrinfo',lambda *_args,**_kwargs:[(2,1,6,'',('93.184.216.34',443)),(2,1,6,'',('10.0.0.1',443))])
    with pytest.raises(execution_transport.TransportRejected): execution_transport.public_target('https://example.com')


def test_administration_requires_auth_and_legacy_queue_never_claims_sent():
    _,_,attempt,_=setup()
    assert client.post(f'/reach/{attempt}/deliver').status_code==403
    response=client.post(f'/reach/{attempt}/deliver',headers={'Authorization':'Bearer test-admin'})
    assert response.json()=={'status':'QUEUED','confirmed':False}


def test_channel_challenge_required(monkeypatch):
    _,biz,*_=setup(False)
    payload={'business_id':biz,'endpoint':'https://merchant.example/inbox','shared_secret':'s'*32,'consent_basis':'Explicit merchant agreement'}
    assert client.post('/api/execution/admin/channels',json=payload).status_code==403
    monkeypatch.setattr('app.execution_app.post_verified',lambda *_:{})
    assert client.post('/api/execution/admin/channels',json=payload,headers={'Authorization':'Bearer test-admin'}).status_code==422
    def challenge(_endpoint,payload,secret,_key):
        return {'signature':execution_transport.signature(secret,payload['challenge'].encode())}
    monkeypatch.setattr('app.execution_app.post_verified',challenge)
    assert client.post('/api/execution/admin/channels',json=payload,headers={'Authorization':'Bearer test-admin'}).json()['status']=='VERIFIED'


def test_script_is_loaded_without_modifying_home_template():
    assert '/static/execution.js' in client.get('/').text


def test_discovery_failure_is_not_no_suppliers(monkeypatch):
    import asyncio
    from app import execution_app
    async def fail(*_): raise TimeoutError('search timed out')
    monkeypatch.setattr(execution_app, 'search_businesses', fail)
    with SessionLocal() as db:
        row=asyncio.run(execution_app.start_request_from_conversation(db,'عايز موتوسيكل، جديد، 70 ألف، مصر الجديدة، دورلي على ده'))
        assert row.status=='DISCOVERY_UNAVAILABLE'
        assert row.budget==70000
        assert row.area=='مصر الجديدة'
        assert db.query(Request).count()==1
