import asyncio
import base64
import hashlib
import hmac
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import realtime_routes as routes  # noqa: E402


class FakeDb:
    def __init__(self, *, before_events=False):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        for migration in sorted((Path(__file__).parents[3] / "migrations/app").glob('*.sql')):
            if before_events and migration.name >= '0164_realtime_usage_events.sql':
                break
            self.connection.executescript(migration.read_text())

    def prepare(self, sql):
        return FakeStatement(self.connection, sql)

    async def batch(self, statements):
        try:
            self.connection.execute('BEGIN')
            for statement in statements:
                self.connection.execute(statement.sql, statement.args)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


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

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        return dict(row) if row else None


class FakeRequest:
    def __init__(self, env, headers, body):
        self.scope = {"env": env}
        self.headers = headers
        self.body = body

    async def json(self):
        return self.body


def signed_headers(secret: str, uid: str = "realtime-user", **extra):
    raw = json.dumps(
        {"uid": uid, "authority": "better-auth", "requestId": "realtime-test", **extra},
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
    assert json.loads(calls["options"]["body"]) == {"session": {"type": "realtime", "model": "gpt-realtime-2"}}
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
    assert tuple(row) == (200, 100, 50, 40, 20, 410, 6_060, 2)

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


def test_realtime_usage_accepts_native_workers_ai_without_external_cost():
    secret = "realtime-secret"
    database = FakeDb()
    env = type("Env", (), {"INTERNAL_ASSERTION_SECRET": secret, "APP_DB": database})()
    response = asyncio.run(
        routes.report_realtime_usage(
            FakeRequest(
                env,
                signed_headers(secret),
                {"provider": "workers-ai", "input_text_tokens": 7, "output_text_tokens": 3},
            )
        )
    )
    assert response.status_code == 204
    row = database.connection.execute("SELECT total_tokens, cost_micros FROM cf_realtime_usage").fetchone()
    assert tuple(row) == (10, 0)


@pytest.mark.parametrize(
    'provider,text,audio,cached,expected_micros',
    [
        ('openai', 100, 0, 40, 256),
        ('openai', 5, 10, 20, 6),
        (' openai ', 100, 0, 40, 256),
        ('gemini', 100, 0, 40, 75),
        ('gemini', 6, 0, 0, 5),
        ('workers-ai', 100, 50, 25, 0),
    ],
)
def test_realtime_cost_prices_cached_input_as_a_subset(provider, text, audio, cached, expected_micros):
    # Same contract as upstream client_reported_turn/client_reported_cost_usd:
    # text-first cache attribution, fixed issued model, integer half-up micros.
    report = routes.UsageReport(
        provider=provider,
        model='caller-cannot-select-rates',
        input_text_tokens=text,
        input_audio_tokens=audio,
        input_cached_tokens=cached,
    )
    assert routes._usage_cost(report) == expected_micros / 1_000_000


def test_realtime_turn_retry_keeps_one_usage_event_across_days(monkeypatch):
    secret = 'realtime-secret'
    db = FakeDb()
    env = type('Env', (), {'APP_DB': db, 'INTERNAL_ASSERTION_SECRET': secret})()
    body = {'provider': 'openai', 'turn_id': 'stable-turn', 'input_text_tokens': 100, 'input_cached_tokens': 40}
    monkeypatch.setattr(routes.time, 'time', lambda: 1_788_681_600)
    assert asyncio.run(routes.report_realtime_usage(FakeRequest(env, signed_headers(secret), body))).status_code == 204
    monkeypatch.setattr(routes.time, 'time', lambda: 1_788_768_000)
    assert asyncio.run(routes.report_realtime_usage(FakeRequest(env, signed_headers(secret), body))).status_code == 204
    assert db.connection.execute('SELECT sum(call_count) FROM cf_realtime_usage').fetchone()[0] == 1
    assert db.connection.execute('SELECT count(*) FROM cf_chat_quota_events').fetchone()[0] == 1
    assert db.connection.execute('SELECT sum(cost_usd) FROM cf_llm_usage_daily').fetchone()[0] == 0.000256


def test_realtime_usage_transaction_failure_rolls_back_quota_and_every_projection():
    secret = 'realtime-secret'
    db = FakeDb()
    db.connection.executescript(
        "CREATE TRIGGER fail_realtime_bucket BEFORE INSERT ON cf_llm_usage_daily "
        "BEGIN SELECT RAISE(ABORT, 'private ledger failure'); END;"
    )
    env = type('Env', (), {'APP_DB': db, 'INTERNAL_ASSERTION_SECRET': secret})()
    request = FakeRequest(env, signed_headers(secret), {'provider': 'openai', 'turn_id': 'one', 'input_text_tokens': 1})
    response = asyncio.run(routes.report_realtime_usage(request))
    assert response.status_code == 502
    for table in ('cf_realtime_usage', 'cf_realtime_usage_events', 'cf_chat_quota_events', 'cf_llm_usage_daily'):
        assert db.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    db.connection.execute('DROP TRIGGER fail_realtime_bucket')
    assert asyncio.run(routes.report_realtime_usage(request)).status_code == 204
    assert db.connection.execute('SELECT count(*) FROM cf_realtime_usage_events').fetchone()[0] == 1


def test_missing_realtime_accounting_is_not_success():
    secret = 'realtime-secret'
    env = type('Env', (), {'INTERNAL_ASSERTION_SECRET': secret})()
    request = FakeRequest(env, signed_headers(secret), {'provider': 'openai', 'input_text_tokens': 1})
    assert asyncio.run(routes.report_realtime_usage(request)).status_code == 502


def test_realtime_quota_admission_is_atomic_and_retry_works_at_the_cap():
    secret = 'realtime-secret'
    db = FakeDb()
    env = type(
        'Env',
        (),
        {
            'APP_DB': db,
            'INTERNAL_ASSERTION_SECRET': secret,
            'FREE_CHAT_QUESTIONS_PER_MONTH': 1,
        },
    )()

    async def report(turn):
        return await routes.report_realtime_usage(
            FakeRequest(
                env,
                signed_headers(secret),
                {'provider': 'openai', 'turn_id': turn, 'input_text_tokens': 1},
            )
        )

    async def compete():
        return await asyncio.gather(report('one'), report('two'))

    responses = asyncio.run(compete())
    assert sorted(response.status_code for response in responses) == [204, 402]
    accepted = 'one' if responses[0].status_code == 204 else 'two'
    assert asyncio.run(report(accepted)).status_code == 204
    assert db.connection.execute('SELECT count(*) FROM cf_chat_quota_events').fetchone()[0] == 1
    assert db.connection.execute('SELECT sum(call_count) FROM cf_llm_usage_daily').fetchone()[0] == 1


def test_realtime_turn_identity_is_uid_scoped_and_native_aliases_do_not_charge_chat():
    secret = 'realtime-secret'
    db = FakeDb()
    env = type('Env', (), {'APP_DB': db, 'INTERNAL_ASSERTION_SECRET': secret})()
    for uid in ('one', 'two'):
        for provider in ('workers-ai', 'cloudflare-workers-ai'):
            body = {'provider': provider, 'turn_id': 'same-id', 'input_text_tokens': 100}
            assert (
                asyncio.run(
                    routes.report_realtime_usage(FakeRequest(env, signed_headers(secret, uid), body))
                ).status_code
                == 204
            )
    assert db.connection.execute('SELECT count(*) FROM cf_realtime_usage_events').fetchone()[0] == 2
    assert db.connection.execute('SELECT sum(call_count) FROM cf_realtime_usage').fetchone()[0] == 2
    assert db.connection.execute('SELECT count(*) FROM cf_chat_quota_events').fetchone()[0] == 0
    assert db.connection.execute('SELECT count(*) FROM cf_llm_usage_daily').fetchone()[0] == 0


@pytest.mark.parametrize('provider', ['openai', 'workers-ai'])
@pytest.mark.parametrize('fence', ['intent', 'tombstone'])
def test_late_realtime_report_cannot_recreate_accounting(provider, fence):
    secret = 'realtime-secret'
    db = FakeDb()
    env = type('Env', (), {'APP_DB': db, 'INTERNAL_ASSERTION_SECRET': secret})()
    if fence == 'intent':
        db.connection.execute(
            "INSERT INTO cf_account_deletion_intents "
            "(uid, job_id, status, phase, next_attempt_at, created_at, updated_at) "
            "VALUES ('realtime-user', 'delete-job', 'pending', 'quiescing', 1, 1, 1)"
        )
    else:
        db.connection.execute("INSERT INTO cf_account_deletion_tombstones VALUES ('realtime-user', 1, 9999999999)")
    db.connection.commit()
    body = {'provider': provider, 'turn_id': 'late', 'input_text_tokens': 1}
    assert asyncio.run(routes.report_realtime_usage(FakeRequest(env, signed_headers(secret), body))).status_code == 502
    for table in ('cf_realtime_usage', 'cf_realtime_usage_events', 'cf_chat_quota_events', 'cf_llm_usage_daily'):
        assert db.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


@pytest.mark.parametrize('created_at,status', [(None, 204), (1, 402)])
def test_realtime_trial_keeps_legacy_missing_timestamp_behavior(created_at, status):
    secret = 'realtime-secret'
    db = FakeDb()
    env = type('Env', (), {'APP_DB': db, 'INTERNAL_ASSERTION_SECRET': secret, 'TRIAL_PAYWALL_ENABLED': 'true'})()
    headers = signed_headers(secret, accountCreatedAt=created_at, byokActive=True)
    body = {'provider': 'openai', 'turn_id': 'trial', 'input_text_tokens': 1}
    # A BYOK indication cannot exempt a managed realtime report.
    assert asyncio.run(routes.report_realtime_usage(FakeRequest(env, headers, body))).status_code == status


def test_realtime_retry_retains_first_amount_and_server_selected_model():
    secret = 'realtime-secret'
    db = FakeDb()
    env = type('Env', (), {'APP_DB': db, 'INTERNAL_ASSERTION_SECRET': secret})()
    body = {'provider': 'openai', 'turn_id': 'one', 'model': 'untrusted-model', 'input_text_tokens': 10}
    for payload in (body, {**body, 'provider': 'gemini', 'input_text_tokens': 100}):
        assert (
            asyncio.run(routes.report_realtime_usage(FakeRequest(env, signed_headers(secret), payload))).status_code
            == 204
        )
    row = db.connection.execute('SELECT * FROM cf_realtime_usage_events').fetchone()
    assert row['provider'] == 'openai' and row['model'] == routes.OPENAI_REALTIME_MODEL
    assert row['input_text_tokens'] == 10 and row['cost_micros'] == 40
    assert 'one' not in row['idempotency_key']
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        db.connection.execute('UPDATE cf_realtime_usage_events SET cost_micros = 0')


def test_realtime_upgrade_preserves_legacy_totals_without_inventing_turns():
    db = FakeDb(before_events=True)
    db.connection.execute(
        "INSERT INTO cf_realtime_usage (uid, usage_date, total_tokens, cost_micros, call_count, updated_at) "
        "VALUES ('legacy-user', '2026-09-01', 1000, 500, 2, 1)"
    )
    db.connection.commit()
    before = tuple(db.connection.execute('SELECT * FROM cf_realtime_usage').fetchone())
    migration = Path(__file__).parents[3] / 'migrations/app/0164_realtime_usage_events.sql'
    db.connection.executescript(migration.read_text())
    assert tuple(db.connection.execute('SELECT * FROM cf_realtime_usage').fetchone()) == before
    for table in ('cf_realtime_usage_events', 'cf_chat_quota_events', 'cf_llm_usage_daily'):
        assert db.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
