"""Actual public/internal HTTP, canonical acceptance and App SQL integration receipts."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import pytest
import candidate_integrations as integration
from candidate_db import CandidateTransaction
from candidate_integration_routes import PATH
from internal_auth import create_request_context
from test_candidate_routes import api, call, new_candidate
from test_candidate_create import env


@pytest.fixture(autouse=True)
def transport(env, monkeypatch):
    time = [datetime.now(timezone.utc)]
    sent = []

    async def send(message):
        sent.append(message)

    env.JOBS = SimpleNamespace(send=send)
    monkeypatch.setattr(integration, 'now', lambda: time[0])
    return sent, time


def accepted(api, transport, *, key='create-1'):
    candidate = new_candidate(api, key=key)
    response = call(api, 'POST', '/v1/candidates/' + candidate['candidate_id'] + '/accept')
    assert response.status_code == 200, response.text
    return candidate['candidate_id'], response.json()['task_id'], transport[0][-1]['payload']['lease_token']


def deliver(api, env, identity, action, *, uid='owner', generation=0, authority='internal', **extra):
    encoded, signature = create_request_context(
        uid,
        env.INTERNAL_ASSERTION_SECRET,
        audience='api-core',
        method='POST',
        path=PATH,
        request_id='integration-test',
        authority=authority,
    )
    return call(
        api,
        'POST',
        PATH,
        request_headers={'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature},
        json={'action': action, 'outbox_id': identity, 'account_generation': generation, **extra},
    )


def stored(env, identity):
    return json.loads(
        env.APP_DB.connection.execute(
            'SELECT record_json FROM cf_candidate_integration_outbox WHERE outbox_id=?', (identity,)
        ).fetchone()[0]
    )


def preparation(api, env, identity, token, platform='todoist'):
    response = deliver(api, env, identity, 'prepare', lease_token=token, platform=platform)
    assert response.status_code == 200, response.text
    return response.json()


def completion(api, env, identity, token, *, platform='todoist', success=True):
    return deliver(
        api,
        env,
        identity,
        'settle',
        lease_token=token,
        platform=platform,
        succeeded=success,
        external_id='external-task' if success and platform != 'apple_reminders' else None,
    )


def test_accept_claim_prepare_export_and_duplicate_deliveries_use_one_receipt(api, env, transport):
    identity, task_id, token = accepted(api, transport)
    assert stored(env, identity)['attempt_count'] == 1
    assert call(api, 'POST', '/v1/candidates/integrations/drain').json() == {'scheduled': 0}
    prepared = preparation(api, env, identity, token)
    assert prepared['task']['id'] == task_id and prepared['task']['exported'] is False
    # Two concurrent/replayed Queue deliveries with the same lease cannot issue two provider calls.
    assert preparation(api, env, identity, token) == {'status': 'obsolete'}
    result = completion(api, env, identity, token)
    assert result.json() == {'status': 'completed'}, result.text
    task = call(api, 'GET', '/v1/action-items/' + task_id).json()
    assert task['exported'] and task['export_platform'] == 'todoist' and task['export_date']
    before = stored(env, identity)
    assert completion(api, env, identity, token).json() == {'status': 'obsolete'}
    assert call(api, 'POST', '/v1/candidates/' + identity + '/accept').status_code == 200
    assert stored(env, identity) == before and len(transport[0]) == 1


def test_failure_preserves_original_five_attempt_policy_and_due_backoff(api, env, transport):
    identity, _, token = accepted(api, transport)
    for attempt in range(1, 6):
        assert preparation(api, env, identity, token)['status'] == 'ready'
        response = completion(api, env, identity, token, success=False)
        assert response.status_code == 200, response.text
        value = stored(env, identity)
        assert value['attempt_count'] == attempt
        assert call(api, 'POST', '/v1/candidates/integrations/drain').json() == {'scheduled': 0}
        if attempt < 5:
            assert value['status'] == 'failed'
            expected = transport[1][0] + timedelta(seconds=30 * 2 ** (attempt - 1))
            assert datetime.fromisoformat(value['available_at']) == expected
            transport[1][0] = expected
            assert call(api, 'POST', '/v1/candidates/integrations/drain').json() == {'scheduled': 1}
            token = transport[0][-1]['payload']['lease_token']
        else:
            assert value['status'] == 'dead_letter' and value['dead_letter_reason'] == 'integration_failed'


def test_lost_queue_hint_keeps_acceptance_and_can_be_reclaimed_after_lease(api, env, transport):
    real_send = env.JOBS.send

    async def fail(message):
        raise RuntimeError('controlled transport failure')

    env.JOBS.send = fail
    candidate = new_candidate(api)
    identity = candidate['candidate_id']
    assert call(api, 'POST', '/v1/candidates/' + identity + '/accept').status_code == 200
    first = stored(env, identity)
    assert first['status'] == 'processing' and not transport[0]
    env.JOBS.send = real_send
    transport[1][0] += timedelta(seconds=300)
    assert call(api, 'POST', '/v1/candidates/integrations/drain').json() == {'scheduled': 1}
    assert preparation(api, env, identity, first['lease_token']) == {'status': 'obsolete'}
    assert completion(api, env, identity, first['lease_token']).json() == {'status': 'obsolete'}
    assert stored(env, identity)['attempt_count'] == 2


def test_apple_push_marks_pending_but_only_device_confirmation_marks_exported(api, env, transport):
    identity, task_id, token = accepted(api, transport)
    prepared = preparation(api, env, identity, token, 'apple_reminders')
    data = prepared['push']['data']
    assert data['type'] == 'apple_reminders_sync' and data['action_item_id'] == task_id
    assert json.loads(data['items']) == [{'id': task_id, 'description': data['description'], 'due_at': data['due_at']}]
    pending = call(api, 'GET', '/v1/action-items/pending-sync').json()
    assert [item['id'] for item in pending['pending_export']] == [task_id]
    assert completion(api, env, identity, token, platform='apple_reminders').json() == {'status': 'completed'}
    assert not call(api, 'GET', '/v1/action-items/' + task_id).json()['exported']
    assert call(api, 'GET', '/v1/action-items/pending-sync', uid='other').json()['pending_export'] == []
    confirmed = call(
        api,
        'PATCH',
        '/v1/action-items/sync-batch',
        json={
            'items': [
                {
                    'id': task_id,
                    'exported': True,
                    'export_platform': 'apple_reminders',
                    'apple_reminder_id': 'device-reminder',
                }
            ]
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    result = call(api, 'GET', '/v1/action-items/pending-sync').json()
    assert result['pending_export'] == [] and result['synced_items'][0]['apple_reminder_id'] == 'device-reminder'


def test_completion_atomicity_and_task_change_during_export(api, env, transport):
    identity, task_id, token = accepted(api, transport)
    preparation(api, env, identity, token)
    env.APP_DB.connection.execute(
        "CREATE TRIGGER fail_export BEFORE UPDATE ON cf_action_items WHEN NEW.exported=1 BEGIN SELECT RAISE(ABORT,'controlled export failure'); END"
    )
    assert completion(api, env, identity, token).status_code == 503
    assert stored(env, identity)['status'] == 'processing'
    assert not call(api, 'GET', '/v1/action-items/' + task_id).json()['exported']
    env.APP_DB.connection.execute('DROP TRIGGER fail_export')
    env.APP_DB.before_write = lambda: env.APP_DB.connection.execute(
        'UPDATE cf_action_items SET description=? WHERE id=?', ('Edited while provider returned', task_id)
    )
    assert completion(api, env, identity, token).json() == {'status': 'completed'}
    task = call(api, 'GET', '/v1/action-items/' + task_id).json()
    assert task['description'] == 'Edited while provider returned' and task['exported']


@pytest.mark.parametrize('fence', ['generation', 'deletion'])
def test_generation_and_account_erasure_prevent_stale_export(api, env, transport, fence):
    identity, task_id, token = accepted(api, transport)
    preparation(api, env, identity, token)
    if fence == 'generation':
        env.APP_DB.connection.execute(
            "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
        )
    else:
        env.APP_DB.connection.execute(
            "INSERT INTO cf_account_deletion_tombstones(uid,completed_at,expires_at) VALUES ('owner',1,9999999999)"
        )
    assert completion(api, env, identity, token).status_code == 409
    assert (
        env.APP_DB.connection.execute('SELECT exported FROM cf_action_items WHERE id=?', (task_id,)).fetchone()[0] == 0
    )


def test_internal_authority_generation_shape_and_owner_are_enforced(api, env, transport):
    identity, _, token = accepted(api, transport)
    extra = {'lease_token': token, 'platform': 'todoist'}
    assert deliver(api, env, identity, 'prepare', authority='firebase', **extra).status_code == 401
    assert deliver(api, env, identity, 'prepare', generation=True, **extra).status_code == 400
    assert deliver(api, env, identity, 'prepare', uid='other', **extra).json() == {'status': 'obsolete'}
    assert call(api, 'POST', '/v1/candidates/integrations/drain', uid=None).status_code == 401
    assert call(api, 'POST', '/v1/candidates/integrations/drain?limit=501').status_code == 422
    assert call(api, 'POST', '/v1/candidates/integrations/drain', generation=1).status_code == 409


def test_malformed_ready_row_is_parked_without_blocking_the_next_identity(api, env, transport):
    identity, _, _ = accepted(api, transport)

    async def corrupt():
        tx = CandidateTransaction(env, 'owner', 0)
        value = json.loads(await tx.record('integrations', identity))
        tx.put(
            'integrations',
            identity,
            dict(value, status='processing', task_id=None, available_at='invalid', claimed_at='invalid'),
        )
        await tx.commit()

    asyncio.run(corrupt())
    assert call(api, 'POST', '/v1/candidates/integrations/drain').json() == {'scheduled': 0}
    assert stored(env, identity)['dead_letter_reason'] == 'malformed'
    assert len(transport[0]) == 1


def test_internal_assertion_is_bound_to_the_encoded_path(api, env, transport):
    identity, _, _ = accepted(api, transport)
    encoded, signature = create_request_context(
        'owner',
        env.INTERNAL_ASSERTION_SECRET,
        audience='api-core',
        method='POST',
        path=PATH,
        request_id='path-test',
        authority='internal',
    )
    result = call(
        api,
        'POST',
        PATH.replace('integrations', '%69ntegrations'),
        request_headers={'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature},
        json={'action': 'schedule', 'outbox_id': identity, 'account_generation': 0},
    )
    assert result.status_code == 401
