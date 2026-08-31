import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import sentry_routes  # noqa: E402
from sentry_routes import sentry_poll, sentry_webhook  # noqa: E402


class FakeStatement:
    def __init__(self, connection, sql):
        self.connection = connection
        self.sql = sql
        self.args = ()

    def bind(self, *args):
        self.args = args
        return self

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0016_action_items.sql").read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeRequest:
    def __init__(self, env, headers=None, body=b""):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.body = body


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def json(self):
        return self.payload


def signature(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_env(**kwargs):
    values = {
        "APP_DB": FakeDb(),
        "SENTRY_WEBHOOK_SECRET": "sentry-secret",
        "SENTRY_ADMIN_UID": "sentry-admin",
        "SENTRY_AUTH_TOKEN": "sentry-token",
    }
    values.update(kwargs)
    return type("Env", (), values)()


def test_webhook_verifies_signature_and_is_idempotent(monkeypatch):
    env = make_env()
    body = json.dumps(
        {
            "action": "created",
            "data": {
                "issue": {"id": "issue-1", "shortId": "OMI-1", "title": "Broken button", "issueCategory": "feedback"}
            },
        }
    ).encode()
    monkeypatch.setattr(
        sentry_routes,
        "worker_fetch",
        lambda *args, **kwargs: _response(200, {"contexts": {"feedback": {"message": "Button is broken"}}}),
    )
    headers = {"sentry-hook-signature": signature("sentry-secret", body)}

    created = asyncio.run(sentry_webhook(FakeRequest(env, headers, body)))
    duplicate = asyncio.run(sentry_webhook(FakeRequest(env, headers, body)))

    assert created == {"status": "created"}
    assert duplicate == {"status": "duplicate"}
    rows = env.APP_DB.connection.execute("SELECT * FROM cf_action_items").fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "sentry_feedback"
    assert rows[0]["idempotency_key"] == "sentry-feedback:issue-1"
    assert "Button is broken" in rows[0]["description"]


def test_webhook_preserves_installation_and_ignored_contracts():
    env = make_env()
    installation = asyncio.run(sentry_webhook(FakeRequest(env, {"sentry-hook-resource": "installation"}, b"not-json")))
    assert installation == {"status": "ok"}

    body = json.dumps(
        {"action": "resolved", "data": {"issue": {"id": "issue-2", "issueCategory": "feedback"}}}
    ).encode()
    result = asyncio.run(
        sentry_webhook(FakeRequest(env, {"sentry-hook-signature": signature("sentry-secret", body)}, body))
    )
    assert result == {"status": "ignored"}


def test_webhook_rejects_bad_auth_and_missing_configuration():
    env = make_env()
    body = b"{}"
    unauthorized = asyncio.run(sentry_webhook(FakeRequest(env, {"sentry-hook-signature": "0" * 64}, body)))
    assert unauthorized.status_code == 401

    unavailable = asyncio.run(sentry_webhook(FakeRequest(make_env(SENTRY_WEBHOOK_SECRET=""), {}, body)))
    assert unavailable.status_code == 503


def test_poll_maps_provider_failures_and_creates_feedback(monkeypatch):
    env = make_env()
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith("/events/latest/"):
            return FakeResponse(200, {"contexts": {"feedback": {"message": "Please fix this"}}})
        return FakeResponse(
            200, [{"id": "issue-3", "shortId": "OMI-3", "title": "Fix this", "issueCategory": "feedback"}]
        )

    monkeypatch.setattr(sentry_routes, "worker_fetch", fetch)
    result = asyncio.run(sentry_poll(FakeRequest(env)))
    assert result == {"status": "ok", "created": 1, "skipped": 0, "total_fetched": 1}
    assert len(calls) == 2

    monkeypatch.setattr(
        sentry_routes,
        "worker_fetch",
        lambda *args, **kwargs: _response(429, {"detail": "rate limited"}),
    )
    limited = asyncio.run(sentry_poll(FakeRequest(env)))
    assert limited == {
        "status": "skipped",
        "reason": "sentry_rate_limited",
        "sentry_status": 429,
        "created": 0,
        "skipped": 0,
        "total_fetched": 0,
    }


async def _response(status, payload):
    return FakeResponse(status, payload)
