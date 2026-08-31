"""Verification for the anonymous public shared-chat assertion.

This is intentionally not an ``AuthContext``. The public route is anonymous;
the assertion only proves that the request crossed the trusted Edge boundary
and carries a bounded opaque rate-limit subject.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any

MAX_ASSERTION_LIFETIME_SECONDS = 60
CLOCK_SKEW_SECONDS = 5
SUBJECT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _decode(value: str) -> dict[str, Any] | None:
    try:
        encoded, signature = value.split(".", 1)
        if not encoded or not signature or value.count(".") != 1:
            return None
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        parsed = json.loads(payload.decode())
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    parsed["_encoded"] = encoded
    parsed["_signature"] = signature
    return parsed


def verify_public_chat_assertion(
    value: str | None,
    secret: str | None,
    *,
    method: str,
    path: str,
    now: int | None = None,
) -> dict[str, Any] | None:
    if not value or not secret or not path.startswith("/"):
        return None
    parsed = _decode(value)
    if parsed is None:
        return None
    encoded = parsed.pop("_encoded")
    signature = parsed.pop("_signature")
    expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    try:
        presented = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected, presented):
        return None
    current = int(time.time()) if now is None else now
    issued_at = parsed.get("issuedAt")
    expires_at = parsed.get("expiresAt")
    if (
        parsed.get("version") != 1
        or parsed.get("kind") != "public-shared-chat"
        or parsed.get("audience") != "api-core"
        or not isinstance(parsed.get("subject"), str)
        or SUBJECT_PATTERN.fullmatch(parsed["subject"]) is None
        or not isinstance(parsed.get("requestId"), str)
        or not parsed["requestId"]
        or not isinstance(parsed.get("assertionId"), str)
        or not parsed["assertionId"]
        or not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or issued_at > current + CLOCK_SKEW_SECONDS
        or expires_at < current
        or expires_at - issued_at > MAX_ASSERTION_LIFETIME_SECONDS
        or parsed.get("method") != method.upper()
        or parsed.get("path") != path
    ):
        return None
    return parsed
