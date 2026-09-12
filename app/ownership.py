"""Anonymous customer capability validation and ownership helpers.

The customer reference is a bearer capability, not a display identifier.  Only
canonical RFC-4122 version-4 UUIDs are accepted, giving each browser a random,
unguessable namespace without requiring an account at this stage.
"""

from __future__ import annotations

import hmac
import os
import re
import uuid

from fastapi import HTTPException, Request as HTTPRequest


CUSTOMER_COOKIE = "maak_customer_ref_v1"
CUSTOMER_HEADER = "x-customer-ref"
_SOURCE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,39}$")


def canonical_customer_ref(value: str | None) -> str | None:
    """Return a canonical UUID4 capability, or ``None`` for invalid input."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip().lower()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122 or str(parsed) != raw:
        return None
    return raw


def require_customer_ref(value: str | None) -> str:
    customer_ref = canonical_customer_ref(value)
    if customer_ref is None:
        raise HTTPException(422, "A valid anonymous customer capability is required")
    return customer_ref


def customer_ref_from_request(
    request: HTTPRequest,
    explicit: str | None = None,
    *,
    required: bool = True,
) -> str | None:
    """Resolve one consistent capability from body/header/cookie inputs.

    Multiple transport locations are supported for HTML forms and JSON clients,
    but they must agree.  A conflicting cookie and request value is rejected to
    prevent accidentally operating on another browser profile's data.
    """
    state_value = getattr(request.state, "customer_ref", None)
    raw_values = [
        state_value,
        explicit,
        request.headers.get(CUSTOMER_HEADER),
        request.cookies.get(CUSTOMER_COOKIE),
    ]
    supplied = [value for value in raw_values if value not in (None, "")]
    if not supplied:
        if required:
            raise HTTPException(401, "Anonymous customer capability is required")
        return None

    normalized = [canonical_customer_ref(value) for value in supplied]
    if any(value is None for value in normalized):
        if required:
            raise HTTPException(422, "Invalid anonymous customer capability")
        return None
    if len(set(normalized)) != 1:
        raise HTTPException(403, "Conflicting customer capabilities")
    return normalized[0]


def is_execution_admin(request: HTTPRequest | None) -> bool:
    """Honor the existing execution administrator bearer-token contract."""
    if request is None:
        return False
    secret = os.getenv("MAAK_EXECUTION_ADMIN_TOKEN", "")
    provided = request.headers.get("authorization", "")
    return bool(secret) and hmac.compare_digest(provided, "Bearer " + secret)


def normalize_source_type(value: str) -> str:
    source = (value or "").strip().upper().replace(" ", "_")
    if not _SOURCE_RE.fullmatch(source):
        raise HTTPException(422, "Invalid external source type")
    return source


def source_consent_type(source: str) -> str:
    return f"EXTERNAL_SOURCE:{normalize_source_type(source)}"
