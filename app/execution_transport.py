"""Verified JSON webhook protocol; public HTTPS only, DNS pinned, no redirects."""
import hashlib
import hmac
import ipaddress
import json
import socket
from urllib.parse import urlsplit
import httpx

class TransportRejected(ValueError):
    pass

def encode(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()

def signature(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

def public_target(endpoint):
    u = urlsplit(endpoint)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.fragment or u.port not in (None, 443):
        raise TransportRejected('PUBLIC_HTTPS_REQUIRED')
    addresses = {r[4][0] for r in socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise TransportRejected('NON_PUBLIC_TARGET')
    address = sorted(addresses)[0]
    host = f'[{address}]' if ':' in address else address
    # Connect to the checked IP, preserving TLS SNI and certificate verification.
    return f'https://{host}{u.path or "/"}' + (f'?{u.query}' if u.query else ''), u.hostname

def post_verified(endpoint, payload, secret, key):
    target, hostname = public_target(endpoint)
    body = encode(payload)
    headers = {'Host': hostname, 'Content-Type': 'application/json',
               'X-Maak-Signature': signature(secret, body), 'Idempotency-Key': key}
    # Direct pinned connection is necessary to prevent proxy DNS re-resolution.
    with httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False) as client:
        with client.stream('POST', target, content=body, headers=headers,
                           extensions={'sni_hostname': hostname}) as response:
            if not 200 <= response.status_code < 300:
                raise TransportRejected(f'HTTP_{response.status_code}')
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 32768:
                    raise TransportRejected('RESPONSE_TOO_LARGE')
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise TransportRejected('INVALID_ACK')
    if not isinstance(result, dict):
        raise TransportRejected('INVALID_ACK')
    return result
