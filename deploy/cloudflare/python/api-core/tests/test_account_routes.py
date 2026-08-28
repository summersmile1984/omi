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

from account_routes import (  # noqa: E402
    get_available_plans,
    get_user_chat_usage_quota,
    get_user_paywall_status,
    get_user_subscription,
    get_user_trial_status,
    get_user_usage,
)


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
    def __init__(self, env, headers=None, query=None):
        self.scope = {"env": env}
        self.headers = headers or {}
        self.query_params = query or {}


def signed_headers(secret: str, uid: str = "account-user", account_created_at=None, **headers):
    context = {"uid": uid, "authority": "better-auth"}
    if account_created_at is not None:
        context["accountCreatedAt"] = account_created_at
    raw = json.dumps(context, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        **headers,
    }


def make_env(secret: str):
    db = FakeDb()
    return type("Env", (), {"APP_DB": db, "INTERNAL_ASSERTION_SECRET": secret})()


def insert_usage(
    env,
    *,
    uid="account-user",
    kind,
    source_id,
    occurred_at,
    seconds=0,
    words=0,
    insights=0,
    memories=0,
):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_usage_sources "
        "(uid, source_kind, source_id, occurred_at, transcription_seconds, words_transcribed, "
        "insights_gained, memories_created, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, kind, source_id, occurred_at, seconds, words, insights, memories, occurred_at),
    )
    env.APP_DB.connection.commit()


def insert_price(env, price_id, plan_id, interval="month"):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_subscription_prices "
        "(id, plan_id, title, price_string, interval, unit_amount, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (price_id, plan_id, f"{plan_id} {interval}", "$10/mo", interval, 1000, int(datetime.now().timestamp())),
    )
    env.APP_DB.connection.commit()


def insert_chat_event(
    env,
    event_id,
    *,
    uid="account-user",
    occurred_at=None,
    cost_usd=0.0,
    settled=True,
):
    now = int(datetime.now(timezone.utc).timestamp()) if occurred_at is None else occurred_at
    env.APP_DB.connection.execute(
        "INSERT INTO cf_chat_quota_events "
        "(uid, idempotency_key, source, occurred_at, cost_usd, prompt_tokens, completion_tokens, model, settled_at) "
        "VALUES (?, ?, 'v2_messages', ?, ?, 1, 1, '@cf/test', NULL)",
        (uid, event_id, now, cost_usd),
    )
    if settled:
        env.APP_DB.connection.execute(
            "UPDATE cf_chat_quota_events SET settled_at = ? WHERE uid = ? AND idempotency_key = ?",
            (now, uid, event_id),
        )
    env.APP_DB.connection.commit()


def test_usage_is_authenticated_validated_and_grouped_for_each_period():
    secret = "account-secret"
    env = make_env(secret)
    now = datetime.now(timezone.utc)
    insert_usage(
        env,
        kind="conversation",
        source_id="conversation-1",
        occurred_at=int(now.timestamp()),
        seconds=90,
        words=12,
        insights=2,
    )
    insert_usage(
        env,
        kind="memory",
        source_id="memory-1",
        occurred_at=int(now.timestamp()),
        memories=1,
    )
    insert_usage(
        env,
        uid="other-user",
        kind="memory",
        source_id="other-memory",
        occurred_at=int(now.timestamp()),
        memories=9,
    )

    for period in ("today", "monthly", "yearly", "all_time"):
        response = asyncio.run(get_user_usage(FakeRequest(env, signed_headers(secret), {"period": period})))
        assert response[period] == {
            "transcription_seconds": 90,
            "words_transcribed": 12,
            "insights_gained": 2,
            "memories_created": 1,
            "speech_seconds": 0,
        }
        assert len(response["history"]) == 1
        assert response["history"][0]["memories_created"] == 1

    assert asyncio.run(get_user_usage(FakeRequest(env))).status_code == 401
    invalid = asyncio.run(get_user_usage(FakeRequest(env, signed_headers(secret), {"period": "week"})))
    assert invalid.status_code == 400


def test_subscription_defaults_to_basic_and_disables_unconfigured_checkout():
    secret = "account-secret"
    env = make_env(secret)
    insert_usage(
        env,
        kind="conversation",
        source_id="monthly-conversation",
        occurred_at=int(datetime.now(timezone.utc).timestamp()),
        seconds=120,
        words=20,
    )

    response = asyncio.run(get_user_subscription(FakeRequest(env, signed_headers(secret))))

    assert response["subscription"]["plan"] == "basic"
    assert response["subscription"]["features"][0] == "1,200 minutes of listening per month"
    assert response["transcription_seconds_used"] == 120
    assert response["transcription_seconds_limit"] == 72_000
    assert response["show_subscription_ui"] is False
    assert response["available_plans"] == []
    assert response["subscription"]["limits"]["chat_questions_per_month"] == 30
    assert response["chat_quota_used"] == 0.0
    assert response["chat_quota_unit"] == "questions"
    assert response["chat_quota_allowed"] is True
    assert asyncio.run(get_user_subscription(FakeRequest(env))).status_code == 401


def test_chat_quota_defaults_to_free_counts_only_current_month_and_blocks_at_limit():
    secret = "account-secret"
    env = make_env(secret)
    now = datetime.now(timezone.utc)
    previous_month = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()) - 1
    insert_chat_event(env, "old", occurred_at=previous_month)
    insert_chat_event(env, "other", uid="other-user")
    for index in range(30):
        insert_chat_event(env, f"current-{index}")

    response = asyncio.run(get_user_chat_usage_quota(FakeRequest(env, signed_headers(secret))))

    assert response == {
        "plan": "Free",
        "plan_type": "basic",
        "unit": "questions",
        "used": 30.0,
        "limit": 30.0,
        "percent": 100.0,
        "allowed": False,
        "reset_at": response["reset_at"],
    }
    assert response["reset_at"] > int(now.timestamp())
    assert asyncio.run(get_user_chat_usage_quota(FakeRequest(env))).status_code == 401


def test_architect_quota_uses_settled_provider_cost_and_rejects_unknown_cost():
    secret = "account-secret"
    env = make_env(secret)
    env.APP_DB.connection.execute(
        "INSERT INTO cf_user_subscriptions (uid, plan, status, updated_at) "
        "VALUES ('account-user', 'architect', 'active', ?)",
        (int(datetime.now(timezone.utc).timestamp()),),
    )
    env.APP_DB.connection.commit()
    insert_chat_event(env, "priced", cost_usd=1.25)

    response = asyncio.run(get_user_chat_usage_quota(FakeRequest(env, signed_headers(secret))))

    assert response["plan"] == "Architect"
    assert response["unit"] == "cost_usd"
    assert response["used"] == 1.25
    assert response["limit"] == 400.0
    insert_chat_event(env, "pending", cost_usd=None, settled=False)
    unavailable = asyncio.run(get_user_chat_usage_quota(FakeRequest(env, signed_headers(secret))))
    assert unavailable.status_code == 503
    assert json.loads(unavailable.body) == {"error": "chat quota unavailable"}


def test_architect_quota_uses_desktop_bucket_cost_without_requiring_desktop_event_settlement():
    secret = "account-secret"
    env = make_env(secret)
    now = int(datetime.now(timezone.utc).timestamp())
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    env.APP_DB.connection.execute(
        "INSERT INTO cf_user_subscriptions (uid, plan, status, updated_at) "
        "VALUES ('account-user', 'architect', 'active', ?)",
        (now,),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_chat_quota_events "
        "(uid, idempotency_key, source, occurred_at) "
        "VALUES ('account-user', 'desktop-message', 'desktop_messages', ?)",
        (now,),
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_llm_usage_daily "
        "(uid, usage_day, usage_kind, feature, model, account, cost_usd, call_count, updated_at) "
        "VALUES ('account-user', ?, 'bucket', 'desktop_chat', '', 'omi', 2.5, 1, ?)",
        (day, now),
    )
    env.APP_DB.connection.commit()

    response = asyncio.run(get_user_chat_usage_quota(FakeRequest(env, signed_headers(secret))))

    assert response["unit"] == "cost_usd"
    assert response["used"] == 2.5
    assert response["allowed"] is True


def test_trial_paywall_uses_signed_account_age_desktop_platform_and_byok_escape():
    secret = "account-secret"
    env = make_env(secret)
    env.TRIAL_PAYWALL_ENABLED = "true"
    now = int(datetime.now(timezone.utc).timestamp())
    old_headers = signed_headers(
        secret,
        account_created_at=now - (4 * 24 * 60 * 60),
        **{"x-app-platform": "macos"},
    )

    paywall = asyncio.run(get_user_paywall_status(FakeRequest(env, old_headers)))
    quota = asyncio.run(get_user_chat_usage_quota(FakeRequest(env, old_headers)))
    trial = asyncio.run(get_user_trial_status(FakeRequest(env, old_headers)))

    assert paywall == {"paywalled": True}
    assert quota["used"] == 30.0
    assert quota["allowed"] is False
    assert trial["trial_started_at"] == now - (4 * 24 * 60 * 60)
    assert trial["trial_remaining_seconds"] == 0
    assert trial["trial_expired"] is True
    mobile = asyncio.run(
        get_user_paywall_status(FakeRequest(env, {**old_headers, "x-app-platform": "ios"}, {"platform": "desktop"}))
    )
    assert mobile == {"paywalled": False}
    byok = asyncio.run(
        get_user_paywall_status(
            FakeRequest(
                env,
                {
                    **old_headers,
                    "x-byok-openai": "key",
                    "x-byok-anthropic": "key",
                    "x-byok-gemini": "key",
                    "x-byok-deepgram": "key",
                },
            )
        )
    )
    assert byok == {"paywalled": False}


def test_trial_routes_fail_open_when_disabled_paid_or_account_age_is_missing():
    secret = "account-secret"
    env = make_env(secret)
    headers = signed_headers(secret, **{"x-app-platform": "desktop"})

    assert asyncio.run(get_user_paywall_status(FakeRequest(env, headers))) == {"paywalled": False}
    trial = asyncio.run(get_user_trial_status(FakeRequest(env, headers)))
    assert trial["trial_expired"] is False
    assert trial["trial_started_at"] is None
    assert trial["trial_duration_seconds"] == 259_200

    env.TRIAL_PAYWALL_ENABLED = "true"
    env.APP_DB.connection.execute(
        "INSERT INTO cf_user_subscriptions (uid, plan, status, updated_at) "
        "VALUES ('account-user', 'operator', 'active', ?)",
        (int(datetime.now(timezone.utc).timestamp()),),
    )
    env.APP_DB.connection.commit()
    paid = asyncio.run(
        get_user_paywall_status(
            FakeRequest(
                env,
                signed_headers(
                    secret,
                    account_created_at=int(datetime.now(timezone.utc).timestamp()) - 1_000_000,
                    **{"x-app-platform": "desktop"},
                ),
            )
        )
    )
    assert paid == {"paywalled": False}


def test_basic_subscription_enables_checkout_only_after_catalog_is_populated():
    secret = "account-secret"
    env = make_env(secret)
    insert_price(env, "price-plus", "plus")

    response = asyncio.run(get_user_subscription(FakeRequest(env, signed_headers(secret))))

    assert response["subscription"]["plan"] == "basic"
    assert response["show_subscription_ui"] is True


def test_price_catalog_enforces_web_mobile_desktop_and_legacy_neo_audiences():
    secret = "account-secret"
    env = make_env(secret)
    for plan in ("plus", "unlimited_v2", "operator", "architect", "unlimited"):
        insert_price(env, f"price-{plan}", plan)

    web = asyncio.run(get_available_plans(FakeRequest(env, signed_headers(secret))))
    assert {plan["plan_id"] for plan in web["plans"]} == {"plus", "unlimited_v2", "operator", "architect"}
    mobile = asyncio.run(get_available_plans(FakeRequest(env, signed_headers(secret, **{"x-app-platform": "ios"}))))
    assert {plan["plan_id"] for plan in mobile["plans"]} == {"plus", "unlimited_v2"}
    desktop = asyncio.run(get_available_plans(FakeRequest(env, signed_headers(secret, **{"x-app-platform": "macos"}))))
    assert {plan["plan_id"] for plan in desktop["plans"]} == {"operator", "architect"}

    env.APP_DB.connection.execute(
        "INSERT INTO cf_user_subscriptions (uid, plan, status, updated_at) VALUES (?, 'unlimited', 'active', ?)",
        ("account-user", int(datetime.now().timestamp())),
    )
    env.APP_DB.connection.commit()
    legacy_mobile = asyncio.run(
        get_available_plans(FakeRequest(env, signed_headers(secret, **{"x-app-platform": "android"})))
    )
    assert {plan["plan_id"] for plan in legacy_mobile["plans"]} == {
        "plus",
        "unlimited_v2",
        "unlimited",
    }


def test_imported_subscription_uses_d1_state_and_only_enables_a_populated_catalog():
    secret = "account-secret"
    env = make_env(secret)
    insert_price(env, "price-architect", "architect")
    env.APP_DB.connection.execute(
        "INSERT INTO cf_user_subscriptions "
        "(uid, plan, status, current_period_end, stripe_subscription_id, current_price_id, "
        "features_json, show_subscription_ui, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "account-user",
            "architect",
            "active",
            2_000_000_000,
            "sub_test",
            "price-architect",
            json.dumps(["Imported feature"]),
            1,
            int(datetime.now().timestamp()),
        ),
    )
    env.APP_DB.connection.commit()

    response = asyncio.run(get_user_subscription(FakeRequest(env, signed_headers(secret))))

    assert response["subscription"]["plan"] == "architect"
    assert response["subscription"]["stripe_subscription_id"] == "sub_test"
    assert response["subscription"]["features"] == ["Imported feature"]
    assert response["show_subscription_ui"] is True
    catalog = asyncio.run(get_available_plans(FakeRequest(env, signed_headers(secret))))
    assert catalog["plans"][0]["is_active"] is True


def test_account_reads_fail_closed_when_d1_is_unavailable():
    secret = "account-secret"

    class FailingDb:
        def prepare(self, _sql):
            raise RuntimeError("D1 unavailable")

    env = type("Env", (), {"APP_DB": FailingDb(), "INTERNAL_ASSERTION_SECRET": secret})()
    request = FakeRequest(env, signed_headers(secret))

    usage = asyncio.run(get_user_usage(request))
    subscription = asyncio.run(get_user_subscription(request))
    catalog = asyncio.run(get_available_plans(request))
    quota = asyncio.run(get_user_chat_usage_quota(request))

    assert usage.status_code == 503
    assert subscription.status_code == 503
    assert catalog.status_code == 503
    assert quota.status_code == 503
