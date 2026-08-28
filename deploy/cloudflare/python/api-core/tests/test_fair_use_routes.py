import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from fair_use_routes import get_fair_use_status  # noqa: E402


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        for name in (
            "0032_conversations.sql",
            "0037_memories.sql",
            "0046_account_usage.sql",
            "0047_fair_use_projection.sql",
        ):
            self.connection.executescript((migration_dir / name).read_text())

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


class FakeRequest:
    def __init__(self, env, headers=None):
        self.scope = {"env": env}
        self.headers = headers or {}


def signed_headers(secret: str, uid: str = "fair-use-user"):
    raw = json.dumps({"uid": uid, "authority": "better-auth"}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret: str):
    return type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()


def test_default_status_is_authenticated_and_reports_zero_usage():
    secret = "fair-use-secret"
    env = make_env(secret)

    response = asyncio.run(get_fair_use_status(FakeRequest(env, signed_headers(secret))))

    assert response["stage"] == "none"
    assert response["speech_hours_today"] == 0
    assert response["limits"] == {"daily_hours": 2.0, "three_day_hours": 8.0, "weekly_hours": 10.0}
    assert response["message"] == "Your usage is within normal limits."
    assert response["dg_budget"]["remaining_ms"] == 1_800_000
    assert asyncio.run(get_fair_use_status(FakeRequest(env))).status_code == 401


def test_status_uses_live_sources_rolling_windows_paid_limits_and_dg_budget():
    secret = "fair-use-secret"
    env = make_env(secret)
    now = int(time.time())
    env.APP_DB.connection.execute(
        "INSERT INTO cf_user_subscriptions (uid, plan, status, updated_at) VALUES (?, 'architect', 'active', ?)",
        ("fair-use-user", now),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_fair_use_states (uid, stage, last_case_ref, restrict_until, updated_at) "
        "VALUES (?, 'restrict', 'FU-ABC123', ?, ?)",
        ("fair-use-user", now + 3_600, now),
    )
    rows = (
        ("realtime", "today", now - 3_600, 3_600_000, 600_000),
        ("sync_fresh", "two-days", now - 2 * 86_400, 7_200_000, 0),
        ("realtime", "five-days", now - 5 * 86_400, 3_600_000, 0),
        ("sync_backfill", "excluded", now - 60, 360_000_000, 1_800_000),
    )
    env.APP_DB.connection.executemany(
        "INSERT INTO cf_fair_use_usage_sources "
        "(uid, source_kind, source_id, occurred_at, speech_ms, dg_ms, updated_at) "
        "VALUES ('fair-use-user', ?, ?, ?, ?, ?, ?)",
        [(*row, now) for row in rows],
    )
    env.APP_DB.connection.commit()

    response = asyncio.run(get_fair_use_status(FakeRequest(env, signed_headers(secret))))

    assert response["stage"] == "restrict"
    assert response["case_ref"] == "FU-ABC123"
    assert response["speech_hours_today"] == 1.0
    assert response["speech_hours_3day"] == 3.0
    assert response["speech_hours_weekly"] == 4.0
    assert response["limits"] == {"daily_hours": 4.0, "three_day_hours": 16.0, "weekly_hours": 20.0}
    assert response["usage_pct"] == {"daily": 25.0, "three_day": 18.8, "weekly": 20.0}
    assert response["dg_budget"]["used_ms"] == 600_000
    assert response["dg_budget"]["remaining_ms"] == 1_200_000
    assert response["dg_budget"]["exhausted"] is False
    assert "team@basedhardware.com" in response["message"]


def test_expired_or_malformed_restriction_is_reported_as_throttled():
    secret = "fair-use-secret"
    env = make_env(secret)
    now = int(time.time())
    env.APP_DB.connection.execute(
        "INSERT INTO cf_fair_use_states (uid, stage, last_case_ref, restrict_until, updated_at) "
        "VALUES (?, 'restrict', 'FU-EXPIRED', ?, ?)",
        ("fair-use-user", now - 1, now),
    )
    env.APP_DB.connection.commit()

    response = asyncio.run(get_fair_use_status(FakeRequest(env, signed_headers(secret))))

    assert response["stage"] == "throttle"
    assert "temporarily reduced" in response["message"]


def test_status_fails_closed_when_d1_is_unavailable():
    secret = "fair-use-secret"

    class FailingDb:
        def prepare(self, _sql):
            raise RuntimeError("D1 unavailable")

    env = type("Env", (), {"APP_DB": FailingDb(), "INTERNAL_ASSERTION_SECRET": secret})()

    response = asyncio.run(get_fair_use_status(FakeRequest(env, signed_headers(secret))))

    assert response.status_code == 503
