import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

MAX_ASSERTION_LIFETIME_SECONDS = 60
CLOCK_SKEW_SECONDS = 5


def create_request_context(
    uid: str,
    secret: str | None,
    *,
    audience: str,
    method: str,
    path: str,
    request_id: str,
    authority: str = "internal",
    now: int | None = None,
) -> tuple[str, str] | None:
    """Create the same request-bound HMAC assertion used by TypeScript Workers."""

    if (
        not uid
        or not secret
        or authority not in {"firebase", "better-auth", "internal"}
        or audience not in {"api-core", "api-ai", "auth", "jobs", "realtime"}
        or not path.startswith("/")
        or not request_id
    ):
        return None
    issued_at = int(time.time()) if now is None else now
    context = {
        "uid": uid,
        "authority": authority,
        "requestId": request_id,
        "version": 1,
        "audience": audience,
        "assertionId": str(uuid.uuid4()),
        "issuedAt": issued_at,
        "expiresAt": issued_at + MAX_ASSERTION_LIFETIME_SECONDS,
        "method": method.upper(),
        "path": path,
    }
    payload = json.dumps(context, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()).decode()
    return encoded, signature.rstrip("=")


def decode_context(encoded: str | None, signature: str | None, secret: str | None) -> dict[str, Any] | None:
    if not encoded or not signature or not secret:
        return None
    try:
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        presented = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, presented):
            return None
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        context = json.loads(payload.decode())
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(context, dict) or not isinstance(context.get("uid"), str) or not context["uid"]:
        return None
    return context


def verify_request_context(
    encoded: str | None,
    signature: str | None,
    secret: str | None,
    *,
    audience: str,
    method: str,
    path: str,
    now: int | None = None,
) -> dict[str, Any] | None:
    context = decode_context(encoded, signature, secret)
    if context is None:
        return None
    issued_at = context.get("issuedAt")
    expires_at = context.get("expiresAt")
    current = int(time.time()) if now is None else now
    if (
        context.get("version") != 1
        or context.get("audience") != audience
        or not isinstance(context.get("assertionId"), str)
        or not context["assertionId"]
        or not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or issued_at > current + CLOCK_SKEW_SECONDS
        or expires_at < current
        or expires_at - issued_at > MAX_ASSERTION_LIFETIME_SECONDS
        or context.get("method") != method.upper()
        or context.get("path") != path
    ):
        return None
    return context
