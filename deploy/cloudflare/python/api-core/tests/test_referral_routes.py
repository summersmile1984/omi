import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from referral_routes import router
from account_routes import router as account_router
from user_export_routes import router as export_router


def code(uid='sender'):
    payload = 'ref1.' + base64.urlsafe_b64encode(uid.encode()).decode().rstrip('=')
    digest = hmac.new(b'r' * 32, f'omi-desktop-referral:{payload}'.encode(), hashlib.sha256).digest()
    return payload + '.' + base64.urlsafe_b64encode(digest).decode().rstrip('=')


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.now = 1788681600
        self.before_batch = None
        self.connection.create_function('unixepoch', 0, lambda: self.now)
        self.connection.execute('PRAGMA foreign_keys = ON')
        for path in sorted((Path(__file__).parents[3] / 'migrations/app').glob('*.sql')):
            self.connection.executescript(path.read_text())

    def prepare(self, sql):
        db = self

        class Statement:
            args = ()

            def __init__(self):
                self.sql = sql

            def bind(self, *args):
                self.args = args
                return self

            async def first(self):
                row = db.connection.execute(sql, self.args).fetchone()
                return dict(row) if row else None

            async def all(self):
                return {'results': [dict(row) for row in db.connection.execute(sql, self.args)]}

        return Statement()

    async def batch(self, statements):
        if self.before_batch:
            action, self.before_batch = self.before_batch, None
            action()
        self.connection.commit()
        try:
            self.connection.execute('BEGIN')
            for statement in statements:
                self.connection.execute(statement.sql, statement.args)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise


@pytest.fixture
def target():
    db = Database()
    env = SimpleNamespace(
        APP_DB=db,
        INTERNAL_ASSERTION_SECRET='internal-test',
        REFERRAL_SIGNING_SECRET='r' * 32,
        PUBLIC_API_BASE_URL='https://api.eddy.test',
        PUBLIC_WEB_BASE_URL='https://eddy.test',
    )
    app = FastAPI()
    for routes in (router, account_router, export_router):
        app.include_router(routes)

    @app.middleware('http')
    async def environment(request, call_next):
        request.scope['env'] = env
        return await call_next(request)

    async def call(method, path='/v1/users/me/referral/claim', *, uid='recipient', age=0, body=None):
        headers = {}
        if uid:
            context = {'uid': uid, 'authority': 'better-auth'}
            if age is not None:
                context['accountCreatedAt'] = db.now - age
            encoded = base64.urlsafe_b64encode(json.dumps(context).encode()).decode().rstrip('=')
            signature = hmac.new(b'internal-test', encoded.encode(), hashlib.sha256).digest()
            headers = {
                'x-omi-auth-context': encoded,
                'x-omi-internal-signature': base64.urlsafe_b64encode(signature).decode().rstrip('='),
            }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://api.eddy.test') as client:
            return await client.request(
                method, path, headers=headers, json=body if body is not None else {'code': code()}
            )

    return db, env, call


def test_referral_link_uses_upstream_signature_and_branded_redirect(target):
    db, env, call = target
    response = asyncio.run(call('GET', '/v1/users/me/referral', uid='sender'))
    assert response.json() == {'referral_url': 'https://api.eddy.test/r/' + code()}
    capture = asyncio.run(call('GET', '/r/' + code(), uid=None))
    assert capture.status_code == 302
    assert capture.headers['location'] == 'https://eddy.test/login?referral=' + code() + '&environment=prod'
    cookie = capture.headers['set-cookie']
    assert all(
        part in cookie for part in ['omi_desktop_referral=', 'HttpOnly', 'Secure', 'SameSite=lax', 'Max-Age=2592000']
    )
    assert capture.headers['cache-control'] == 'no-store'
    assert db.connection.execute('SELECT count(*) FROM cf_referral_claims').fetchone()[0] == 0


def test_atomic_grant_retry_export_and_expiry(target):
    db, env, call = target
    assert asyncio.run(call('POST')).json() == {'claimed': True, 'trial_days': 30}
    original = tuple(db.connection.execute('SELECT * FROM cf_user_subscriptions').fetchone())
    assert asyncio.run(call('POST', body={'code': code('different-sender')})).json()['claimed'] is False
    assert tuple(db.connection.execute('SELECT * FROM cf_user_subscriptions').fetchone()) == original
    subscription = asyncio.run(call('GET', '/v1/users/me/subscription')).json()['subscription']
    assert subscription['plan'] == 'operator' and subscription['cancel_at_period_end'] is True
    assert subscription['current_period_end'] - subscription['current_period_start'] == 30 * 86400
    export = asyncio.run(call('GET', '/v1/users/export')).json()
    assert len(export['referral_claims']) == len(export['referral_attributions']) == 1
    assert export['referral_attributions'][0]['sender_uid'] == 'sender'
    assert asyncio.run(call('GET', '/v1/users/export', uid='other')).json()['referral_claims'] == []
    db.now += 30 * 86400 - 1
    assert db.connection.execute('SELECT plan FROM cf_effective_user_subscriptions').fetchone()[0] == 'operator'
    db.now += 1
    assert asyncio.run(call('GET', '/v1/users/me/subscription')).json()['subscription']['plan'] == 'basic'


@pytest.mark.parametrize('age,claimed', [(0, True), (900, True), (901, False), (-1, False), (None, False)])
def test_new_account_window_including_unmigrated_missing_timestamp(target, age, claimed):
    db, env, call = target
    assert asyncio.run(call('POST', age=age)).json()['claimed'] is claimed


def test_concurrent_claims_have_one_winner_and_independent_accounts(target):
    db, env, call = target

    async def competing():
        return await asyncio.gather(*(call('POST') for _ in range(4)))

    assert sum(response.json()['claimed'] for response in asyncio.run(competing())) == 1
    assert asyncio.run(call('POST', uid='other')).json()['claimed'] is True
    assert asyncio.run(call('POST', uid='sender')).json()['claimed'] is False


@pytest.mark.parametrize('plan', ['operator', 'architect', 'unlimited', 'plus', 'unlimited_v2'])
def test_paid_plan_is_never_replaced_even_when_inactive(target, plan):
    db, env, call = target
    db.connection.execute(
        "INSERT INTO cf_user_subscriptions (uid, plan, status, updated_at) VALUES ('recipient', ?, 'inactive', 1)",
        (plan,),
    )
    assert asyncio.run(call('POST')).json()['claimed'] is False
    assert db.connection.execute('SELECT plan FROM cf_user_subscriptions').fetchone()[0] == plan


def test_projection_failure_rolls_back_claim_and_is_retryable(target):
    db, env, call = target
    db.connection.executescript(
        "CREATE TRIGGER fail_attribution BEFORE INSERT ON cf_referral_attributions BEGIN SELECT RAISE(ABORT, 'private failure'); END;"
    )
    response = asyncio.run(call('POST'))
    assert response.status_code == 503 and 'private failure' not in response.text
    for table in ['cf_referral_claims', 'cf_referral_attributions', 'cf_user_subscriptions']:
        assert db.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    db.connection.execute('DROP TRIGGER fail_attribution')
    assert asyncio.run(call('POST')).json()['claimed'] is True


@pytest.mark.parametrize('fence', ['intent', 'tombstone'])
def test_deletion_between_admission_and_commit_rejects_grant(target, fence):
    db, env, call = target

    def delete():
        if fence == 'intent':
            db.connection.execute(
                "INSERT INTO cf_account_deletion_intents (uid, job_id, status, phase, next_attempt_at, created_at, updated_at) VALUES ('recipient', 'job', 'pending', 'quiescing', 1, 1, 1)"
            )
        else:
            db.connection.execute("INSERT INTO cf_account_deletion_tombstones VALUES ('recipient', 1, 9999999999)")

    db.before_batch = delete
    assert asyncio.run(call('POST')).status_code == 503
    assert db.connection.execute('SELECT count(*) FROM cf_referral_claims').fetchone()[0] == 0


def test_later_paid_subscription_survives_referral_expiry_and_sender_erasure(target):
    db, env, call = target
    assert asyncio.run(call('POST')).json()['claimed'] is True
    # The deletion registry purges inviter attribution, never the recipient receipt.
    db.connection.execute("DELETE FROM cf_referral_attributions WHERE sender_uid = 'sender'")
    assert asyncio.run(call('POST', body={'code': code('second')})).json()['claimed'] is False
    db.connection.execute(
        "UPDATE cf_user_subscriptions SET stripe_subscription_id = 'sub_paid' WHERE uid = 'recipient'"
    )
    db.now += 31 * 86400
    assert db.connection.execute('SELECT plan FROM cf_effective_user_subscriptions').fetchone()[0] == 'operator'


def test_invalid_missing_and_tampered_credentials_never_grant(target):
    db, env, call = target
    assert asyncio.run(call('POST', uid=None)).status_code == 401
    assert asyncio.run(call('POST', body={'code': 'bad'})).status_code == 404
    assert asyncio.run(call('POST', body={'code': code() + 'a'})).status_code == 404
    assert asyncio.run(call('POST', body={'code': 12})).status_code == 422
    env.REFERRAL_SIGNING_SECRET = ''
    assert asyncio.run(call('GET', '/v1/users/me/referral')).status_code == 503
    assert db.connection.execute('SELECT count(*) FROM cf_referral_claims').fetchone()[0] == 0
