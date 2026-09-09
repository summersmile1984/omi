import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import FastAPI
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from email_preference_routes import NEUTRAL_ERROR_HTML, router  # noqa: E402
from internal_auth import verify_request_context  # noqa: E402
from user_export_routes import router as export_router  # noqa: E402


# Independent issuer with the upstream lifecycle.py wire format. No clocks or
# session state participate in this deliberately unexpiring capability.
def signed_token(uid='owner', secret='email-test-secret', purpose='lifecycle'):
    encoded = base64.urlsafe_b64encode(uid.encode()).decode().rstrip('=')
    signature = hmac.new(secret.encode(), f'{uid}:{purpose}'.encode(), hashlib.sha256).digest()
    return encoded + '.' + base64.urlsafe_b64encode(signature).decode().rstrip('=')


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.fail = False
        self.before_write = None
        for path in sorted((Path(__file__).parents[3] / 'migrations/app').glob('*.sql')):
            self.connection.executescript(path.read_text())

    def prepare(self, sql):
        database = self

        class Statement:
            args = ()

            def bind(self, *args):
                self.args = args
                return self

            def execute(self):
                if database.fail:
                    raise RuntimeError('private database error')
                if sql.startswith('INSERT INTO cf_user_email_preferences') and database.before_write:
                    database.before_write()
                return database.connection.execute(sql, self.args)

            async def first(self):
                row = self.execute().fetchone()
                database.connection.commit()
                return dict(row) if row else None

            async def all(self):
                return {'results': [dict(row) for row in self.execute().fetchall()]}

        return Statement()


class Auth:
    users = {'owner', 'other'}
    fail = False
    wrong_identity = False
    calls = 0

    async def fetch(self, url, *, method, headers):
        self.calls += 1
        assert url == 'https://auth.internal/internal/profile' and method == 'GET'
        context = verify_request_context(
            headers['x-omi-auth-context'],
            headers['x-omi-internal-signature'],
            'internal-test-secret',
            audience='auth',
            method='GET',
            path='/internal/profile',
        )
        assert context
        if self.fail:
            raise RuntimeError('private auth error')
        uid = context['uid']

        class Response:
            status = 200 if uid in self.users else 410

            async def json(inner):
                return {'uid': 'different' if self.wrong_identity else uid}

        return Response()


@pytest.fixture
def target():
    database, auth = Database(), Auth()
    env = SimpleNamespace(
        APP_DB=database,
        AUTH=auth,
        INTERNAL_ASSERTION_SECRET='internal-test-secret',
        LIFECYCLE_EMAIL_SIGNING_SECRET='email-test-secret',
        BRAND_RUNTIME_JSON=json.dumps({'brand_id': 'eddy', 'display_name': 'Eddy <&>', 'ai_persona_name': 'Eddy'}),
    )
    app = FastAPI()
    app.include_router(router)
    app.include_router(export_router)

    @app.middleware('http')
    async def environment(request, call_next):
        request.scope['env'] = env
        return await call_next(request)

    def request(
        method, token=None, *, body=None, path=None, uid=None, content_type='application/x-www-form-urlencoded'
    ):
        headers = {'content-type': content_type}
        if uid:
            encoded = base64.urlsafe_b64encode(json.dumps({'uid': uid}).encode()).decode().rstrip('=')
            signature = hmac.new(b'internal-test-secret', encoded.encode(), hashlib.sha256).digest()
            headers.update(
                {
                    'x-omi-auth-context': encoded,
                    'x-omi-internal-signature': base64.urlsafe_b64encode(signature).decode().rstrip('='),
                }
            )
        route = path or '/email/unsubscribe' + ('' if token is None else '?token=' + quote(token, safe=''))

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url='https://core.test'
            ) as client:
                return await client.request(method, route, content=body, headers=headers)

        return asyncio.run(run())

    yield env, request
    database.connection.close()


def test_existing_account_without_preferences_get_is_read_only_and_post_needs_no_session(target):
    env, request = target
    with pytest.raises(sqlite3.IntegrityError, match='NOT NULL'):
        env.APP_DB.connection.execute('INSERT INTO cf_user_email_preferences (uid) VALUES (NULL)')
    token = signed_token()
    for _ in range(2):
        page = request('GET', token)
        assert page.status_code == 200 and 'Unsubscribe from Eddy &lt;&amp;&gt; emails?' in page.text
        assert f'<form method="post" action="/email/unsubscribe?token={token}">' in page.text
        assert page.headers['cache-control'] == 'no-store'
        assert page.headers['referrer-policy'] == 'no-referrer'
    assert not env.APP_DB.connection.execute('SELECT * FROM cf_user_email_preferences').fetchall()
    for body, content_type in (
        ('List-Unsubscribe=One-Click', 'application/x-www-form-urlencoded'),
        ('unparsed', 'multipart/form-data'),
        ('{malformed json', 'application/json'),
        (None, 'text/plain'),
    ):
        result = request('POST', token, body=body, content_type=content_type, uid='other')
        assert result.status_code == 200 and 'You have been unsubscribed.' in result.text
        assert token not in result.text and 'owner' not in result.text
    rows = [dict(row) for row in env.APP_DB.connection.execute('SELECT * FROM cf_user_email_preferences')]
    assert len(rows) == 1 and rows[0]['uid'] == 'owner' and rows[0]['lifecycle_opted_out'] == 1
    assert isinstance(rows[0]['lifecycle_opted_out_at'], int)
    exported = request('GET', path='/v1/users/export', uid='owner').json()
    assert exported['email_preferences'] == [{key: value for key, value in rows[0].items() if key != 'uid'}]
    assert request('GET', path='/v1/users/export', uid='other').json()['email_preferences'] == []


@pytest.mark.parametrize(
    'token',
    [
        None,
        '',
        'bad',
        'x.' + 'a' * 43,
        signed_token(secret='wrong'),
        signed_token(purpose='transactional'),
        signed_token() + '=',
        signed_token().replace('.', '=.'),
        '<script>坏</script>',
        'x' * 2049,
    ],
)
def test_invalid_tokens_are_neutral_do_not_echo_and_do_not_reach_auth_or_database(target, token):
    env, request = target
    for method in ('GET', 'POST'):
        response = request(method, token)
        assert response.status_code == 400 and response.text == NEUTRAL_ERROR_HTML
    assert env.AUTH.calls == 0
    assert not env.APP_DB.connection.execute('SELECT * FROM cf_user_email_preferences').fetchall()


@pytest.mark.parametrize('failure', ['deleted', 'auth', 'wrong_identity', 'store', 'secret', 'brand'])
def test_missing_accounts_and_dependency_errors_are_indistinguishable_from_invalid_tokens(target, failure):
    env, request = target
    token = signed_token('deleted' if failure == 'deleted' else 'owner')
    if failure == 'auth':
        env.AUTH.fail = True
    if failure == 'wrong_identity':
        env.AUTH.wrong_identity = True
    if failure == 'store':
        env.APP_DB.fail = True
    if failure == 'secret':
        env.LIFECYCLE_EMAIL_SIGNING_SECRET = None
    if failure == 'brand':
        env.BRAND_RUNTIME_JSON = '{}'
    for method in ('GET', 'POST'):
        response = request(method, token)
        assert response.status_code == 400 and response.text == NEUTRAL_ERROR_HTML
    assert not env.APP_DB.connection.execute('SELECT * FROM cf_user_email_preferences').fetchall()


def test_deletion_started_between_auth_check_and_write_cannot_recreate_preferences(target):
    env, request = target

    def fence():
        env.APP_DB.connection.execute(
            "INSERT INTO cf_account_deletion_tombstones (uid, completed_at, expires_at) VALUES ('owner', 1, 9999999999)"
        )

    env.APP_DB.before_write = fence
    response = request('POST', signed_token())
    assert response.status_code == 400 and response.text == NEUTRAL_ERROR_HTML
    assert not env.APP_DB.connection.execute('SELECT * FROM cf_user_email_preferences').fetchall()
    env.APP_DB.before_write = None
    assert request('GET', signed_token()).text == NEUTRAL_ERROR_HTML
