import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from internal_auth import verify_request_context  # noqa: E402


def test_api_ai_rejects_an_api_core_assertion():
    payload = {
        "version": 1,
        "uid": "user-1",
        "authority": "better-auth",
        "requestId": "request-1",
        "audience": "api-core",
        "assertionId": "assertion-1",
        "issuedAt": 100,
        "expiresAt": 160,
        "method": "POST",
        "path": "/v1/embeddings",
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(
        hmac.new(b"test-secret", encoded.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")

    assert (
        verify_request_context(
            encoded,
            signature,
            "test-secret",
            audience="api-ai",
            method="POST",
            path="/v1/embeddings",
            now=120,
        )
        is None
    )
