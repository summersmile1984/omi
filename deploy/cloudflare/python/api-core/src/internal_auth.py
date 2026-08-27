import base64
import hashlib
import hmac
import json
from typing import Any


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
