"""Actual D1 SQL and registered HTTP metadata paths with request-bound identity."""

import asyncio
from dataclasses import is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import FastAPI
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

import frame_request_store
from frame_request_contract import FrameRequest, FrameRequestState, FrameRequestStateUpdate
from frame_request_policy import validate_transition
from frame_request_routes import router
from internal_auth import create_request_context
from jit_policy import JIT_ADMISSION_ALLOWLIST, JITFlagEvaluation
from test_screen_frame_views import Database, Statement

BASE = '/v1/frame-requests'


class Query(Statement):
    async def first(self):
        hook = getattr(self.db, 'interleave', None)
        if hook and hook[0](self.sql):
            self.db.interleave = None
            await hook[1]()
        return await super().first()

    async def all(self):
        return {'results': [dict(row) for row in self.execute().fetchall()]}


class D1(Database):
    def prepare(self, sql):
        return Query(self, sql)


@pytest.fixture
def target():
    db = D1()
    db.connection.execute("INSERT INTO cf_jit_flags VALUES ('',1,0,1)")
    env = SimpleNamespace(APP_DB=db, INTERNAL_ASSERTION_SECRET='frame-metadata-test-key')
    app = FastAPI()
    app.include_router(router)

    @app.middleware('http')
    async def environment(request, call_next):
        request.scope['env'] = env
        return await call_next(request)

    async def call(method, path=BASE, *, uid='owner', body=None, headers=None):
        auth = {}
        if uid:
            encoded, signature = create_request_context(
                uid,
                env.INTERNAL_ASSERTION_SECRET,
                audience='api-core',
                method=method,
                path=urlsplit(path).path,
                request_id='frame-test',
                authority='better-auth',
            )
            auth = {'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://eddy.test') as client:
            return await client.request(method, path, json=body, headers=auth if headers is None else headers)

    yield SimpleNamespace(db=db, env=env, call=lambda *a, **kw: asyncio.run(call(*a, **kw)), async_call=call)
    db.connection.close()


def create(target, **changes):
    return target.call(
        'POST', body={'device_id': 'desktop', 'dedupe_key': 'intent', 'screenshot_id': 'screen', **changes}
    )


def test_original_dataclass_policy_and_metadata_roundtrip(target, monkeypatch):
    assert is_dataclass(JITFlagEvaluation)
    stamp = datetime(2026, 9, 6, 8, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(frame_request_store, 'now', lambda: stamp)
    result = create(target, requested_ttl_seconds=100)
    assert result.status_code == 200, result.text
    row = result.json()['request']
    identity = hashlib.sha256(b'intent\0\0screen').hexdigest()
    window = int(stamp.timestamp()) // 60
    assert row['request_id'] == 'frame-' + hashlib.sha256(f'{identity}:0:{window}:0'.encode()).hexdigest()
    assert row['dedupe_key'] == identity and row['attempt_number'] == 0
    assert row['created_at'] == stamp.isoformat().replace('+00:00', 'Z')
    assert FrameRequest.model_validate(row).expires_at == stamp + timedelta(seconds=100)
    assert result.headers['cache-control'] == 'no-store'
    assert target.call('GET', BASE + '/status/' + row['request_id']).json()['request'] == row


def test_active_identity_spans_dedupe_window_and_terminal_allows_new_attempt(target, monkeypatch):
    stamp = datetime.now(timezone.utc)
    monkeypatch.setattr(frame_request_store, 'now', lambda: stamp)
    first = create(target).json()['request']
    monkeypatch.setattr(frame_request_store, 'now', lambda: stamp + timedelta(seconds=65))
    replay = create(target)
    assert replay.json()['deduplicated'] and replay.json()['request']['request_id'] == first['request_id']
    cancelled = target.call(
        'POST',
        f"{BASE}/{first['request_id']}/state",
        body={'device_id': 'desktop', 'state': 'cancelled', 'terminal_reason': 'user_cancelled'},
    )
    assert cancelled.status_code == 200
    new = create(target).json()
    assert not new['deduplicated'] and new['request']['attempt_number'] == 1
    assert new['request']['request_id'] != first['request_id']


def test_pending_claim_device_isolation_and_pixel_state_is_not_fabricated(target):
    row = create(target).json()['request']
    assert len(target.call('GET', BASE + '/pending?device_id=desktop').json()['requests']) == 1
    assert not target.call('GET', BASE + '/pending?device_id=another').json()['requests']
    path = f"{BASE}/{row['request_id']}/state"
    assert target.call('POST', path, body={'device_id': 'another', 'state': 'claimed'}).status_code == 403
    claimed = target.call('POST', path, body={'device_id': 'desktop', 'state': 'claimed'})
    assert claimed.status_code == 200 and claimed.json()['request']['claimed_at']
    assert not target.call('GET', BASE + '/pending?device_id=desktop').json()['requests']
    assert (
        target.call(
            'POST', path, body={'device_id': 'desktop', 'state': 'uploaded', 'storage_id': 'opaque'}
        ).status_code
        == 503
    )
    assert target.call('GET', BASE + '/status/' + row['request_id']).json()['request']['state'] == 'claimed'
    assert target.call('GET', BASE + '/status/' + row['request_id'], uid='other').status_code == 404


def test_quota_and_conversation_exclusion_are_atomic_sql_invariants(target):
    for i in range(8):
        assert create(target, dedupe_key=f'item-{i}').status_code == 200
    result = create(target, dedupe_key='ninth')
    assert result.status_code == 400 and 'pending_count' in result.text
    assert create(target, device_id='another', conversation_id='meeting').status_code == 200
    other = create(target, device_id='third', conversation_id='meeting', dedupe_key='second')
    assert other.status_code == 400 and 'active frame request' in other.text


def test_expired_requests_do_not_starve_bounded_delivery(target, monkeypatch):
    stamp = datetime.now(timezone.utc)
    monkeypatch.setattr(frame_request_store, 'now', lambda: stamp)
    old = create(target, requested_ttl_seconds=1).json()['request']
    monkeypatch.setattr(frame_request_store, 'now', lambda: stamp + timedelta(seconds=2))
    assert target.call('GET', BASE + '/pending?device_id=desktop').json()['requests'] == []
    status = target.call('GET', BASE + '/status/' + old['request_id']).json()['request']
    assert status['state'] == 'expired' and status['terminal_reason']
    assert create(target).status_code == 200


@pytest.mark.parametrize('fault', ['kill', 'generation', 'deletion'])
def test_authority_change_between_admission_and_insert_stores_no_request(target, fault):
    original = target.db.prepare
    triggered = False

    def prepare(sql):
        nonlocal triggered
        if sql.startswith('INSERT INTO cf_frame_requests') and not triggered:
            triggered = True
            if fault == 'kill':
                target.db.connection.execute("UPDATE cf_jit_flags SET kill_switch=1 WHERE uid=''")
            if fault == 'generation':
                target.db.connection.execute(
                    "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
                )
            if fault == 'deletion':
                target.db.connection.execute(
                    "INSERT INTO cf_account_deletion_tombstones(uid,completed_at,expires_at) VALUES ('owner',1,9999999999)"
                )
        return original(sql)

    target.db.prepare = prepare
    assert create(target).status_code == 404
    assert target.db.connection.execute('SELECT count(*) FROM cf_frame_requests').fetchone()[0] == 0


def test_missing_flag_legacy_generation_and_global_kill_contract(target):
    target.db.connection.execute('DELETE FROM cf_jit_flags')
    decision = target.call('GET', '/v1/jit/rollout-decision').json()
    assert decision['effective'] == 'disabled' and decision['reason'] == 'flag_absent'
    assert create(target).status_code == 404
    allowlisted = next(iter(JIT_ADMISSION_ALLOWLIST))
    assert target.call('GET', '/v1/jit/rollout-decision', uid=allowlisted).json()['effective'] == 'enabled'
    target.db.connection.execute("INSERT INTO cf_jit_flags VALUES ('',1,1,1)")
    target.db.connection.execute('INSERT INTO cf_jit_flags VALUES (?,1,0,1)', (allowlisted,))
    assert target.call('GET', '/v1/jit/rollout-decision', uid=allowlisted).json()['effective'] == 'disabled'
    target.db.connection.execute("UPDATE cf_jit_flags SET kill_switch=0 WHERE uid=''")
    # No cutover document: the upstream legacy principal retains generation 0.
    assert create(target).status_code == 200
    assert create(target, account_generation=1).status_code == 404


def test_provider_failure_cannot_admit_even_allowlisted_generation(target, capsys):
    def broken(sql):
        raise RuntimeError('private-provider-detail')

    target.db.prepare = broken
    uid = next(iter(JIT_ADMISSION_ALLOWLIST))
    assert target.call('GET', '/v1/jit/rollout-decision', uid=uid).json()['effective'] == 'enabled'
    assert target.call('POST', body={'device_id': 'desktop', 'dedupe_key': 'intent'}, uid=uid).status_code == 404
    output = capsys.readouterr().out
    assert 'private-provider-detail' not in output and uid not in output
    events = [json.loads(line) for line in output.splitlines()]
    assert len(events) == 2 and all(event['outcome'] == 'exhausted' for event in events)


def test_bad_auth_bad_input_and_original_transition_guard(target):
    assert target.call('POST', body={}, uid=None).status_code == 401
    assert target.call('POST', body={'device_id': 'desktop', 'dedupe_key': 'a', 'extra': 1}).status_code == 422
    assert create(target, device_id=' ').status_code == 400
    row = FrameRequest.model_validate(create(target).json()['request'])
    with pytest.raises(ValueError):
        validate_transition(
            row,
            next_state=FrameRequestState.attached,
            uid='owner',
            device_id='desktop',
            account_generation=0,
            now=datetime.now(timezone.utc),
        )


def test_conversation_delete_erases_its_metadata(target):
    target.db.connection.execute("INSERT INTO cf_conversations(uid,id,created_at) VALUES ('owner','meeting',1)")
    row = create(target, conversation_id='meeting').json()['request']
    target.db.connection.execute("DELETE FROM cf_conversations WHERE uid='owner' AND id='meeting'")
    assert target.call('GET', BASE + '/status/' + row['request_id']).status_code == 404


@pytest.mark.parametrize('competing_intent', ['intent', 'last-slot'])
def test_overlapping_admission_replays_winner_or_obeys_last_device_slot(target, competing_intent):
    for i in range(7):
        assert create(target, dedupe_key=f'item-{i}').status_code == 200
    winners = []

    async def compete():
        winners.append(
            await target.async_call(
                'POST', body={'device_id': 'desktop', 'dedupe_key': competing_intent, 'screenshot_id': 'screen'}
            )
        )

    # Both real HTTP calls pass admission before either insert. The other
    # request commits at the D1 statement seam, then the suspended call resumes.
    target.db.interleave = (lambda sql: sql.startswith('INSERT INTO cf_frame_requests'), compete)
    result = create(target)
    assert winners[0].status_code == 200
    if competing_intent == 'intent':
        assert result.status_code == 200 and result.json()['deduplicated']
        assert result.json()['request']['request_id'] == winners[0].json()['request']['request_id']
    else:
        assert result.status_code == 400 and 'pending_count' in result.text
    assert target.db.connection.execute('SELECT count(*) FROM cf_frame_requests').fetchone()[0] == 8


def test_claim_compare_and_swap_does_not_overwrite_concurrent_terminal_state(target):
    row = create(target).json()['request']
    path = f"{BASE}/{row['request_id']}/state"

    async def cancel():
        result = await target.async_call(
            'POST', path, body={'device_id': 'desktop', 'state': 'cancelled', 'terminal_reason': 'user_cancelled'}
        )
        assert result.status_code == 200

    target.db.interleave = (lambda sql: sql.startswith('UPDATE cf_frame_requests SET state = ?'), cancel)
    assert target.call('POST', path, body={'device_id': 'desktop', 'state': 'claimed'}).status_code == 409
    state = target.call('GET', BASE + '/status/' + row['request_id']).json()['request']
    assert state['state'] == 'cancelled' and state['claimed_at'] is None


@pytest.mark.parametrize('boundary', ['status', 'dedupe', 'claim'])
def test_revoked_authority_is_checked_in_the_read_or_mutation_statement(target, boundary):
    row = create(target).json()['request']

    async def revoke():
        target.db.connection.execute("UPDATE cf_jit_flags SET kill_switch=1 WHERE uid=''")

    prefixes = {
        'status': 'SELECT * FROM cf_frame_requests WHERE uid = ? AND request_id',
        'dedupe': 'SELECT * FROM cf_frame_requests WHERE uid = ? AND device_id',
        'claim': 'UPDATE cf_frame_requests SET state = ?',
    }
    target.db.interleave = (lambda sql: sql.startswith(prefixes[boundary]), revoke)
    if boundary == 'status':
        response = target.call('GET', BASE + '/status/' + row['request_id'])
    elif boundary == 'dedupe':
        response = create(target)
    else:
        response = target.call(
            'POST', f"{BASE}/{row['request_id']}/state", body={'device_id': 'desktop', 'state': 'claimed'}
        )
    assert response.status_code in {404, 409}
    assert target.db.connection.execute('SELECT state FROM cf_frame_requests').fetchone()[0] == 'requested'


def test_user_export_contains_only_owned_frame_metadata(target):
    from user_export_routes import _EXPORT_QUERIES, _rows

    own = create(target).json()['request']
    assert target.call('POST', uid='other', body={'device_id': 'desktop', 'dedupe_key': 'other'}).status_code == 200
    _, table, order = next(item for item in _EXPORT_QUERIES if item[0] == 'frame_requests')
    rows = asyncio.run(_rows(target.env, table, order, 'owner'))
    assert len(rows) == 1 and rows[0]['request_id'] == own['request_id'] and 'uid' not in rows[0]
