# External execution v1

This is an additive layer on the existing conversation core and SQLite database.
Production entry point: `app.execution_app:app`. Run the existing Railway draft
startup patch **first**, unchanged. No migrations rewrite existing tables. Do not
remove the volume, change DATABASE_URL, or replace the draft implementation.

## Verified facts before the change (2026-09-09)

- GitHub main: 73c92f5; Railway source configuration still reported 013a4a3.
- Latest Railway deployment 4aca9649 was FAILED, but the public /health was 200.
- Live /health reported local-gguf and degraded=false, with zero active integrations.
- Live motorcycle/new/70k/Heliopolis/start sequence created exactly one request;
  thanks created none. Some preceding responses used degraded fallback.
- Draft implementation is injected by MAAK_PATCH_A/B at startup, not in main.
  These values are unavailable through the OAuth tool; preserve them unchanged.

## What this adds

- Real geocoding and category-specific Overpass discovery within 5 km of an area,
  six-hour durable caching and serialized geocoder calls. Existing Google Places
  and general discovery remain fallbacks. No paid API is required for OSM.
- Results carry original OSM source URLs. A lead is not verified inventory, price,
  merchant permission, an offer, or evidence of delivery. OSM coverage is limited.
- A merchant webhook must pass a signed ownership challenge and have explicit
  consent recorded by an authenticated operator. No cold WhatsApp simulation.
- Durable jobs are unique per request/business, claimed atomically. QUEUED is not
  SENT. HTTP 2xx alone is not SENT. Missing/invalid acknowledgements, timeout, or
  interrupted workers are UNCERTAIN and never retried automatically.
- Revocation and closed request states prevent pending dispatch. Outgoing HTTPS
  rejects private/reserved/mixed DNS and redirects; the checked IP is pinned while
  preserving TLS hostname verification. Responses are size-limited.
- Signed, timestamped, deduplicated inbound offers are attributed through the job
  and channel, not caller-supplied customer/request IDs. Invalid, above-budget,
  cross-request and duplicate callbacks are handled explicitly. Existing offers
  must use the existing amendment lifecycle rather than silent overwrite.
- Evidence-backed updates are persisted in the original conversation; the existing
  home page receives a small polling script without replacing its conversation UI.
- Old integration/activation administration cannot turn on unverified transport.

## Merchant protocol

Configure `BASE_URL` as the public HTTPS app URL, or allow RAILWAY_PUBLIC_DOMAIN
fallback. Set a strong `MAAK_EXECUTION_ADMIN_TOKEN` in Railway (never commit it).

An operator POSTs `/api/execution/admin/channels` with a Bearer admin token and:
`business_id`, `endpoint` (public HTTPS), `shared_secret` (at least 32 characters,
previously agreed with that merchant), and `consent_basis` (actual authorization).

The app POSTs `{type: "channel.verify", challenge: "..."}`. The merchant returns
`{signature: HMAC_SHA256(shared_secret, challenge)}` in lowercase hex. Failure
leaves no verified channel. The response returns the channel ID, never its secret.

Future requests discovered for that business are queued. For an existing lead,
POST `/api/execution/admin/queue` with `request_id` and `business_id`. It queues
once and does not broadcast to undiscovered businesses.

Outgoing request JSON includes type `request.created`, idempotency_key, request
constraints, a 24-hour merchant reply link, and callback_url. X-Maak-Signature is
HMAC-SHA256 of the exact UTF-8 JSON bytes. The merchant must deduplicate using
Idempotency-Key and return the same receipt on repeats:

```json
{"accepted":true,"idempotency_key":"echo the request key","message_id":"merchant receipt ID"}
```

This confirms channel acceptance, not human reading or inventory availability.
The merchant can use the existing reply page, or POST to callback_url:

```json
{"event_id":"unique event ID","idempotency_key":"request key","price":65000,"eta":"within two days","notes":"new"}
```

Headers: X-Maak-Timestamp = Unix seconds; X-Maak-Signature = HMAC-SHA256 of
`timestamp + "." + exact raw body`. Timestamp tolerance is 5 minutes. Replays of
an identical event return accepted/duplicate; changed content with the same ID
returns 409. Prices must be finite and positive.

Revoke via POST `/api/execution/admin/channels/{id}/revoke`. Revocation is checked
again at dispatch. Replacing channel keys/endpoints requires an audited operation;
v1 deliberately has no automatic credential replacement or uncertain-send retry.

## Validation and operational limits

Run `python -m pytest -q` with repository-pinned dependencies. Tests use isolated
local SQLite and test merchants only; they must never run against /data/spike.db.
Tests verify honest states, duplicate dispatch, timeouts/crash recovery, ownership
challenge, signatures/replays, budgets, cross-request replies, revoked channels,
SSRF controls, original conversation tests, and UI script insertion.

Use one application worker with the current single Railway volume/replica.
Graceful shutdown/restart may leave an in-flight job UNCERTAIN; this is deliberate.
Never classify it as not delivered or resend blindly. No real merchant endpoint
or SMTP/WhatsApp account was present at initial inspection. Production can search,
but cannot honestly contact a merchant until a verified channel is connected.

Existing prototype uses customer_ref + unpredictable thread ID as a bearer-style
conversation boundary, not full account authentication. This layer preserves it.
Production authentication and protection of the broader legacy admin/read pages
remain separate work. Secrets in verified_channels require protected DB access.

## Real supplier WhatsApp channel

The application now has a direct Meta WhatsApp Cloud API adapter. It is disabled
until all `MAAK_WHATSAPP_*` variables in `.env.example` are configured and the
message template is approved. The approved template must have four body variables,
in order: request ID, item, area, and the 24-hour merchant reply URL.

Configure Meta's webhook callback as:
`https://<BASE_URL>/api/execution/whatsapp/webhook`. Use
`MAAK_WHATSAPP_VERIFY_TOKEN` as the verification token and subscribe to message
and message-status events. Incoming webhook bodies are accepted only with a valid
`X-Hub-Signature-256` generated using `MAAK_WHATSAPP_APP_SECRET`.

An authenticated operator records the supplier's explicit opt-in:

```http
POST /api/execution/admin/whatsapp-channels
Authorization: Bearer <MAAK_EXECUTION_ADMIN_TOKEN>
Content-Type: application/json

{"business_id":123,"recipient":"201000000000","consent_basis":"...actual supplier authorization..."}
```

The state model is deliberately strict:

- `PROVIDER_ACCEPTED`: Meta returned a `wamid`; this is not supplier contact.
- `DELIVERED`: WhatsApp reported delivery; this is still not a human ACK.
- `ACKNOWLEDGED`: that same supplier number replied with a context reference to
  the exact outbound `wamid`; only here may the product say contact is confirmed.
- `UNCERTAIN`: timeout, malformed provider response, or failed delivery. It is not
  retried automatically because the provider may already have accepted the send.

Arbitrary inbound messages, replies from another number, unsigned callbacks, and
statuses for unknown `wamid` values cannot advance a request. Template replies may
acknowledge contact, while a priced offer is still recorded through the existing
signed offer/reply flow and validation rules.

OSM references: https://operations.osmfoundation.org/policies/nominatim/
https://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL
https://wiki.openstreetmap.org/wiki/Tag:shop%3Dmotorcycle
