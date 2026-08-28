import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from internal_auth import verify_request_context  # noqa: E402


def signed_context(secret: str) -> tuple[str, str]:
    raw = json.dumps(
        {
            "version": 1,
            "uid": "user-1",
            "authority": "better-auth",
            "requestId": "request-1",
            "audience": "api-core",
            "assertionId": "assertion-1",
            "issuedAt": 100,
            "expiresAt": 160,
            "method": "GET",
            "path": "/v1/conversations",
        },
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return encoded, base64.urlsafe_b64encode(signature).decode().rstrip("=")


def test_internal_context_is_bound_to_request_and_lifetime():
    encoded, signature = signed_context("test-secret")
    verify = lambda **overrides: verify_request_context(
        encoded,
        signature,
        "test-secret",
        audience=overrides.get("audience", "api-core"),
        method=overrides.get("method", "GET"),
        path=overrides.get("path", "/v1/conversations"),
        now=overrides.get("now", 120),
    )

    assert verify()["uid"] == "user-1"
    assert verify(audience="api-ai") is None
    assert verify(method="POST") is None
    assert verify(path="/v1/conversations/other") is None
    assert verify(now=161) is None
