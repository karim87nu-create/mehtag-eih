"""Meta WhatsApp Cloud API transport with signed webhook evidence.

An API message id is provider acceptance only. A delivered status is device/channel
delivery only. Only an inbound supplier message is a human acknowledgement.
"""
import hashlib
import hmac
import json
import os
import re
import httpx

PHONE_RE = re.compile(r'^[1-9][0-9]{7,14}$')

class WhatsAppRejected(ValueError):
    pass

def normalize_phone(value):
    phone = re.sub(r'[^0-9]', '', value or '')
    if not PHONE_RE.fullmatch(phone):
        raise WhatsAppRejected('INVALID_E164_PHONE')
    return phone

def configured():
    return all(os.getenv(name) for name in (
        'MAAK_WHATSAPP_PHONE_NUMBER_ID', 'MAAK_WHATSAPP_ACCESS_TOKEN',
        'MAAK_WHATSAPP_APP_SECRET', 'MAAK_WHATSAPP_VERIFY_TOKEN',
        'MAAK_WHATSAPP_TEMPLATE'))

def send_template(recipient, request_id, item, area, reply_url, *, client=None):
    if not configured():
        raise WhatsAppRejected('WHATSAPP_NOT_CONFIGURED')
    phone = normalize_phone(recipient)
    version = os.getenv('MAAK_WHATSAPP_GRAPH_VERSION', 'v23.0')
    number_id = os.environ['MAAK_WHATSAPP_PHONE_NUMBER_ID']
    payload = {
        'messaging_product':'whatsapp', 'to':phone, 'type':'template',
        'template':{
            'name':os.environ['MAAK_WHATSAPP_TEMPLATE'],
            'language':{'code':os.getenv('MAAK_WHATSAPP_TEMPLATE_LANGUAGE','ar')},
            'components':[{'type':'body','parameters':[
                {'type':'text','text':str(request_id)},
                {'type':'text','text':(item or 'طلب')[:200]},
                {'type':'text','text':(area or 'غير محدد')[:200]},
                {'type':'text','text':reply_url[:1000]},
            ]}],
        },
    }
    session = client or httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False)
    close = client is None
    try:
        response = session.post(
            f'https://graph.facebook.com/{version}/{number_id}/messages', json=payload,
            headers={'Authorization':'Bearer '+os.environ['MAAK_WHATSAPP_ACCESS_TOKEN']})
        if not 200 <= response.status_code < 300:
            raise WhatsAppRejected(f'HTTP_{response.status_code}')
        if len(response.content) > 32768:
            raise WhatsAppRejected('RESPONSE_TOO_LARGE')
        data = response.json()
        message_id = ((data.get('messages') or [{}])[0]).get('id')
        if not isinstance(message_id, str) or not message_id.startswith('wamid.'):
            raise WhatsAppRejected('INVALID_PROVIDER_ACCEPTANCE')
        return message_id
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        if isinstance(exc, WhatsAppRejected):
            raise
        raise WhatsAppRejected(type(exc).__name__) from exc
    finally:
        if close:
            session.close()

def verify_webhook_signature(raw_body, signature_header):
    secret = os.getenv('MAAK_WHATSAPP_APP_SECRET', '')
    if not secret or not signature_header.startswith('sha256='):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest('sha256='+expected, signature_header)

def parse_events(payload):
    events=[]
    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value=change.get('value') or {}
            for status in value.get('statuses', []):
                if status.get('id') and status.get('status'):
                    events.append({'kind':'status','message_id':status['id'],'status':status['status']})
            for message in value.get('messages', []):
                context=(message.get('context') or {}).get('id')
                if message.get('id') and context:
                    events.append({'kind':'reply','message_id':message['id'],'context_id':context,
                                   'from':message.get('from'),'type':message.get('type')})
    return events
