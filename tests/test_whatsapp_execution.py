import hashlib
import hmac
import json
import pytest
from fastapi.testclient import TestClient
from app.execution_app import app
from app import execution
from app.db import Base, engine, SessionLocal
from app.models import Business, Request, ConversationThread, ConversationCaseLink, ReachAttempt, TransportDelivery
from app.execution_models import WhatsAppChannel, WhatsAppOutbound

client=TestClient(app)
CUSTOMER='91d99d9d-cd63-4aa5-a075-cb041a425dee'

@pytest.fixture(autouse=True)
def reset(monkeypatch):
    client.cookies.clear(); Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    monkeypatch.setenv('BASE_URL','https://maak.example')
    monkeypatch.setenv('MAAK_EXECUTION_ADMIN_TOKEN','admin-token')
    monkeypatch.setenv('MAAK_WHATSAPP_APP_SECRET','app-secret')
    monkeypatch.setenv('MAAK_WHATSAPP_VERIFY_TOKEN','verify-token')

def setup():
    with SessionLocal() as db:
        biz=Business(name='WhatsApp merchant')
        req=Request(customer_ref=CUSTOMER,raw_text='موتوسيكل جديد',item='موتوسيكل',area='مصر الجديدة',status='SUPPLY_FOUND_NO_CHANNEL')
        thread=ConversationThread(id='wa-thread',customer_ref=CUSTOMER)
        db.add_all([biz,req,thread]); db.commit()
        db.add_all([ConversationCaseLink(thread_id=thread.id,case_type='REQUEST',case_id=req.id),
                    WhatsAppChannel(business_id=biz.id,recipient='201000000000',consent_basis='Explicit supplier opt in')])
        db.commit(); attempt=execution.route_request_to_business(db,req,biz)
        delivery=execution.deliver_request(db,attempt)
        return req.id,biz.id,attempt.id,delivery.id

def signed(payload):
    raw=json.dumps(payload,separators=(',',':')).encode()
    sig=hmac.new(b'app-secret',raw,hashlib.sha256).hexdigest()
    return raw,{'X-Hub-Signature-256':'sha256='+sig,'Content-Type':'application/json'}

def test_registration_requires_admin_and_masks_recipient():
    with SessionLocal() as db:
        biz=Business(name='Supplier'); db.add(biz); db.commit(); business_id=biz.id
    body={'business_id':business_id,'recipient':'+20 100 000 0000','consent_basis':'Explicit supplier opt in'}
    assert client.post('/api/execution/admin/whatsapp-channels',json=body).status_code==403
    response=client.post('/api/execution/admin/whatsapp-channels',json=body,headers={'Authorization':'Bearer admin-token'})
    assert response.status_code==200 and response.json()['recipient_suffix']=='0000'
    assert 'recipient' not in response.json()

def test_provider_acceptance_is_not_contact(monkeypatch):
    req,biz,attempt,delivery=setup()
    monkeypatch.setattr(execution,'send_template',lambda *_:'wamid.provider-1')
    execution.worker_tick()
    with SessionLocal() as db:
        assert db.query(WhatsAppOutbound).one().status=='PROVIDER_ACCEPTED'
        assert db.get(ReachAttempt,attempt).status=='PROVIDER_ACCEPTED'
        assert 'لم يؤكدها بعد' in db.query(execution.ExecutionNotice).one().content

def test_delivery_is_still_not_human_ack_but_reply_is(monkeypatch):
    req,biz,attempt,delivery=setup()
    monkeypatch.setattr(execution,'send_template',lambda *_:'wamid.provider-1'); execution.worker_tick()
    status={'entry':[{'changes':[{'value':{'statuses':[{'id':'wamid.provider-1','status':'delivered'}]}}]}]}
    raw,headers=signed(status); assert client.post('/api/execution/whatsapp/webhook',content=raw,headers=headers).json()['matched_events']==1
    with SessionLocal() as db: assert db.query(WhatsAppOutbound).one().status=='DELIVERED'
    reply={'entry':[{'changes':[{'value':{'messages':[{'id':'wamid.reply-1','from':'201000000000','type':'text','context':{'id':'wamid.provider-1'}}]}}]}]}
    raw,headers=signed(reply); assert client.post('/api/execution/whatsapp/webhook',content=raw,headers=headers).json()['matched_events']==1
    with SessionLocal() as db:
        assert db.query(WhatsAppOutbound).one().status=='ACKNOWLEDGED'
        assert db.get(ReachAttempt,attempt).status=='RESPONDED'

def test_spoofed_or_unrelated_reply_is_ignored(monkeypatch):
    setup(); monkeypatch.setattr(execution,'send_template',lambda *_:'wamid.provider-1'); execution.worker_tick()
    reply={'entry':[{'changes':[{'value':{'messages':[{'id':'wamid.reply','from':'201099999999','type':'text','context':{'id':'wamid.provider-1'}}]}}]}]}
    raw,headers=signed(reply); assert client.post('/api/execution/whatsapp/webhook',content=raw,headers=headers).json()['matched_events']==0
    with SessionLocal() as db: assert db.query(WhatsAppOutbound).one().status=='PROVIDER_ACCEPTED'

def test_webhook_signature_and_verification_are_required():
    assert client.get('/api/execution/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=ok').text=='ok'
    assert client.post('/api/execution/whatsapp/webhook',content=b'{}').status_code==401
