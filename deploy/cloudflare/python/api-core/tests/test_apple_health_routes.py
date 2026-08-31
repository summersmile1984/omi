import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from apple_health_routes import (  # noqa: E402
    delete_apple_health_connection,
    save_apple_health_connection,
    sync_apple_health_data,
)
from integration_routes import get_integration_status  # noqa: E402


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for migration in sorted(migration_dir.glob("*.sql")):
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

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row is not None else None

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


class FakeRequest:
    def __init__(self, env, headers, body=None):
        self.scope = {"env": env}
        self.headers = headers
        self._body = json.dumps(body).encode() if body is not None else b"{}"

    async def body(self):
        return self._body


def signed_headers(secret: str, uid: str = "health-user"):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "health-test"},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def environment():
    secret = "apple-health-secret"
    env = type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    return env, secret


def test_apple_health_sync_persists_bounded_projection_and_reports_types():
    env, secret = environment()
    body = {
        "period_days": 7,
        "total_steps": 7000,
        "daily_steps": [{"date": "2026-08-28", "steps": 1000}],
        "total_sleep_hours": 49.5,
        "sleep_sessions_count": 7,
        "sleep_sessions": [{"start": "2026-08-27T23:00:00Z"}],
        "heart_rate_average": 62,
        "heart_rate_min": 48,
        "heart_rate_max": 142,
        "total_active_energy": 3500,
        "workouts": [{"type": "walking", "minutes": 30}],
    }
    response = asyncio.run(sync_apple_health_data(FakeRequest(env, signed_headers(secret), body)))
    assert response["status"] == "ok"
    assert response["app_key"] == "apple_health"
    assert response["data_types_synced"] == [
        "period_days",
        "steps",
        "sleep",
        "heart_rate",
        "active_energy",
        "workouts",
    ]
    row = env.APP_DB.connection.execute(
        "SELECT connected, health_data_json, last_synced FROM cf_apple_health WHERE uid = ?",
        ("health-user",),
    ).fetchone()
    assert row["connected"] == 1
    assert json.loads(row["health_data_json"])["steps"]["average_per_day"] == 1000
    assert row["last_synced"] == response["synced_at"]

    status = asyncio.run(
        get_integration_status(
            FakeRequest(env, signed_headers(secret)),
            "apple_health",
        )
    )
    assert status == {"connected": True, "app_key": "apple_health"}


def test_apple_health_connection_save_delete_and_validation_are_uid_scoped():
    env, secret = environment()
    headers = signed_headers(secret)

    saved = asyncio.run(save_apple_health_connection(FakeRequest(env, headers, {})))
    assert saved == {"status": "ok", "app_key": "apple_health"}
    other = asyncio.run(get_integration_status(FakeRequest(env, signed_headers(secret, "other-user")), "apple_health"))
    assert other == {"connected": False, "app_key": "apple_health"}

    invalid = asyncio.run(
        sync_apple_health_data(
            FakeRequest(env, headers, {"period_days": -1}),
        )
    )
    assert invalid.status_code == 400

    deleted = asyncio.run(delete_apple_health_connection(FakeRequest(env, headers)))
    assert deleted.status_code == 204
    missing = asyncio.run(delete_apple_health_connection(FakeRequest(env, headers)))
    assert missing.status_code == 404

    unauthorized = asyncio.run(sync_apple_health_data(FakeRequest(env, {}, {})))
    assert unauthorized.status_code == 401
