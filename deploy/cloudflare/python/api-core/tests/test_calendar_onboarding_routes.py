import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from calendar_onboarding_routes import (  # noqa: E402
    get_calendar_onboarding_status,
    reset_calendar_onboarding,
    skip_calendar_onboarding,
)


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript((migration_dir / "0022_calendar_onboarding.sql").read_text())

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

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeRequest:
    def __init__(self, env, headers):
        self.scope = {"env": env}
        self.headers = headers


def signed_headers(secret: str, uid: str = "calendar-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "calendar-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def test_calendar_onboarding_flags_are_idempotent_and_uid_scoped():
    secret = "calendar-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    headers = signed_headers(secret)

    assert asyncio.run(get_calendar_onboarding_status(FakeRequest(env, headers))) == {
        "connected": False,
        "onboarding_completed": False,
        "needs_reconnect": False,
        "reauth_reason": None,
        "state": "not_started",
    }
    assert asyncio.run(skip_calendar_onboarding(FakeRequest(env, headers))) == {"skipped": True}
    assert asyncio.run(skip_calendar_onboarding(FakeRequest(env, headers))) == {"skipped": True}
    assert asyncio.run(get_calendar_onboarding_status(FakeRequest(env, headers)))["state"] == "skipped"

    other = signed_headers(secret, "other-calendar-user")
    assert asyncio.run(get_calendar_onboarding_status(FakeRequest(env, other)))["state"] == "not_started"

    assert asyncio.run(reset_calendar_onboarding(FakeRequest(env, headers))) == {"reset": True}
    assert asyncio.run(get_calendar_onboarding_status(FakeRequest(env, headers)))["state"] == "not_started"


def test_calendar_onboarding_surfaces_reconnect_without_exposing_tokens():
    secret = "calendar-secret"
    db = FakeDb()
    env = type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()
    db.connection.execute(
        "INSERT INTO cf_user_calendar_onboarding "
        "(uid, connected, onboarding_skipped, reauth_required, has_access_token, reauth_reason, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("calendar-user", 1, 0, 0, 0, None, 1, 1),
    )
    db.connection.commit()

    state = asyncio.run(get_calendar_onboarding_status(FakeRequest(env, signed_headers(secret))))
    assert state == {
        "connected": True,
        "onboarding_completed": True,
        "needs_reconnect": True,
        "reauth_reason": None,
        "state": "needs_reconnect",
    }

    db.connection.execute(
        "UPDATE cf_user_calendar_onboarding SET reauth_required = 1, reauth_reason = ? WHERE uid = ?",
        ("token_expired", "calendar-user"),
    )
    db.connection.commit()
    state = asyncio.run(get_calendar_onboarding_status(FakeRequest(env, signed_headers(secret))))
    assert state["reauth_reason"] == "token_expired"
    assert "access_token" not in state
    assert "has_access_token" not in state


def test_calendar_onboarding_requires_signed_better_auth_context():
    secret = "calendar-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    response = asyncio.run(get_calendar_onboarding_status(FakeRequest(env, {})))
    assert response.status_code == 401
