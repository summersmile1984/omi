import base64
import hashlib
import hmac
import json
import time
from typing import Any

MAX_ASSERTION_LIFETIME_SECONDS = 60
CLOCK_SKEW_SECONDS = 5
AUTH_AUTHORITIES = frozenset({"firebase", "better-auth", "internal", "mcp-oauth"})


def _valid_identity_context(context: dict[str, Any]) -> bool:
    authority = context.get("authority")
    if authority not in AUTH_AUTHORITIES:
        return False
    scopes = context.get("scopes")
    client_id = context.get("oauthClientId")
    if authority != "mcp-oauth":
        return scopes is None and client_id is None
    return (
        isinstance(scopes, list)
        and len(scopes) <= 16
        and all(isinstance(scope, str) and 0 < len(scope) <= 128 for scope in scopes)
        and len(scopes) == len(set(scopes))
        and isinstance(client_id, str)
        and 0 < len(client_id) <= 2_048
    )


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
    if (
        not isinstance(context, dict)
        or not isinstance(context.get("uid"), str)
        or not context["uid"]
        or not _valid_identity_context(context)
    ):
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
