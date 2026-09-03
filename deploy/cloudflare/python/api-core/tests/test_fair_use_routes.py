import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import fair_use_routes as fair_use_routes_module  # noqa: E402
from fair_use_routes import (  # noqa: E402
    get_fair_use_status,
    get_flagged_users,
    get_public_case_status,
    get_user_fair_use_detail,
    lookup_fair_use_case,
    reset_user_fair_use,
    resolve_fair_use_event,
    set_user_fair_use_stage,
)


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
            "0048_fair_use_usage_revision.sql",
            "0049_fair_use_enforcement.sql",
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

    async def all(self):
        rows = self.connection.execute(self.sql, self.args).fetchall()
        return {"results": [dict(row) for row in rows]}

    async def run(self):
        cursor = self.connection.execute(self.sql, self.args)
        self.connection.commit()
        return {"meta": {"changes": cursor.rowcount}}


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


def make_env(secret: str, admin_key: str | None = None):
    return type(
        "Env",
        (),
        {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret, "FAIR_USE_ADMIN_KEY": admin_key},
    )()


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


def test_status_uses_live_sources_rolling_windows_paid_limits_and_dg_budget(monkeypatch):
    secret = "fair-use-secret"
    env = make_env(secret)
    now = int(datetime(2026, 8, 15, 12, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr(fair_use_routes_module.time, "time", lambda: now)
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


def test_public_case_lookup_exposes_only_case_status_and_support_fields():
    secret = "fair-use-secret"
    env = make_env(secret)
    now = int(time.time())
    env.APP_DB.connection.execute(
        "INSERT INTO cf_fair_use_states (uid, stage, last_case_ref, restrict_until, updated_at) "
        "VALUES ('fair-use-user', 'restrict', 'FU-A1B2C3D4E5F6', ?, ?)",
        (now + 3_600, now),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_fair_use_events "
        "(event_id, uid, case_ref, created_at, trigger, daily_speech_ms, three_day_speech_ms, "
        "weekly_speech_ms, daily_threshold_ms, three_day_threshold_ms, weekly_threshold_ms, "
        "enforcement_action, previous_stage, new_stage) "
        "VALUES ('event-1', 'fair-use-user', 'FU-A1B2C3D4E5F6', ?, 'daily', 7200001, 7200001, "
        "7200001, 7200000, 28800000, 36000000, 'restrict', 'throttle', 'restrict')",
        (now,),
    )
    env.APP_DB.connection.commit()

    response = asyncio.run(get_public_case_status("fu-a1b2c3d4e5f6", FakeRequest(env)))

    assert response == {
        "case_ref": "FU-A1B2C3D4E5F6",
        "stage": "restrict",
        "message": response["message"],
        "created_at": response["created_at"],
        "updated_at": response["created_at"],
        "support_email": "team@basedhardware.com",
    }
    assert "case reference" in response["message"]
    assert "uid" not in response
    assert "classifier" not in response
    assert asyncio.run(get_public_case_status("invalid", FakeRequest(env))).status_code == 404
    assert asyncio.run(get_public_case_status("FU-000000000000", FakeRequest(env))).status_code == 404


def test_admin_routes_fail_closed_for_missing_secret_and_legacy_key():
    no_secret = make_env("internal")
    old_key = make_env("internal", admin_key="current-key")

    assert asyncio.run(get_flagged_users(FakeRequest(no_secret, {"x-admin-key": "legacy-key"}))).status_code == 403
    assert asyncio.run(get_flagged_users(FakeRequest(old_key, {"x-admin-key": "legacy-key"}))).status_code == 403
    assert asyncio.run(get_flagged_users(FakeRequest(old_key))).status_code == 403


def test_admin_routes_manage_d1_state_events_and_usage():
    admin_key = "fair-use-admin-secret"
    env = make_env("internal", admin_key=admin_key)
    headers = {"x-admin-key": admin_key}
    now = int(time.time())
    env.APP_DB.connection.execute(
        "INSERT INTO cf_fair_use_states "
        "(uid, stage, last_case_ref, updated_at, violation_count_7d, violation_count_30d, "
        "last_classifier_score, last_classifier_type) "
        "VALUES ('managed-user', 'warning', 'FU-A1B2C3D4E5F6', ?, 1, 2, 1.0, 'free_exhausted')",
        (now,),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_fair_use_events "
        "(event_id, uid, case_ref, created_at, trigger, daily_speech_ms, three_day_speech_ms, "
        "weekly_speech_ms, daily_threshold_ms, three_day_threshold_ms, weekly_threshold_ms, classifier_json, "
        "enforcement_action, previous_stage, new_stage) "
        "VALUES ('event-admin', 'managed-user', 'FU-A1B2C3D4E5F6', ?, 'daily', 7200001, 7200001, "
        "7200001, 7200000, 28800000, 36000000, '{\"usage_type\":\"free_exhausted\"}', "
        "'warning', 'none', 'warning')",
        (now,),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_fair_use_usage_sources "
        "(uid, source_kind, source_id, occurred_at, speech_ms, dg_ms, updated_at) "
        "VALUES ('managed-user', 'sync_fresh', 'admin-usage', ?, 7200001, 0, ?)",
        (now, now),
    )
    env.APP_DB.connection.commit()

    flagged = asyncio.run(get_flagged_users(FakeRequest(env, headers)))
    assert [row["uid"] for row in flagged["users"]] == ["managed-user"]
    assert flagged["users"][0]["id"] == "current"
    assert flagged["fair_use_enabled"] is True

    detail = asyncio.run(get_user_fair_use_detail("managed-user", FakeRequest(env, headers)))
    assert detail["state"]["stage"] == "warning"
    assert detail["events"][0]["id"] == "event-admin"
    assert detail["events"][0]["classifier"]["usage_type"] == "free_exhausted"
    assert detail["current_speech_ms"] == {
        "daily_ms": 7_200_001,
        "three_day_ms": 7_200_001,
        "weekly_ms": 7_200_001,
    }

    case = asyncio.run(lookup_fair_use_case("fu-a1b2c3d4e5f6", FakeRequest(env, headers)))
    assert case["uid"] == "managed-user"
    assert case["event_id"] == "event-admin"

    resolved = asyncio.run(
        resolve_fair_use_event("managed-user", "event-admin", FakeRequest(env, headers), notes="verified")
    )
    assert resolved == {"status": "resolved"}
    event = env.APP_DB.connection.execute(
        "SELECT resolved, resolved_by, admin_notes FROM cf_fair_use_events WHERE event_id = 'event-admin'"
    ).fetchone()
    assert event["resolved"] == 1
    assert event["resolved_by"].startswith("admin:")
    assert event["admin_notes"] == "verified"

    updated = asyncio.run(set_user_fair_use_stage("managed-user", FakeRequest(env, headers), stage="restrict"))
    assert updated == {"status": "updated", "stage": "restrict"}
    restricted_state = env.APP_DB.connection.execute(
        "SELECT stage, restrict_until FROM cf_fair_use_states WHERE uid = 'managed-user'"
    ).fetchone()
    assert restricted_state["stage"] == "restrict"
    assert restricted_state["restrict_until"] >= now + 30 * 86_400 - 1

    reset = asyncio.run(reset_user_fair_use("managed-user", FakeRequest(env, headers)))
    assert reset == {"status": "reset"}
    state = env.APP_DB.connection.execute(
        "SELECT stage, violation_count_7d, last_classifier_type, cleared_by FROM cf_fair_use_states "
        "WHERE uid = 'managed-user'"
    ).fetchone()
    assert state["stage"] == "none"
    assert state["violation_count_7d"] == 0
    assert state["last_classifier_type"] == "none"
    assert state["cleared_by"].startswith("admin:")

    created = asyncio.run(reset_user_fair_use("legacy-user-without-state", FakeRequest(env, headers)))
    assert created == {"status": "reset"}
    assert (
        env.APP_DB.connection.execute(
            "SELECT stage FROM cf_fair_use_states WHERE uid = 'legacy-user-without-state'"
        ).fetchone()["stage"]
        == "none"
    )

    invalid_stage = asyncio.run(set_user_fair_use_stage("managed-user", FakeRequest(env, headers), stage="invalid"))
    assert invalid_stage.status_code == 400
    missing_event = asyncio.run(resolve_fair_use_event("managed-user", "missing", FakeRequest(env, headers)))
    assert missing_event.status_code == 404
