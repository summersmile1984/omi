import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import crisp_routes  # noqa: E402
from crisp_routes import get_crisp_unread  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, payload=None, *, malformed=False):
        self.status = status
        self.payload = payload
        self.malformed = malformed

    async def json(self):
        if self.malformed:
            raise ValueError("malformed")
        return self.payload


class FakeAuth:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FakeRequest:
    def __init__(self, env, headers=None):
        self.scope = {"env": env}
        self.headers = headers or {}


def signed_headers(secret: str, uid: str = "crisp-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "crisp-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        "x-request-id": "crisp-request",
    }


def make_env(secret="crisp-secret", *, configured=True, profile=None):
    auth = FakeAuth(
        FakeResponse(payload=profile if profile is not None else {"uid": "crisp-user", "email": "Support@Example.com"})
    )
    values = {
        "INTERNAL_ASSERTION_SECRET": secret,
        "AUTH": auth,
    }
    if configured:
        values.update(
            {
                "CRISP_PLUGIN_IDENTIFIER": "plugin-id",
                "CRISP_PLUGIN_KEY": "plugin-key",
                "CRISP_WEBSITE_ID": "website-id",
            }
        )
    return type("Env", (), values)(), auth


def test_crisp_unread_requires_edge_auth_and_preserves_unconfigured_empty_shape():
    secret = "crisp-secret"
    env, _ = make_env(secret, configured=False)

    unauthorized = asyncio.run(get_crisp_unread(FakeRequest(env)))
    assert unauthorized.status_code == 401

    response = asyncio.run(get_crisp_unread(FakeRequest(env, signed_headers(secret))))
    assert response == {"unread_count": 0, "messages": []}


def test_crisp_unread_finds_session_and_filters_operator_messages(monkeypatch):
    secret = "crisp-secret"
    env, auth = make_env(secret)
    calls = []
    responses = [
        FakeResponse(
            payload={
                "data": [
                    {"meta": {"email": "support@example.com"}, "session_id": "session-1"},
                ]
            }
        ),
        FakeResponse(
            payload={
                "data": [
                    {"from": "operator", "type": "text", "timestamp": 99, "content": "old"},
                    {"from": "operator", "type": "text", "timestamp": 101, "content": "new"},
                    {"from": "user", "type": "text", "timestamp": 102, "content": "ignore"},
                    {"from": "operator", "type": "text", "timestamp": 103, "content": {"kind": "note"}},
                ]
            }
        ),
    ]

    async def fake_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(crisp_routes, "worker_fetch", fake_fetch)
    # The session lookup is case-insensitive and only the bounded profile email
    # is sent to Auth; Crisp receives only the provider credential header.
    result = asyncio.run(get_crisp_unread(FakeRequest(env, signed_headers(secret)), since=100))

    assert result == {
        "unread_count": 2,
        "messages": [
            {"text": "new", "timestamp": 101, "from": "operator"},
            {"text": '{"kind":"note"}', "timestamp": 103, "from": "operator"},
        ],
    }
    assert len(auth.calls) == 1
    assert [url for url, _ in calls] == [
        "https://api.crisp.chat/v1/website/website-id/conversations/1",
        "https://api.crisp.chat/v1/website/website-id/conversation/session-1/messages",
    ]
    assert calls[0][1]["headers"]["authorization"].startswith("Basic ")


def test_crisp_unread_returns_empty_for_missing_profile_and_502_for_malformed_provider(monkeypatch):
    secret = "crisp-secret"
    env, _ = make_env(secret, profile={"uid": "crisp-user"})
    assert asyncio.run(get_crisp_unread(FakeRequest(env, signed_headers(secret)))) == {
        "unread_count": 0,
        "messages": [],
    }

    env, _ = make_env(secret)

    async def malformed_fetch(url, **kwargs):
        return FakeResponse(malformed=True)

    monkeypatch.setattr(crisp_routes, "worker_fetch", malformed_fetch)
    response = asyncio.run(get_crisp_unread(FakeRequest(env, signed_headers(secret))))
    assert response.status_code == 502
    assert json.loads(response.body) == {"error": "Crisp request failed"}
