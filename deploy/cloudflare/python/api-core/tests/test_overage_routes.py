import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from overage_routes import (  # noqa: E402
    OVERAGE_EXPLAINER_TITLE,
    PROVIDER_REFERENCE_RATES,
    get_overage_info,
)


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


class FakeDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        migration_dir = Path(__file__).parents[3] / "migrations/app"
        self.connection.executescript(
            "CREATE TABLE cf_account_deletion_intents (uid TEXT PRIMARY KEY);"
            "CREATE TABLE cf_account_deletion_tombstones (uid TEXT PRIMARY KEY);"
        )
        for name in (
            "0032_conversations.sql",
            "0037_memories.sql",
            "0042_chat_messages.sql",
            "0046_account_usage.sql",
            "0054_chat_sessions.sql",
            "0055_chat_quota_accounting.sql",
            "0056_llm_usage_daily.sql",
        ):
            self.connection.executescript((migration_dir / name).read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)


class FakeRequest:
    def __init__(self, env, headers=None):
        self.scope = {"env": env}
        self.headers = headers or {}


def signed_headers(secret: str, uid: str = "overage-user"):
    raw = json.dumps({"uid": uid}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret="overage-secret", **values):
    defaults = {
        "APP_DB": FakeDb(),
        "INTERNAL_ASSERTION_SECRET": secret,
        "NEO_CHAT_QUESTIONS_PER_MONTH": "2",
        "OPERATOR_CHAT_QUESTIONS_PER_MONTH": "2",
        "ARCHITECT_CHAT_COST_USD_PER_MONTH": "0.2",
        "OVERAGE_MARKUP_MULTIPLIER": "1.15",
    }
    defaults.update(values)
    return type("Env", (), defaults)()


def insert_subscription(env, plan):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_user_subscriptions (uid, plan, status, updated_at) " "VALUES ('overage-user', ?, 'active', 1)",
        (plan,),
    )
    env.APP_DB.connection.commit()


def insert_chat_event(env, event_id, *, source="v2_messages", cost_usd=0.1, settled=True):
    occurred_at = int(datetime.now(timezone.utc).timestamp())
    env.APP_DB.connection.execute(
        "INSERT INTO cf_chat_quota_events "
        "(uid, idempotency_key, source, occurred_at, cost_usd, prompt_tokens, completion_tokens, model) "
        "VALUES ('overage-user', ?, ?, ?, ?, 10, 2, '@cf/test')",
        (event_id, source, occurred_at, cost_usd),
    )
    if settled:
        env.APP_DB.connection.execute(
            "UPDATE cf_chat_quota_events SET settled_at = ? " "WHERE uid = 'overage-user' AND idempotency_key = ?",
            (occurred_at, event_id),
        )
    env.APP_DB.connection.commit()


def insert_desktop_bucket(env, cost_usd):
    usage_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    env.APP_DB.connection.execute(
        "INSERT INTO cf_llm_usage_daily "
        "(uid, usage_day, usage_kind, feature, model, account, cost_usd, call_count, updated_at) "
        "VALUES ('overage-user', ?, 'bucket', 'desktop_chat', '', 'omi', ?, 1, 1)",
        (usage_day, cost_usd),
    )
    env.APP_DB.connection.commit()


def test_operator_overage_combines_managed_and_desktop_usage():
    secret = "overage-secret"
    env = make_env(secret)
    insert_subscription(env, "operator")
    insert_chat_event(env, "managed-1")
    insert_chat_event(env, "managed-2")
    insert_chat_event(env, "desktop-1", source="desktop_messages", cost_usd=0, settled=False)
    insert_desktop_bucket(env, 0.1)

    response = asyncio.run(get_overage_info(FakeRequest(env, signed_headers(secret))))

    assert response == {
        "plan": "Operator",
        "plan_type": "operator",
        "is_overage_plan": True,
        "included_questions": 2,
        "included_cost_usd": None,
        "used_questions": 3,
        "excess_questions": 1,
        "real_cost_usd": 0.3,
        "overage_usd": 0.115,
        "markup_multiplier": 1.15,
        "markup_percent": 15.0,
        "reset_at": response["reset_at"],
        "explainer_title": OVERAGE_EXPLAINER_TITLE,
        "explainer_body": response["explainer_body"],
        "provider_reference_rates": PROVIDER_REFERENCE_RATES,
        "byok_available": True,
    }
    assert isinstance(response["reset_at"], int)
    assert "15% buffer" in response["explainer_body"]


def test_architect_overage_uses_exact_cost_excess_and_free_stays_zero():
    secret = "overage-secret"
    architect = make_env(secret)
    insert_subscription(architect, "architect")
    for index in range(3):
        insert_chat_event(architect, f"architect-{index}")

    architect_response = asyncio.run(get_overage_info(FakeRequest(architect, signed_headers(secret))))
    assert architect_response["included_questions"] is None
    assert architect_response["included_cost_usd"] == 0.2
    assert architect_response["real_cost_usd"] == 0.3
    assert architect_response["overage_usd"] == 0.115
    assert architect_response["is_overage_plan"] is True

    free = make_env(secret)
    insert_chat_event(free, "free-1")
    free_response = asyncio.run(get_overage_info(FakeRequest(free, signed_headers(secret))))
    assert free_response["plan"] == "Free"
    assert free_response["plan_type"] == "basic"
    assert free_response["included_questions"] is None
    assert free_response["included_cost_usd"] is None
    assert free_response["used_questions"] == 1
    assert free_response["real_cost_usd"] == 0.1
    assert free_response["overage_usd"] == 0.0
    assert free_response["is_overage_plan"] is False


def test_overage_authenticates_and_fails_closed_for_unsettled_provider_cost():
    secret = "overage-secret"
    env = make_env(secret)
    assert asyncio.run(get_overage_info(FakeRequest(env))).status_code == 401

    insert_subscription(env, "operator")
    insert_chat_event(env, "running", settled=False)
    response = asyncio.run(get_overage_info(FakeRequest(env, signed_headers(secret))))
    assert response.status_code == 503

    class FailingDb:
        def prepare(self, _sql):
            raise RuntimeError("D1 unavailable")

    failing = type("Env", (), {"APP_DB": FailingDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    unavailable = asyncio.run(get_overage_info(FakeRequest(failing, signed_headers(secret))))
    assert unavailable.status_code == 503
