"""Real HTTP handlers for the original Candidate and cross-surface feedback flow."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import httpx
from fastapi import FastAPI
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from candidate_routes import router
from candidate_control_routes import router as control_router
from task_intelligence_routes import router as intelligence_router
from action_item_routes import router as task_router, batch_router as task_batch_router
from workstream_routes import router as workstream_router
from recommendation_routes import router as recommendation_router
from recurrence_routes import router as recurrence_router
from candidate_integration_routes import router as integration_router
from staged_candidate_routes import router as staged_router
from user_export_routes import router as export_router
from candidate_read import list_candidates
from candidate_attention import record_feedback
from candidate_kernel_recommendation import FeedbackCreate
from candidate_kernel_suggested import candidate_recommendation_dedupe_key
from test_candidate_create import create, env, proposal
from test_candidate_control_routes import signed_headers


@pytest.fixture
def api(env):
    env.INTERNAL_ASSERTION_SECRET = 'candidate-api-test-secret'
    app = FastAPI()
    for value in (
        control_router,
        router,
        intelligence_router,
        task_batch_router,
        task_router,
        workstream_router,
        export_router,
        staged_router,
        recommendation_router,
        recurrence_router,
        integration_router,
    ):
        app.include_router(value)

    @app.middleware('http')
    async def runtime(request, call_next):
        request.scope['env'] = env
        return await call_next(request)

    async def send(method, path, *, uid='owner', generation=0, key=None, request_headers=None, **kwargs):
        headers = signed_headers(env.INTERNAL_ASSERTION_SECRET, uid) if uid is not None else {}
        headers['x-account-generation'] = str(generation)
        headers.update(request_headers or {})
        if key is not None:
            headers['idempotency-key'] = key
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='https://candidate.test'
        ) as client:
            return await client.request(method, path, headers=headers, **kwargs)

    return send


def call(api, method, path, **kwargs):
    return asyncio.run(api(method, path, **kwargs))


def new_candidate(api, *, key='create-1', workstream=False, value=None):
    response = call(
        api, 'POST', '/v1/candidates', key=key, json=value or proposal(workstream=workstream).model_dump(mode='json')
    )
    assert response.status_code == 200, response.text
    return response.json()


def suggested(api):
    response = call(api, 'GET', '/v1/candidates?surface=suggested')
    assert response.status_code == 200, response.text
    return response.json()['candidates']


def intervention(api, candidate_id, *, key='intervention-1', subject='candidate', surface='what_matters_now'):
    body = {
        'surface': surface,
        'subject_kind': subject,
        'subject_id': candidate_id,
        'dedupe_key': candidate_recommendation_dedupe_key(candidate_id),
        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    response = call(api, 'POST', '/v1/task-intelligence/interventions', key=key, json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


@pytest.mark.parametrize('workstream', [False, True])
def test_public_create_accept_replay_and_owned_task_or_workstream_reads(api, workstream):
    candidate = new_candidate(api, workstream=workstream)
    assert new_candidate(api, workstream=workstream) == candidate
    assert call(api, 'GET', '/v1/candidates/' + candidate['candidate_id']).json() == candidate
    receipt = call(api, 'POST', '/v1/candidates/' + candidate['candidate_id'] + '/accept')
    assert receipt.status_code == 200, receipt.text
    result = receipt.json()
    assert result['newly_resolved'] and result['status'] == 'accepted'
    repeated = call(api, 'POST', '/v1/candidates/' + candidate['candidate_id'] + '/accept').json()
    assert repeated == {**result, 'newly_resolved': False}
    task = call(api, 'GET', '/v1/action-items/' + result['task_id'])
    assert task.status_code == 200, task.text
    assert task.json()['description'] == 'Complete the report'
    assert not task.json()['completed']
    if workstream:
        stream = call(api, 'GET', '/v1/workstreams/' + result['workstream_id'])
        assert stream.status_code == 200, stream.text
        # Upstream models.workstream.WorkstreamDetailProjection owns this envelope.
        assert stream.json()['workstream']['objective'] == 'Keep the report current'
        assert stream.json()['tasks'][0]['id'] == result['task_id']
    assert call(api, 'GET', '/v1/candidates/' + candidate['candidate_id'], uid='other').status_code == 404
    assert suggested(api) == []


def test_public_auth_generation_and_payload_validation(api):
    assert call(api, 'GET', '/v1/candidates', uid=None).status_code == 401
    assert (
        call(api, 'POST', '/v1/candidates', uid=None, key='k', json=proposal().model_dump(mode='json')).status_code
        == 401
    )
    assert (
        call(api, 'POST', '/v1/candidates', generation=1, key='k', json=proposal().model_dump(mode='json')).status_code
        == 409
    )
    assert call(api, 'POST', '/v1/candidates', key='k', json={'unknown': 'field'}).status_code == 422
    assert call(api, 'GET', '/v1/candidates?limit=501').status_code == 422
    assert call(api, 'GET', '/v1/candidates?offset=-1').status_code == 422


@pytest.mark.parametrize('decision', ['reject', 'expire'])
def test_public_terminal_decision_replays_and_disappears_from_suggested(api, decision):
    candidate = new_candidate(api)
    assert len(suggested(api)) == 1
    path = '/v1/candidates/' + candidate['candidate_id'] + '/' + decision
    first = call(api, 'POST', path, json={'reason': 'owner_choice'})
    assert first.status_code == 200, first.text
    repeated = call(api, 'POST', path, json={'reason': 'another_reason'})
    assert repeated.json() == {**first.json(), 'newly_resolved': False}
    assert suggested(api) == []
    assert call(api, 'POST', '/v1/candidates/' + candidate['candidate_id'] + '/accept').status_code == 409


def test_subsecond_pagination_and_original_suggested_limits(api, env):
    base = datetime.now(timezone.utc).replace(microsecond=0)
    expected = []
    for index in range(8):
        record = create(
            env, proposal(description=f'Action {index}'), key=f'item-{index}', now=base + timedelta(microseconds=index)
        )
        expected.insert(0, record.candidate_id)
    first = call(api, 'GET', '/v1/candidates?limit=3').json()
    second = call(api, 'GET', '/v1/candidates?limit=3&offset=3').json()
    assert [row['candidate_id'] for row in first['candidates']] == expected[:3] and first['has_more']
    assert [row['candidate_id'] for row in second['candidates']] == expected[3:6]
    assert [row['candidate_id'] for row in suggested(api)] == expected[:5]
    create(env, proposal(description='Old action'), key='expired', now=base - timedelta(days=3))
    create(env, proposal(description='Low confidence', confidence=0.79), key='low-confidence')
    assert [row['candidate_id'] for row in suggested(api)] == expected[:5]


def test_later_feedback_suppresses_equivalent_candidates_across_surfaces_and_replay_keeps_expiry(api, env):
    first = new_candidate(api, workstream=True)
    second = new_candidate(api, workstream=True, key='same-workstream-new-occurrence')
    assert first['candidate_id'] != second['candidate_id'] and len(suggested(api)) == 1
    shown, _ = intervention(api, first['candidate_id'])
    body = {
        'subject_kind': 'candidate',
        'subject_id': first['candidate_id'],
        'intervention_id': shown['intervention_id'],
        'action': 'later',
    }
    feedback = call(api, 'POST', '/v1/task-intelligence/feedback', key='later-1', json=body)
    assert feedback.status_code == 200, feedback.text
    before = [tuple(row) for row in env.APP_DB.connection.execute('SELECT * FROM cf_task_attention_overrides')]
    assert suggested(api) == []
    replay = call(api, 'POST', '/v1/task-intelligence/feedback', key='later-1', json=body)
    assert replay.json() == feedback.json()
    assert [tuple(row) for row in env.APP_DB.connection.execute('SELECT * FROM cf_task_attention_overrides')] == before
    original_time = datetime.fromisoformat(feedback.json()['created_at'].replace('Z', '+00:00'))
    asynchronous_replay = asyncio.run(
        record_feedback(
            env,
            'owner',
            FeedbackCreate.model_validate(body),
            idempotency_key='later-1',
            generation=0,
            now=original_time + timedelta(hours=1),
        )
    )
    assert asynchronous_replay.created_at == original_time
    after_expiry, _ = asyncio.run(
        list_candidates(env, 'owner', surface='suggested', now=original_time + timedelta(hours=25))
    )
    assert len(after_expiry) == 1


def test_identical_intervention_bodies_with_distinct_keys_keep_distinct_occurrences(api):
    candidate = new_candidate(api)
    original, body = intervention(api, candidate['candidate_id'])
    replay = call(api, 'POST', '/v1/task-intelligence/interventions', key='intervention-1', json=body)
    another = call(api, 'POST', '/v1/task-intelligence/interventions', key='intervention-2', json=body)
    assert replay.json() == original and another.status_code == 200, another.text
    assert another.json()['intervention_id'] != original['intervention_id']
    conflict = call(
        api,
        'POST',
        '/v1/task-intelligence/interventions',
        key='intervention-1',
        json={**body, 'subject_id': 'different'},
    )
    assert conflict.status_code == 409


def test_feedback_rejects_wrong_subject_and_rolls_back_when_override_write_fails(api, env):
    candidate = new_candidate(api)
    shown, _ = intervention(api, candidate['candidate_id'])
    body = {
        'subject_kind': 'candidate',
        'subject_id': 'wrong',
        'intervention_id': shown['intervention_id'],
        'action': 'dismiss',
    }
    assert call(api, 'POST', '/v1/task-intelligence/feedback', key='wrong', json=body).status_code == 409
    body['subject_id'] = candidate['candidate_id']
    assert call(api, 'POST', '/v1/task-intelligence/feedback', key='other', uid='other', json=body).status_code == 404
    env.APP_DB.connection.execute(
        "CREATE TRIGGER attention_failure BEFORE INSERT ON cf_task_attention_overrides BEGIN SELECT RAISE(ABORT,'attention unavailable'); END"
    )
    failed = call(api, 'POST', '/v1/task-intelligence/feedback', key='rollback', json=body)
    assert failed.status_code == 503, failed.text
    assert env.APP_DB.connection.execute('SELECT count(*) FROM cf_task_feedback').fetchone()[0] == 0
    assert env.APP_DB.connection.execute('SELECT count(*) FROM cf_candidate_write_guard').fetchone()[0] == 0
    assert len(suggested(api)) == 1


def test_not_mine_feedback_rejects_candidate_and_already_handled_task_proposes_completion(api):
    candidate = new_candidate(api)
    shown, _ = intervention(api, candidate['candidate_id'])
    feedback = call(
        api,
        'POST',
        '/v1/task-intelligence/feedback',
        key='not-mine',
        json={
            'subject_kind': 'candidate',
            'subject_id': candidate['candidate_id'],
            'intervention_id': shown['intervention_id'],
            'action': 'dismiss',
            'reason': 'not_mine',
        },
    )
    assert feedback.status_code == 200, feedback.text
    assert call(api, 'GET', '/v1/candidates/' + candidate['candidate_id']).json()['status'] == 'rejected'
    another = new_candidate(
        api, key='accepted-task', value=proposal(description='A different task').model_dump(mode='json')
    )
    task = call(api, 'POST', '/v1/candidates/' + another['candidate_id'] + '/accept').json()['task_id']
    shown, _ = intervention(api, task, key='task-shown', subject='task')
    handled = call(
        api,
        'POST',
        '/v1/task-intelligence/feedback',
        key='handled',
        json={
            'subject_kind': 'task',
            'subject_id': task,
            'intervention_id': shown['intervention_id'],
            'action': 'dismiss',
            'reason': 'already_handled',
        },
    )
    assert handled.status_code == 200, handled.text
    proposed = handled.json()['proposed_completion_candidate_id']
    assert call(api, 'GET', '/v1/candidates/' + proposed).json()['proposed_action'] == 'complete'
    assert not call(api, 'GET', '/v1/action-items/' + task).json()['completed']
    assert call(api, 'POST', '/v1/candidates/' + proposed + '/accept').status_code == 200
    assert call(api, 'GET', '/v1/action-items/' + task).json()['completed']


def test_inventory_is_read_only_and_includes_owned_historical_generation(api, env):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_task_candidates(uid,candidate_id,account_generation,status,description,request_fingerprint,created_at,updated_at) VALUES ('owner','historical-row',3,'pending','Old suggestion','old-fingerprint',1,1)"
    )
    response = call(api, 'POST', '/v1/candidates/migrate-staged', json={'limit': 100})
    assert response.status_code == 200, response.text
    assert response.json()['scanned'] == 1 and response.json()['created'] == 0 and response.json()['dry_run']
    assert env.APP_DB.connection.execute('SELECT count(*) FROM cf_candidates').fetchone()[0] == 0


def test_owner_export_includes_current_and_historical_feedback_without_internal_metadata(api, env):
    candidate = new_candidate(api)
    shown, body = intervention(api, candidate['candidate_id'])
    feedback = call(
        api,
        'POST',
        '/v1/task-intelligence/feedback',
        key='export-feedback',
        json={
            'subject_kind': 'candidate',
            'subject_id': candidate['candidate_id'],
            'intervention_id': shown['intervention_id'],
            'action': 'later',
        },
    )
    assert feedback.status_code == 200, feedback.text
    other = call(api, 'POST', '/v1/task-intelligence/interventions', uid='other', key='other', json=body)
    assert other.status_code == 200, other.text
    hidden_feedback = call(
        api,
        'POST',
        '/v1/task-intelligence/feedback',
        uid='other',
        key='other',
        json={
            'subject_kind': 'candidate',
            'subject_id': candidate['candidate_id'],
            'intervention_id': other.json()['intervention_id'],
            'action': 'dismiss',
        },
    )
    assert hidden_feedback.status_code == 200, hidden_feedback.text
    # Historical physical rows store request-only JSON, not a canonical record.
    env.APP_DB.connection.execute(
        "INSERT INTO cf_task_feedback(uid,feedback_id,account_generation,request_fingerprint,payload_json,created_at) VALUES ('owner','old-feedback',0,'old-request',?,1)",
        (json.dumps({'subject_kind': 'task', 'subject_id': 'old-task', 'action': 'opened'}),),
    )
    response = call(api, 'GET', '/v1/users/export')
    assert response.status_code == 200, response.text
    data = response.json()['task_data']
    assert len(data['task_interventions']) == len(data['task_attention_overrides']) == 1
    assert data['task_interventions'][0]['intervention_id'] == shown['intervention_id']
    assert data['task_feedback'][0]['feedback_id'] == feedback.json()['feedback_id']
    assert data['task_feedback'][0]['created_at'] == feedback.json()['created_at']
    assert data['task_feedback'][1]['subject_id'] == 'old-task'
    assert data['task_feedback'][1]['feedback_id'] == 'old-feedback'
    assert len(data['task_feedback']) == 2
    for rows in (data['task_interventions'], data['task_feedback'], data['task_attention_overrides']):
        for row in rows:
            assert (
                not {
                    'uid',
                    'payload_json',
                    'record_json',
                    'request_fingerprint',
                    '_request_hash',
                    '_override_expires_at',
                }
                & row.keys()
            )


def test_attention_expiry_retains_subsecond_boundary(api, env):
    candidate = new_candidate(api)
    shown, _ = intervention(api, candidate['candidate_id'])
    current = datetime.now(timezone.utc).replace(microsecond=100000)
    expires = current + timedelta(seconds=2, microseconds=400000)
    asyncio.run(
        record_feedback(
            env,
            'owner',
            FeedbackCreate.model_validate(
                {
                    'subject_kind': 'candidate',
                    'subject_id': candidate['candidate_id'],
                    'intervention_id': shown['intervention_id'],
                    'action': 'later',
                    'later_until': expires,
                }
            ),
            idempotency_key='precise-expiry',
            generation=0,
            now=current,
        )
    )
    before, _ = asyncio.run(list_candidates(env, 'owner', surface='suggested', now=expires - timedelta(microseconds=1)))
    after, _ = asyncio.run(list_candidates(env, 'owner', surface='suggested', now=expires))
    assert before == [] and len(after) == 1
