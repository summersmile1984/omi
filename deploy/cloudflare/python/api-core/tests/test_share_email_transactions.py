"""Exercise the private production mail API and all migrations as real SQL."""

import asyncio
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from internal_auth import create_request_context
from share_email_routes import router
from share_email_contract import DAILY_SEND_QUOTA, SHARE_EMAIL_CLAIM_TTL_SECONDS
from test_share_recipients import Auth, SECRET
from test_developer_routes import FakeDb, FakeStatement


class Statement(FakeStatement):
    def bind(self, *values):
        return Statement(self.connection, self.sql, values)

    async def first(self):
        row = self.connection.execute(self.sql, self.args).fetchone()
        self.connection.commit()
        return dict(row) if row else None


class Database(FakeDb):
    def prepare(self, sql):
        return Statement(self.connection, sql)


def fixture(visibility='private'):
    db = Database()
    db.connection.execute(
        'INSERT INTO cf_conversations(uid,id,created_at,updated_at,visibility,structured_json) VALUES '
        "('owner','meeting',1,1,?,?)",
        (visibility, json.dumps({'title': 'Planning', 'overview': '## Notes\n- **Ship** <script>x</script>'})),
    )
    if visibility != 'private':
        db.connection.execute(
            'INSERT INTO cf_shared_conversation_index VALUES (?,?,?,1)', ('meeting', 'owner', visibility)
        )
    db.connection.commit()
    env = SimpleNamespace(
        APP_DB=db,
        AUTH=Auth(),
        INTERNAL_ASSERTION_SECRET=SECRET,
        BRAND_RUNTIME_JSON=json.dumps({'brand_id': 'eddy', 'display_name': 'Eddy', 'ai_persona_name': 'Eddy'}),
        PUBLIC_SHARE_BASE_URL='https://eddy.example.invalid',
    )
    return db, env


def call(
    env, path='/internal/share-email/conversations/meeting/prepare', body=None, *, uid='owner', authority='internal'
):
    app = FastAPI()
    app.include_router(router)

    @app.middleware('http')
    async def bindings(request, handler):
        request.scope['env'] = env
        return await handler(request)

    async def run():
        signed = create_request_context(
            uid, SECRET, audience='api-core', method='POST', path=path, request_id='mail-test', authority=authority
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://core.test') as client:
            return await client.post(
                path, json=body, headers={'x-omi-auth-context': signed[0], 'x-omi-internal-signature': signed[1]}
            )

    return asyncio.run(run())


def prepare(env, recipients=('guest@example.invalid',)):
    return call(env, body={'recipient_emails': list(recipients)})


def action(env, dispatch_id, operation, body=None):
    return call(env, '/internal/share-email/dispatches/' + dispatch_id + '/' + operation, body)


def sql(db, query, args=()):
    rows = db.connection.execute(query, args).fetchall()
    db.connection.commit()
    return [dict(row) for row in rows]


def state(db):
    return (
        sql(db, 'SELECT visibility FROM cf_conversations')[0]['visibility'],
        sum(x['used'] for x in sql(db, 'SELECT used FROM cf_share_email_quota')),
        len(sql(db, 'SELECT email FROM cf_share_email_recipients')),
    )


def test_send_publication_claim_confirmation_brand_and_idempotent_repeat():
    db, env = fixture()
    response = prepare(env, [' Guest@example.invalid ', 'guest@example.invalid'])
    assert response.status_code == 200, response.text
    identifier = response.json()['dispatch_id']
    assert state(db) == ('shared', 1, 1)
    assert sql(db, 'SELECT visibility FROM cf_shared_conversation_index') == [{'visibility': 'shared'}]
    claim = action(env, identifier, 'claim')
    assert claim.status_code == 200, claim.text
    value = claim.json()
    assert value['to'] == ['guest@example.invalid']
    assert value['subject'] == 'Meeting notes: Planning'
    assert '<h2>Notes</h2>' in value['html'] and '<strong>Ship</strong>' in value['html']
    assert '<script>' not in value['html'] and '&lt;script&gt;' in value['html']
    assert 'https://eddy.example.invalid/conversations/meeting' in value['html']
    assert '>Eddy</a>' in value['html'] and 'omi.me' not in value['html']
    assert value['reply_to'] == 'owner@example.invalid'
    assert action(env, identifier, 'claim').status_code == 409
    assert action(env, identifier, 'finish', {'phase': 'sent', 'message_id': 'native-id'}).status_code == 200
    assert prepare(env).json() == {'sent_to': ['guest@example.invalid']}
    assert sql(db, 'SELECT payload_json FROM cf_share_email_dispatches') == [{'payload_json': None}]
    assert state(db) == ('shared', 1, 1)


def test_live_duplicate_is_conflict_and_overlap_dispatches_only_new_addresses():
    db, env = fixture()
    first = prepare(env).json()['dispatch_id']
    assert prepare(env).status_code == 409
    # The later share intent writes a new visibility revision. It owns keeping
    # the link live if the first prepared operation is cancelled.
    second = prepare(env, ['guest@example.invalid', 'second@example.invalid']).json()['dispatch_id']
    assert action(env, first, 'claim').status_code == 409
    assert action(env, second, 'claim').json()['to'] == ['second@example.invalid']
    assert state(db) == ('shared', 1, 1)
    assert action(env, second, 'finish', {'phase': 'sent'}).status_code == 200


@pytest.mark.parametrize('visibility', ['private', 'shared', 'public'])
def test_definitive_failure_refunds_once_and_preserves_preexisting_visibility(visibility):
    db, env = fixture(visibility)
    identifier = prepare(env).json()['dispatch_id']
    assert action(env, identifier, 'claim').status_code == 200
    assert action(env, identifier, 'finish', {'phase': 'rejected'}).status_code == 200
    assert action(env, identifier, 'finish', {'phase': 'rejected'}).status_code == 200
    assert state(db) == (visibility, 0, 0)
    assert 'dispatch_id' in prepare(env).json()


def test_ambiguous_delivery_keeps_link_quota_and_never_reclaims_after_ttl():
    db, env = fixture()
    identifier = prepare(env).json()['dispatch_id']
    assert action(env, identifier, 'claim').status_code == 200
    assert action(env, identifier, 'finish', {'phase': 'ambiguous'}).status_code == 200
    sql(db, 'UPDATE cf_share_email_dispatches SET expires_at=1')
    assert prepare(env).json() == {'sent_to': ['guest@example.invalid']}
    assert state(db) == ('shared', 1, 1)


def test_daily_quota_and_midnight_refund_stay_on_original_dispatch_day():
    db, env = fixture()
    assert DAILY_SEND_QUOTA == 30 and SHARE_EMAIL_CLAIM_TTL_SECONDS == 180
    identifier = prepare(env).json()['dispatch_id']
    assert action(env, identifier, 'claim').status_code == 200
    sql(db, 'UPDATE cf_share_email_quota SET used=30')
    assert prepare(env, ['another@example.invalid']).status_code == 429
    assert state(db) == ('shared', 30, 1)
    sql(db, "INSERT INTO cf_share_email_quota VALUES ('owner','20990101',4)")
    assert action(env, identifier, 'finish', {'phase': 'rejected'}).status_code == 200
    assert sql(db, "SELECT used FROM cf_share_email_quota WHERE day='20990101'") == [{'used': 4}]
    assert sum(x['used'] for x in sql(db, 'SELECT used FROM cf_share_email_quota')) == 33


@pytest.mark.parametrize(
    'mutation',
    [
        "UPDATE cf_conversations SET visibility='public'",
        "UPDATE cf_conversations SET visibility='shared'",
        "UPDATE cf_conversations SET starred=1",
    ],
)
def test_failed_send_never_rolls_back_another_actor_write(mutation):
    db, env = fixture()
    identifier = prepare(env).json()['dispatch_id']
    assert action(env, identifier, 'claim').status_code == 200
    sql(db, mutation)
    expected = state(db)[0]
    assert action(env, identifier, 'finish', {'phase': 'rejected'}).status_code == 200
    assert state(db) == (expected, 0, 0)


def test_prepared_restart_can_retry_but_dispatching_restart_is_ambiguous():
    db, env = fixture()
    identifier = prepare(env).json()['dispatch_id']
    sql(db, 'UPDATE cf_share_email_dispatches SET expires_at=1')
    second = prepare(env).json()['dispatch_id']
    assert second != identifier and state(db) == ('shared', 1, 1)
    assert action(env, second, 'claim').status_code == 200
    sql(db, 'UPDATE cf_share_email_dispatches SET expires_at=1')
    assert prepare(env).json() == {'sent_to': ['guest@example.invalid']}
    assert sql(db, 'SELECT phase FROM cf_share_email_dispatches WHERE id=?', (second,)) == [{'phase': 'ambiguous'}]


def test_auth_enrichment_race_rolls_back_all_reservations():
    db, env = fixture()
    env.AUTH.callback = lambda: sql(
        db, 'UPDATE cf_conversations SET structured_json=?', (json.dumps({'title': 'Changed'}),)
    )
    response = prepare(env)
    assert response.status_code == 409, response.text
    assert state(db) == ('private', 0, 0)
    assert sql(db, 'SELECT id FROM cf_share_email_dispatches') == []


def test_private_authority_and_locked_or_deleted_conversation_denials():
    db, env = fixture()
    assert call(env, body={'recipient_emails': ['guest@example.invalid']}, authority='better-auth').status_code == 401
    assert call(env, body={'recipient_emails': ['guest@example.invalid']}, uid='other').status_code == 404
    sql(db, 'UPDATE cf_conversations SET is_locked=1')
    assert prepare(env).status_code == 402
    sql(db, 'DELETE FROM cf_conversations')
    assert prepare(env).status_code == 404


@pytest.mark.parametrize(
    'body,status',
    [
        ({'recipient_emails': []}, 422),
        ({'recipient_emails': ['invalid']}, 400),
        ({'recipient_emails': ['g@example.invalid'] * 6}, 422),
    ],
)
def test_upstream_request_validation(body, status):
    db, env = fixture()
    assert call(env, body=body).status_code == status
    assert state(db) == ('private', 0, 0)


def test_conversation_delete_cascades_claims_and_account_fence_prevents_dispatch():
    db, env = fixture()
    identifier = prepare(env).json()['dispatch_id']
    sql(db, "INSERT INTO cf_account_deletion_tombstones VALUES ('owner',1,9999999999)")
    assert action(env, identifier, 'claim').status_code >= 400
    assert sql(db, 'SELECT phase FROM cf_share_email_dispatches')[0]['phase'] == 'prepared'
    sql(db, 'DELETE FROM cf_conversations')
    assert sql(db, 'SELECT * FROM cf_share_email_dispatches') == []
    assert sql(db, 'SELECT * FROM cf_share_email_recipients') == []


def test_owner_export_contains_receipts_without_internal_mail_payload_or_leases():
    from user_export_routes import export_user_data
    from test_user_export_routes import FakeRequest, signed_headers

    db, env = fixture()
    identifier = prepare(env).json()['dispatch_id']
    response = asyncio.run(export_user_data(FakeRequest(env, signed_headers(SECRET, 'owner'))))
    assert response.status_code == 200
    document = json.loads(response.body)
    assert document['share_email_dispatches'][0]['id'] == identifier
    assert document['share_email_dispatches'][0]['phase'] == 'prepared'
    assert 'payload' not in document['share_email_dispatches'][0]
    assert 'expires_at' not in document['share_email_dispatches'][0]
    assert document['share_email_recipients'][0]['email'] == 'guest@example.invalid'
    assert document['share_email_quota'][0]['used'] == 1
