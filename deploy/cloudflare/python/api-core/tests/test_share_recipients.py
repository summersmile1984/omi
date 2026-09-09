"""Real recipient HTTP/SQL behavior with only the Auth service controlled."""

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from internal_auth import create_request_context, verify_request_context
from share_recipient_routes import router
from test_screen_frame_views import Database

SECRET = 'share-recipient-test-secret'
PATH = '/v1/conversations/meeting/share-recipients'
OWNER = {'name': 'Owner', 'email': 'owner@example.invalid'}
GUEST = {'name': '嘉宾', 'email': 'guest@example.invalid'}


class Auth:
    def __init__(self):
        self.profile = {'uid': 'owner', **OWNER}
        self.status, self.calls, self.callback = 200, [], None

    async def fetch(self, url, *, method, headers):
        assert url == 'https://auth.internal/internal/profile'
        claim = verify_request_context(
            headers['x-omi-auth-context'],
            headers['x-omi-internal-signature'],
            SECRET,
            audience='auth',
            method=method,
            path='/internal/profile',
        )
        assert claim and claim['uid'] == 'owner'
        self.calls.append(claim)
        if self.callback:
            self.callback()

        async def value():
            return self.profile

        return SimpleNamespace(status=self.status, json=value)


def fixture(participants=None, source='system_calendar', event=None):
    db = Database()
    external = {
        'calendar_meeting_context': {
            'calendar_source': source,
            'participants': participants if participants is not None else [OWNER, GUEST],
        }
    }
    db.connection.execute(
        'INSERT INTO cf_conversations (uid,id,created_at,updated_at,external_data_json,calendar_event_json) '
        'VALUES (?, ?, 1, 1, ?, ?)',
        ('owner', 'meeting', json.dumps(external), json.dumps(event)),
    )
    db.connection.commit()
    env = SimpleNamespace(APP_DB=db, AUTH=Auth(), INTERNAL_ASSERTION_SECRET=SECRET)
    return db, env


def call(env, *, uid='owner', signed_path=PATH, signed_method='GET', signed=True):
    app = FastAPI()
    app.include_router(router)

    @app.middleware('http')
    async def bindings(request, handler):
        request.scope['env'] = env
        return await handler(request)

    async def run():
        headers = {'x-omi-uid': 'owner', 'x-owner-email': 'attacker@example.invalid'}
        if signed:
            encoded, signature = create_request_context(
                uid,
                SECRET,
                audience='api-core',
                method=signed_method,
                path=signed_path,
                request_id='test',
                authority='better-auth',
            )
            headers.update({'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://core.test') as client:
            return await client.get(PATH, headers=headers)

    response = asyncio.run(run())
    assert response.headers['cache-control'] == 'private, no-store'
    return response


@pytest.mark.parametrize(
    'source', ['system_calendar', 'macos_calendar', 'google', 'google_calendar', 'outlook_calendar']
)
def test_calendar_sources_exclude_owner_and_deduplicate_without_migration_state(source):
    db, env = fixture(
        [
            OWNER,
            GUEST,
            {**GUEST, 'email': ' GUEST@example.invalid '},
            {'name': 'unknown', 'email': 'unknown@example.invalid'},
        ],
        source,
    )
    assert db.connection.execute('SELECT count(*) FROM cf_account_cutover').fetchone()[0] == 0
    response = call(env)
    assert response.status_code == 200, response.text
    assert response.json() == {'recipients': [GUEST]}
    assert len(env.AUTH.calls) == 1


@pytest.mark.parametrize('source', ['screen', 'screen_capture', '', None])
def test_inferred_identity_never_proposes_recipients(source):
    _, env = fixture(source=source)
    assert call(env).json() == {'recipients': []}


def test_calendar_event_pairs_names_and_emails_and_ignores_address_as_name():
    _, env = fixture(
        [],
        event={
            'attendees': ['Owner', '嘉宾', 'bare@example.invalid'],
            'attendee_emails': [OWNER['email'], GUEST['email'], 'bare@example.invalid'],
        },
    )
    assert call(env).json() == {'recipients': [GUEST]}


def test_proposal_size_cap_and_large_meeting_suppression():
    people = [{'name': f'Guest Person {i}', 'email': f'g{i}@example.invalid'} for i in range(10)]
    _, env = fixture([OWNER, *people[:9]])
    assert call(env).json() == {'recipients': people[:5]}
    _, env = fixture([OWNER, *people])
    assert call(env).json() == {'recipients': []}


@pytest.mark.parametrize('email', [None, '', 'invalid'])
def test_missing_owner_address_suppresses_suggestions_with_private_telemetry(email, capsys):
    _, env = fixture()
    env.AUTH.profile['email'] = email
    assert call(env).json() == {'recipients': []}
    event = json.loads(capsys.readouterr().out)
    assert event['from'] == 'share_recipients_proposed'
    assert event['to'] == 'share_recipients_suppressed'
    assert event['reason'] == 'auth'
    assert GUEST['email'] not in json.dumps(event)


@pytest.mark.parametrize(
    'kwargs',
    [
        {'signed': False},
        {'signed_path': '/v1/conversations/other/share-recipients'},
        {'signed_method': 'POST'},
        {'uid': 'other'},
    ],
)
def test_identity_and_request_binding(kwargs):
    _, env = fixture()
    assert call(env, **kwargs).status_code == (404 if kwargs.get('uid') == 'other' else 401)
    assert env.AUTH.calls == []


@pytest.mark.parametrize(
    'mutation,status',
    [
        ('UPDATE cf_conversations SET is_locked=1', 402),
        ('DELETE FROM cf_conversations', 404),
        ("UPDATE cf_conversations SET external_data_json='{}'", 409),
        ("INSERT INTO cf_account_deletion_tombstones (uid,completed_at,expires_at) VALUES ('owner',1,9999999999)", 404),
    ],
)
def test_auth_await_cannot_return_revoked_or_changed_calendar_data(mutation, status):
    db, env = fixture()
    env.AUTH.callback = lambda: db.connection.execute(mutation)
    response = call(env)
    assert response.status_code == status, response.text
    assert GUEST['email'] not in response.text


@pytest.mark.parametrize(
    'status,profile,expected', [(503, {}, 503), (410, {}, 401), (200, {'uid': 'other', **OWNER}, 503)]
)
def test_owner_authority_failure_is_not_an_empty_success(status, profile, expected):
    _, env = fixture()
    env.AUTH.status, env.AUTH.profile = status, profile
    response = call(env)
    assert response.status_code == expected
    assert GUEST['email'] not in response.text
