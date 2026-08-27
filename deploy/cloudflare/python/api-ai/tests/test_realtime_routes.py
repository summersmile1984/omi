import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import realtime_routes as routes  # noqa: E402


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration = Path(__file__).parents[3] / "migrations/app/0031_realtime_usage.sql"
        self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeStatement:
    def __init__(self, connection, sql):
        self.connection = connection
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeRequest:
    def __init__(self, env, headers, body):
        self.scope = {"env": env}
        self.headers = headers
        self.body = body

    async def json(self):
        return self.body


def signed_headers(secret: str, uid: str = "realtime-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "realtime-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_realtime_mint_requires_auth_and_provider_key():
    secret = "realtime-secret"
    env = type("Env", (), {"INTERNAL_ASSERTION_SECRET": secret})()
    unauthenticated = asyncio.run(routes.mint_realtime_session(FakeRequest(env, {}, {"provider": "openai"})))
    assert unauthenticated.status_code == 401

    missing = asyncio.run(
        routes.mint_realtime_session(FakeRequest(env, signed_headers(secret), {"provider": "openai"}))
    )
    assert missing.status_code == 503
    assert json.loads(missing.body) == {
        "error": "OpenAI realtime is not configured",
        "reason": "provider_not_configured",
        "backend_route": "/v2/realtime/session",
        "retryable": True,
        "provider": "OpenAI",
    }


def test_openai_mint_uses_worker_fetch_and_hashes_ephemeral_token(monkeypatch):
    secret = "realtime-secret"
    database = FakeDb()
    env = type(
        "Env",
        (),
        {
            "INTERNAL_ASSERTION_SECRET": secret,
            "OPENAI_API_KEY": "provider-key",
            "APP_DB": database,
            "OPENAI_REALTIME_CLIENT_SECRETS_URL": "https://openai.example.test/client_secrets",
        },
    )()
    calls = {}

    class FakeResponse:
        status = 200

        async def json(self):
            return {"value": "ek_ephemeral_secret", "expires_at": 1_777_000_000}

    async def fake_fetch(url, **options):
        calls["url"] = url
        calls["options"] = options
        return FakeResponse()

    monkeypatch.setattr(routes, "worker_fetch", fake_fetch)
    response = asyncio.run(
        routes.mint_realtime_session(FakeRequest(env, signed_headers(secret), {"provider": "openai"}))
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "provider": "openai",
        "token": "ek_ephemeral_secret",
        "expires_at": "1777000000",
    }
    assert calls["url"] == "https://openai.example.test/client_secrets"
    assert calls["options"]["headers"] == {
        "authorization": "Bearer provider-key",
        "content-type": "application/json",
    }
    assert json.loads(calls["options"]["body"]) == {
        "session": {"type": "realtime", "model": "gpt-realtime-2"}
    }
    stored = database.connection.execute("SELECT token_hash FROM cf_realtime_sessions").fetchone()
    assert stored[0] == hashlib.sha256(b"ek_ephemeral_secret").hexdigest()
    assert "ek_ephemeral_secret" not in stored[0]


def test_realtime_mint_classifies_provider_quota_failure(monkeypatch):
    secret = "realtime-secret"
    env = type("Env", (), {"INTERNAL_ASSERTION_SECRET": secret, "OPENAI_API_KEY": "provider-key"})()

    class FakeResponse:
        status = 429

        async def json(self):
            return {"error": {"code": "rate_limit", "message": "quota exceeded"}}

    async def fake_fetch(_url, **_options):
        return FakeResponse()

    monkeypatch.setattr(routes, "worker_fetch", fake_fetch)
    response = asyncio.run(
        routes.mint_realtime_session(FakeRequest(env, signed_headers(secret), {"provider": "openai"}))
    )
    assert response.status_code == 429
    assert json.loads(response.body)["reason"] == "provider_quota_exceeded"
    assert json.loads(response.body)["retryable"] is True


def test_gemini_mint_uses_bounded_server_ttl(monkeypatch):
    secret = "realtime-secret"
    env = type("Env", (), {"INTERNAL_ASSERTION_SECRET": secret, "GEMINI_API_KEY": "gemini-key"})()
    calls = {}

    class FakeResponse:
        status = 200

        async def json(self):
            return {"name": "auth_tokens/realtime"}

    async def fake_fetch(url, **options):
        calls["url"] = url
        calls["options"] = options
        return FakeResponse()

    monkeypatch.setattr(routes, "worker_fetch", fake_fetch)
    response = asyncio.run(
        routes.mint_realtime_session(FakeRequest(env, signed_headers(secret), {"provider": "gemini"}))
    )
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["provider"] == "gemini"
    assert payload["token"] == "auth_tokens/realtime"
    assert calls["url"].endswith("?key=gemini-key")
    request_body = json.loads(calls["options"]["body"])
    assert request_body["uses"] == 1
    assert request_body["newSessionExpireTime"] < request_body["expireTime"]


def test_realtime_usage_is_uid_scoped_and_aggregates_d1_rows():
    secret = "realtime-secret"
    database = FakeDb()
    env = type("Env", (), {"INTERNAL_ASSERTION_SECRET": secret, "APP_DB": database})()
    body = {
        "provider": "openai",
        "model": "gpt-realtime-2",
        "input_text_tokens": 100,
        "input_audio_tokens": 50,
        "input_cached_tokens": 25,
        "output_text_tokens": 20,
        "output_audio_tokens": 10,
    }
    first = asyncio.run(routes.report_realtime_usage(FakeRequest(env, signed_headers(secret), body)))
    second = asyncio.run(routes.report_realtime_usage(FakeRequest(env, signed_headers(secret), body)))
    assert first.status_code == 204
    assert second.status_code == 204

    row = database.connection.execute(
        "SELECT input_text_tokens, input_audio_tokens, input_cached_tokens, output_text_tokens, "
        "output_audio_tokens, total_tokens, cost_micros, call_count FROM cf_realtime_usage"
    ).fetchone()
    assert tuple(row) == (200, 100, 50, 40, 20, 410, 6_260, 2)

    other = asyncio.run(
        routes.report_realtime_usage(
            FakeRequest(env, signed_headers(secret, "other-user"), {**body, "input_text_tokens": 1})
        )
    )
    assert other.status_code == 204
    assert database.connection.execute("SELECT COUNT(*) FROM cf_realtime_usage").fetchone()[0] == 2


def test_realtime_usage_rejects_unknown_provider():
    secret = "realtime-secret"
    env = type("Env", (), {"INTERNAL_ASSERTION_SECRET": secret, "APP_DB": FakeDb()})()
    response = asyncio.run(
        routes.report_realtime_usage(
            FakeRequest(env, signed_headers(secret), {"provider": "unknown", "input_text_tokens": 1})
        )
    )
    assert response.status_code == 400
