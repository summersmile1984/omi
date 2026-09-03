import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from internal_auth import (  # noqa: E402
    create_request_context,
    decode_context,
    verify_request_context,
)


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


def test_internal_context_creation_matches_the_request_bound_verifier():
    signed = create_request_context(
        "user-1",
        "test-secret",
        audience="auth",
        method="get",
        path="/internal/profile",
        request_id="request-2",
        now=100,
    )
    assert signed is not None
    encoded, signature = signed
    context = verify_request_context(
        encoded,
        signature,
        "test-secret",
        audience="auth",
        method="GET",
        path="/internal/profile",
        now=120,
    )
    assert context is not None
    assert context["uid"] == "user-1"
    assert context["authority"] == "internal"
    assert context["requestId"] == "request-2"


def test_internal_context_validates_mcp_oauth_identity_fields():
    secret = "test-secret"

    def verify(payload):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = (
            base64.urlsafe_b64encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
            .decode()
            .rstrip("=")
        )
        return verify_request_context(
            encoded,
            signature,
            secret,
            audience="api-core",
            method="POST",
            path="/v1/mcp/memories/search",
            now=120,
        )

    base = {
        "version": 1,
        "uid": "mcp-user",
        "authority": "mcp-oauth",
        "requestId": "request-1",
        "audience": "api-core",
        "assertionId": "assertion-1",
        "issuedAt": 100,
        "expiresAt": 160,
        "method": "POST",
        "path": "/v1/mcp/memories/search",
        "scopes": ["memories.read"],
        "oauthClientId": "https://client.example/metadata.json",
    }
    assert verify(base)["oauthClientId"] == base["oauthClientId"]
    assert verify({**base, "scopes": ["memories.read", "memories.read"]}) is None
    assert verify({**base, "authority": "better-auth"}) is None


def test_legacy_uid_only_hmac_context_stays_valid_without_gaining_oauth_fields():
    secret = "test-secret"

    def sign(payload):
        encoded = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        )
        signature = (
            base64.urlsafe_b64encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
            .decode()
            .rstrip("=")
        )
        return decode_context(encoded, signature, secret)

    assert sign({"uid": "legacy-user"}) == {"uid": "legacy-user"}
    assert sign({"uid": "legacy-user", "scopes": ["memories.read"]}) is None
    assert sign({"uid": "legacy-user", "oauthClientId": "client"}) is None
