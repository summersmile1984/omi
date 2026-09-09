"""Original desktop snapshot payloads through public routes and actual migration SQL."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest
from test_candidate_create import env, proposal
from test_candidate_routes import api, call, new_candidate
from test_recommendation_routes import model, due_candidate
from recommendation_state import RecommendationReader
from recommendation_kernel import _stable_id

HEADERS = {'x-app-platform': 'macos', 'x-device-id-hash': 'device-a'}
CONTEXT = '/v1/task-intelligence/context-snapshot'
LOOPS = '/v1/task-intelligence/open-loop-snapshot'


def context_payload(now=None, **changes):
    now = now or datetime.now(timezone.utc)
    return {
        'device_id': 'device-a',
        'snapshot_id': 'context-1',
        'matches': [],
        'generated_at': now.isoformat(),
        'expires_at': (now + timedelta(minutes=20)).isoformat(),
        **changes,
    }


def stream(api, key):
    body = proposal(workstream=True).model_dump(mode='json')
    body['workstream_proposal']['title'] = key
    candidate = new_candidate(api, key=key, value=body)
    result = call(api, 'POST', '/v1/candidates/' + candidate['candidate_id'] + '/accept')
    assert result.status_code == 200, result.text
    return result.json()['workstream_id']


def loop_payload(workstream, **changes):
    value = context_payload()
    del value['snapshot_id']
    del value['matches']
    return {
        **value,
        'owner': 'owner',
        'runtime_id': 'runtime-1',
        'workstream_id': workstream,
        'conversation_id': 'conversation-1',
        'context_packet_version': 'v1',
        'open_loop_snapshot': [],
        **changes,
    }


def put(api, path, value, *, key='snapshot-1', **kwargs):
    return call(api, 'PUT', path, key=key, json=value, request_headers=HEADERS, **kwargs)


def table(env, name):
    return [dict(row) for row in env.APP_DB.connection.execute('SELECT * FROM ' + name)]


def test_original_open_loop_payloads_coexist_per_device_runtime_and_workstream(api, env):
    a, b = stream(api, 'first-stream'), stream(api, 'second-stream')
    bodies = [loop_payload(a), loop_payload(b), loop_payload(a, runtime_id='runtime-2')]
    for i, body in enumerate(bodies):
        result = put(api, LOOPS, body, key=f'loop-{i}')
        assert result.status_code == 200, result.text
        assert result.json()['snapshot_id'] == _stable_id(
            'loop-snapshot', 0, 'macos_device-a', body['runtime_id'], body['workstream_id']
        )
        assert result.json()['replaced'] is False
        assert put(api, LOOPS, body, key=f'loop-{i}').json() == result.json()
    reader = RecommendationReader(env, 'owner', 0, 'macos_device-a')
    found = asyncio.run(
        reader.list_open_loop_snapshots(
            'owner', device_id='macos_device-a', now=datetime.now(timezone.utc), account_generation=0
        )
    )
    assert {(v.runtime_id, v.workstream_id) for v in found} == {(v['runtime_id'], v['workstream_id']) for v in bodies}
    assert len(table(env, 'cf_task_open_loop_snapshots')) == 3
    updated = {
        **bodies[0],
        'generated_at': (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        'context_packet_version': 'v2',
    }
    revised = put(api, LOOPS, updated, key='new-loop-version')
    assert revised.status_code == 200 and revised.json()['replaced'], revised.text
    assert len(table(env, 'cf_task_open_loop_snapshots')) == 3
    assert (
        asyncio.run(
            reader.list_open_loop_snapshots(
                'owner', device_id='macos_other-device', now=datetime.now(timezone.utc), account_generation=0
            )
        )
        == []
    )
    assert put(api, LOOPS, {**bodies[0], 'context_packet_version': 'v2'}, key='loop-0').status_code == 409


def test_context_receipt_replay_retains_first_result_after_newer_state_and_export_is_portable(api, env):
    now = datetime.now(timezone.utc)
    first = context_payload(now)
    response = put(api, CONTEXT, first)
    assert response.status_code == 200, response.text
    newer = context_payload(now + timedelta(seconds=1), snapshot_id='context-2')
    replacement = put(api, CONTEXT, newer, key='newer')
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()['replaced'] is True
    assert put(api, CONTEXT, first).json() == response.json()
    assert json.loads(table(env, 'cf_task_context_snapshots')[0]['payload_json'])['snapshot_id'] == 'context-2'
    assert put(api, CONTEXT, first, key='older').status_code == 409
    assert put(api, CONTEXT, {**newer, 'snapshot_id': 'different'}, key='timestamp-reuse').status_code == 409
    assert put(api, CONTEXT, {**newer, 'snapshot_id': 'different'}).status_code == 409
    denied = call(
        api, 'PUT', CONTEXT, key='another-device', json={**newer, 'device_id': 'other'}, request_headers=HEADERS
    )
    assert denied.status_code == 403
    export_response = call(api, 'GET', '/v1/users/export')
    assert export_response.status_code == 200, export_response.text
    exported = export_response.json()
    assert exported['task_data']['task_context_snapshots'][0]['snapshot_id'] == 'context-2'
    assert 'scope_key' not in json.dumps(exported) and 'receipt_id' not in json.dumps(exported.get('task_data', {}))


@pytest.mark.parametrize('variation', ['long_ttl', 'future', 'expired', 'raw_context'])
def test_original_snapshot_window_and_bounded_schema_reject_invalid_inputs(api, env, variation):
    now = datetime.now(timezone.utc)
    value = context_payload(now)
    if variation == 'long_ttl':
        value['expires_at'] = (now + timedelta(hours=2)).isoformat()
    if variation == 'future':
        value = context_payload(now + timedelta(minutes=6))
    if variation == 'expired':
        value = context_payload(now - timedelta(hours=1))
    if variation == 'raw_context':
        value['raw_context'] = 'must not enter normalized state'
    assert put(api, CONTEXT, value).status_code == 422
    assert table(env, 'cf_task_context_snapshots') == table(env, 'cf_task_snapshot_receipts') == []


def test_open_loop_requires_owned_open_workstream_at_commit(api, env):
    workstream = stream(api, 'owned')
    body = loop_payload(workstream)
    assert put(api, LOOPS, {**body, 'owner': 'other'}).status_code == 422
    assert put(api, LOOPS, {**body, 'workstream_id': 'missing'}).status_code == 422
    env.APP_DB.before_write = lambda: env.APP_DB.connection.execute(
        "UPDATE cf_workstreams SET status='completed' WHERE uid='owner' AND id=?", (workstream,)
    )
    assert put(api, LOOPS, body).status_code == 422
    assert table(env, 'cf_task_open_loop_snapshots') == table(env, 'cf_task_snapshot_receipts') == []


def test_late_receipt_failure_and_generation_change_cannot_leave_snapshot(api, env):
    value = context_payload()
    db = env.APP_DB.connection
    db.execute(
        "CREATE TRIGGER reject_snapshot_receipt BEFORE INSERT ON cf_task_snapshot_receipts BEGIN SELECT RAISE(ABORT,'controlled receipt failure'); END"
    )
    assert put(api, CONTEXT, value).status_code == 503
    assert (
        table(env, 'cf_task_context_snapshots')
        == table(env, 'cf_task_snapshot_receipts')
        == table(env, 'cf_candidate_write_guard')
        == []
    )
    db.execute('DROP TRIGGER reject_snapshot_receipt')
    env.APP_DB.before_write = lambda: db.execute(
        "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
    )
    assert put(api, CONTEXT, value).status_code == 409
    assert table(env, 'cf_task_context_snapshots') == []
    assert put(api, CONTEXT, value, generation=1).status_code == 200
    assert table(env, 'cf_task_context_snapshots')[0]['account_generation'] == 1


def test_expiry_removes_snapshot_and_receipt_and_allows_new_request(api, env):
    now = datetime.now(timezone.utc)
    value = context_payload(now)
    assert put(api, CONTEXT, value).status_code == 200
    reader = RecommendationReader(env, 'owner', 0, 'macos_device-a')
    expired = now + timedelta(minutes=21)
    assert (
        asyncio.run(reader.get_context_snapshot('owner', 'macos_device-a', now=expired, account_generation=0)) is None
    )
    assert table(env, 'cf_task_context_snapshots') == table(env, 'cf_task_snapshot_receipts') == []
    assert put(api, CONTEXT, value).json()['replaced'] is False


def test_context_changes_original_recommendation_facts_and_cache_material(api, env, model):
    ai, clock = model
    candidate = due_candidate(api, clock)
    before = call(api, 'GET', '/v1/what-matters-now', request_headers=HEADERS)
    assert before.status_code == 200, before.text
    value = context_payload(
        clock.current,
        matches=[{'subject_kind': 'candidate', 'subject_id': candidate['candidate_id'], 'signals': ['app']}],
    )
    assert put(api, CONTEXT, value).status_code == 200
    after = call(api, 'GET', '/v1/what-matters-now', request_headers=HEADERS)
    assert after.status_code == 200, after.text
    assert after.json()['material_version'] != before.json()['material_version']
    assert len(ai.calls) == 2
    subjects = json.loads(ai.calls[-1][1]['messages'][1]['content'])['subjects']
    assert subjects[0]['facts']['context_match_signals'] == ['app']


def test_forward_migration_preserves_existing_snapshot_rows_and_new_scopes(env):
    db = sqlite3.connect(':memory:', isolation_level=None)
    paths = sorted((Path(__file__).parents[3] / 'migrations/app').glob('*.sql'))
    try:
        for p in paths:
            if p.name.startswith('0186_'):
                break
            db.executescript(p.read_text())
        for kind in ['context', 'open_loop']:
            name = 'cf_task_' + kind + '_snapshots'
            db.execute(
                f'INSERT INTO {name} VALUES (?,?,?,?,?,?,?,?,?)',
                ('historical', 'macos_old', 0, 'old', 'hash', 'malformed legacy payload', 1, 2, 1),
            )
        db.executescript(next(p for p in paths if p.name.startswith('0186_')).read_text())
        for kind in ['context', 'open_loop']:
            row = db.execute('SELECT payload_json,receipt_id FROM cf_task_' + kind + '_snapshots').fetchone()
            assert row == ('malformed legacy payload', None)
    finally:
        db.close()


def test_expired_read_rechecks_snapshot_replaced_before_its_delete_transaction(api, env):
    from candidate_kernel_recommendation import NormalizedContextSnapshot
    from recommendation_snapshots import save_snapshot

    now = datetime.now(timezone.utc)
    assert put(api, CONTEXT, context_payload(now)).status_code == 200
    checked = now + timedelta(minutes=21)
    newer = NormalizedContextSnapshot.model_validate(
        context_payload(checked, snapshot_id='concurrent-new', device_id='macos_device-a')
    )
    original_batch = env.APP_DB.batch
    competing = False

    async def publish_before_delete(statements):
        nonlocal competing
        if not competing:
            competing = True
            await save_snapshot(env, 'owner', 0, newer, idempotency_key='concurrent', now=checked)
        return await original_batch(statements)

    env.APP_DB.batch = publish_before_delete
    reader = RecommendationReader(env, 'owner', 0, 'macos_device-a')
    result = asyncio.run(reader.get_context_snapshot('owner', 'macos_device-a', now=checked, account_generation=0))
    assert result == newer
    assert json.loads(table(env, 'cf_task_context_snapshots')[0]['payload_json'])['snapshot_id'] == 'concurrent-new'
    receipts = table(env, 'cf_task_snapshot_receipts')
    assert len(receipts) == 1 and json.loads(receipts[0]['record_json'])['receipt']['snapshot_id'] == 'concurrent-new'
