import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from llm_usage_routes import (  # noqa: E402
    RecordLlmUsageBucketRequest,
    get_llm_top_features,
    get_llm_usage,
    get_total_llm_cost,
    record_llm_usage_bucket,
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


def signed_headers(secret: str, uid: str = "usage-user"):
    raw = json.dumps({"uid": uid}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return {
        "x-omi-auth-context": encoded,
        "x-omi-internal-signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def make_env(secret="usage-secret"):
    return type("Env", (), {"APP_DB": FakeDb(), "INTERNAL_ASSERTION_SECRET": secret})()


def insert_feature(env, *, feature, input_tokens, output_tokens, call_count, usage_day=None, uid="usage-user"):
    day = usage_day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    env.APP_DB.connection.execute(
        "INSERT INTO cf_llm_usage_daily "
        "(uid, usage_day, usage_kind, feature, model, account, input_tokens, output_tokens, "
        "total_tokens, call_count, updated_at) VALUES (?, ?, 'feature', ?, '@cf/test', 'omi', ?, ?, ?, ?, ?)",
        (uid, day, feature, input_tokens, output_tokens, input_tokens + output_tokens, call_count, 1),
    )
    env.APP_DB.connection.commit()


def test_bucket_records_atomically_per_account_and_total_cost_avoids_alias_double_count():
    secret = "usage-secret"
    env = make_env(secret)
    request = FakeRequest(env, signed_headers(secret))

    for payload in (
        RecordLlmUsageBucketRequest(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.1),
        RecordLlmUsageBucketRequest(input_tokens=2, output_tokens=3, total_tokens=5, cost_usd=0.02),
        RecordLlmUsageBucketRequest(input_tokens=7, output_tokens=1, total_tokens=8, cost_usd=0.06, account="personal"),
    ):
        assert asyncio.run(record_llm_usage_bucket(request, payload)) == {"status": "ok"}

    rows = env.APP_DB.connection.execute(
        "SELECT account, input_tokens, output_tokens, total_tokens, cost_usd, call_count "
        "FROM cf_llm_usage_daily WHERE usage_kind = 'bucket' ORDER BY account"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "account": "omi",
            "input_tokens": 12,
            "output_tokens": 8,
            "total_tokens": 20,
            "cost_usd": 0.12000000000000001,
            "call_count": 2,
        },
        {
            "account": "personal",
            "input_tokens": 7,
            "output_tokens": 1,
            "total_tokens": 8,
            "cost_usd": 0.06,
            "call_count": 1,
        },
    ]
    assert asyncio.run(get_total_llm_cost(request)) == {"total_cost_usd": 0.18}


def test_feature_summary_filters_days_uid_and_bucket_rows_then_sorts_top_features():
    secret = "usage-secret"
    env = make_env(secret)
    request = FakeRequest(env, signed_headers(secret))
    today = datetime.now(timezone.utc)
    insert_feature(env, feature="chat", input_tokens=100, output_tokens=20, call_count=2)
    insert_feature(env, feature="memory", input_tokens=40, output_tokens=10, call_count=1)
    insert_feature(
        env,
        feature="old",
        input_tokens=1_000,
        output_tokens=1_000,
        call_count=1,
        usage_day=(today - timedelta(days=31)).strftime("%Y-%m-%d"),
    )
    insert_feature(env, feature="other", input_tokens=9_000, output_tokens=9_000, call_count=1, uid="other-user")
    env.APP_DB.connection.execute(
        "INSERT INTO cf_llm_usage_daily "
        "(uid, usage_day, usage_kind, feature, model, account, input_tokens, output_tokens, "
        "total_tokens, cost_usd, call_count, updated_at) "
        "VALUES ('usage-user', ?, 'bucket', 'desktop_chat', '', 'omi', 500, 500, 1000, 1, 1, 1)",
        (today.strftime("%Y-%m-%d"),),
    )
    env.APP_DB.connection.commit()

    response = asyncio.run(get_llm_usage(request, days=30))
    top = asyncio.run(get_llm_top_features(request, days=30, limit=1))

    assert response == {
        "summary": {
            "chat": {"input_tokens": 100, "output_tokens": 20, "call_count": 2},
            "memory": {"input_tokens": 40, "output_tokens": 10, "call_count": 1},
        },
        "top_features": [
            {"feature": "chat", "input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "call_count": 2},
            {"feature": "memory", "input_tokens": 40, "output_tokens": 10, "total_tokens": 50, "call_count": 1},
        ],
        "period_days": 30,
    }
    assert top == [response["top_features"][0]]


def test_chat_settlement_backfills_and_then_increments_exactly_once():
    migration_dir = Path(__file__).parents[3] / "migrations/app"
    connection = sqlite3.connect(":memory:")
    connection.executescript(
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
    ):
        connection.executescript((migration_dir / name).read_text())
    connection.execute(
        "INSERT INTO cf_chat_quota_events "
        "(uid, idempotency_key, source, occurred_at, cost_usd, prompt_tokens, completion_tokens, model, settled_at) "
        "VALUES ('usage-user', 'before', 'v2_messages', 1787958000, 0.25, 100, 20, '@cf/test', 1787958001)"
    )
    connection.executescript((migration_dir / "0056_llm_usage_daily.sql").read_text())
    connection.execute(
        "INSERT INTO cf_chat_quota_events "
        "(uid, idempotency_key, source, occurred_at, cost_usd, prompt_tokens, completion_tokens, model) "
        "VALUES ('usage-user', 'after', 'v2_messages', 1787958000, 0.1, 50, 10, '@cf/test')"
    )
    connection.execute(
        "UPDATE cf_chat_quota_events SET settled_at = 1787958002 WHERE uid = 'usage-user' AND idempotency_key = 'after'"
    )
    connection.execute(
        "UPDATE cf_chat_quota_events SET settled_at = 1787958003 WHERE uid = 'usage-user' AND idempotency_key = 'after'"
    )
    row = connection.execute(
        "SELECT input_tokens, output_tokens, total_tokens, cost_usd, call_count "
        "FROM cf_llm_usage_daily WHERE uid = 'usage-user' AND feature = 'chat'"
    ).fetchone()
    assert row == (150, 30, 180, 0.35, 2)


def test_routes_authenticate_and_fail_closed_when_d1_is_unavailable():
    env = make_env()
    assert asyncio.run(get_llm_usage(FakeRequest(env), days=30)).status_code == 401
    assert asyncio.run(get_total_llm_cost(FakeRequest(env))).status_code == 401

    class FailingDb:
        def prepare(self, _sql):
            raise RuntimeError("D1 unavailable")

    failing = type("Env", (), {"APP_DB": FailingDb(), "INTERNAL_ASSERTION_SECRET": "usage-secret"})()
    request = FakeRequest(failing, signed_headers("usage-secret"))
    assert asyncio.run(get_llm_usage(request, days=30)).status_code == 503
    assert asyncio.run(get_llm_top_features(request, days=30, limit=3)).status_code == 503
    assert asyncio.run(get_total_llm_cost(request)).status_code == 503
    assert asyncio.run(record_llm_usage_bucket(request, RecordLlmUsageBucketRequest())).status_code == 503
