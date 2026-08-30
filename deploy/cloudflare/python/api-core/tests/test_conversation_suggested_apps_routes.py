import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from conversation_routes import get_conversation_suggested_apps  # noqa: E402


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for name in (
            "0032_conversations.sql",
            "0033_conversation_sync_flag.sql",
            "0035_app_catalog.sql",
            "0036_app_installations.sql",
            "0045_app_reviews.sql",
            "0060_app_subscriptions.sql",
        ):
            self.connection.executescript((migration_dir / name).read_text())
        self.connection.execute("ALTER TABLE cf_conversations ADD COLUMN app_id TEXT")

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

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}


class FakeRequest:
    def __init__(self, env, headers):
        self.scope = {"env": env}
        self.headers = headers
        self.query_params = {}


def signed_headers(secret: str, uid: str = "conversation-user"):
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"uid": uid, "authority": "better-auth", "requestId": "suggested-apps-test"},
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret: str):
    return type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()


def test_suggested_apps_are_ordered_public_and_uid_scoped():
    secret = "suggested-apps-secret"
    env = make_env(secret)
    db = env.APP_DB.connection
    db.execute(
        "INSERT INTO cf_conversations (uid, id, created_at, suggested_apps_json) VALUES (?, ?, ?, ?)",
        ("conversation-user", "conversation-1", 1, '["paid-app", "free-app", "missing", "private-app", "persona-app"]'),
    )
    app_payload = lambda name, **extra: json.dumps(
        {"name": name, "description": name, "capabilities": ["chat"], **extra}
    )
    db.executemany(
        "INSERT INTO cf_app_catalog (id, approved, disabled, installs, rating_avg, rating_count, data_json, updated_at) VALUES (?, 1, 0, ?, ?, ?, ?, 1)",
        [
            ("paid-app", 8, 4.5, 2, app_payload("Paid", is_paid=True, payment_link="https://pay.example/checkout")),
            ("free-app", 3, 4.0, 1, app_payload("Free")),
            ("private-app", 1, 3.0, 1, app_payload("Private", private=True)),
            ("persona-app", 1, 3.0, 1, app_payload("Persona", capabilities=["persona"])),
        ],
    )
    db.execute(
        "INSERT INTO cf_user_enabled_apps (uid, app_id, created_at) VALUES (?, ?, ?)",
        ("conversation-user", "free-app", 1),
    )
    db.commit()

    response = asyncio.run(get_conversation_suggested_apps(FakeRequest(env, signed_headers(secret)), "conversation-1"))

    assert response["conversation_id"] == "conversation-1"
    assert [app["id"] for app in response["suggested_apps"]] == ["paid-app", "free-app"]
    assert response["suggested_apps"][0]["is_user_paid"] is False
    assert "client_reference_id=uid_conversation-user" in response["suggested_apps"][0]["payment_link"]
    assert response["suggested_apps"][1]["enabled"] is True


def test_suggested_apps_rejects_unauthorized_and_missing_conversations():
    secret = "suggested-apps-secret"
    env = make_env(secret)
    assert asyncio.run(get_conversation_suggested_apps(FakeRequest(env, {}), "missing")).status_code == 401
    response = asyncio.run(get_conversation_suggested_apps(FakeRequest(env, signed_headers(secret)), "missing"))
    assert response.status_code == 404
