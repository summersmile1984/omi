"""Original Candidate acceptance across D1 tasks, workstreams and outboxes."""

import asyncio
from datetime import datetime, timezone
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from candidate_accept import accept_candidate
from candidate_create import get_candidate
from candidate_kernel_models import CandidateCreate, CandidateStatus
from candidate_kernel_policy import CandidateConflictError, CandidateGenerationMismatchError, CandidateNotFoundError
from test_candidate_create import create, env, proposal, rows
from test_candidate_resolve import resolve

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def accept(env, candidate, *, generation=0, uid='owner', links=None):
    return asyncio.run(
        accept_candidate(env, uid, candidate.candidate_id, generation=generation, expected_task_links=links, now=NOW)
    )


def linked_proposal(*, workstream=False, goal_id=None, workstream_id=None):
    payload = proposal(workstream=workstream).model_dump(mode='json')
    payload.update(goal_id=goal_id, workstream_id=workstream_id)
    return CandidateCreate.model_validate(payload)


def mutation(task_id, change, *, action='update', goal_id=None, workstream_id=None):
    payload = proposal(evidence='mutation-source').model_dump(mode='json')
    payload.update(
        proposed_action=action, task_id=task_id, task_change=change, goal_id=goal_id, workstream_id=workstream_id
    )
    return CandidateCreate.model_validate(payload)


def seed_goal(env, *, uid='owner', key='goal-1', status='focused', generation=0):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_goals(uid,id,title,desired_outcome,status,source,created_at,updated_at,account_generation) "
        "VALUES (?,?,'Goal','Reach it',?,'user',1,1,?)",
        (uid, key, status, generation),
    )


def seed_workstream(env, *, goal_id='goal-1', generation=0):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_workstreams(uid,id,goal_id,title,objective,status,created_at,updated_at,account_generation) "
        "VALUES ('owner','workstream-1',?,'Work','Finish it','open',1,1,?)",
        (goal_id, generation),
    )


def effects(env):
    tables = (
        'cf_candidates',
        'cf_action_items',
        'cf_workstreams',
        'cf_workstream_events',
        'cf_candidate_integration_outbox',
        'cf_vector_projection_outbox',
        'cf_candidate_write_guard',
    )
    return {table: [dict(row) for row in env.APP_DB.connection.execute('SELECT * FROM ' + table)] for table in tables}


@pytest.mark.parametrize('workstream', [False, True])
def test_accept_creates_one_canonical_result_and_durable_side_effects(env, workstream):
    candidate = create(env, proposal(workstream=workstream))
    receipt = accept(env, candidate)
    assert receipt.newly_resolved and receipt.status == CandidateStatus.accepted
    after = effects(env)
    again = accept(env, candidate)
    assert again == receipt.model_copy(update={'newly_resolved': False})
    assert effects(env) == after
    assert len(after['cf_action_items']) == len(after['cf_candidate_integration_outbox']) == 1
    task = after['cf_action_items'][0]
    assert task['id'] == receipt.task_id and task['description'] == 'Complete the report'
    assert task['completed'] == 0 and task['owner'] == 'unknown'
    assert json.loads(task['provenance_json'])[0]['id'] == 'source-1'
    outbox = json.loads(after['cf_candidate_integration_outbox'][0]['record_json'])
    assert outbox['candidate_id'] == candidate.candidate_id and outbox['task_id'] == receipt.task_id
    assert outbox['status'] == 'pending' and outbox['attempt_count'] == 0
    stored = asyncio.run(get_candidate(env, 'owner', candidate.candidate_id))
    assert stored.status == CandidateStatus.accepted and stored.result_task_id == receipt.task_id
    assert after['cf_candidate_write_guard'] == []
    if workstream:
        assert len(after['cf_workstreams']) == len(after['cf_workstream_events']) == 1
        assert task['workstream_id'] == receipt.workstream_id
        event = after['cf_workstream_events'][0]
        assert event['sequence'] == 1 and event['summary'] == 'Work initiated from an accepted suggestion'
        assert json.loads(event['evidence_refs_json'])[0]['id'] == 'source-1'
        assert {row['source_kind'] for row in after['cf_vector_projection_outbox']} == {'action_item', 'workstream'}
    else:
        assert not after['cf_workstreams'] and not after['cf_workstream_events']
        assert task['candidate_id'] == candidate.candidate_id and task['capture_confidence'] == 0.85
        assert stored.expires_at is None
    with pytest.raises(CandidateConflictError, match='already accepted'):
        resolve(env, candidate)
    assert effects(env) == after


@pytest.mark.parametrize('workstream', [False, True])
def test_late_integration_write_failure_rolls_back_entire_acceptance(env, workstream):
    candidate = create(env, proposal(workstream=workstream))
    before = effects(env)
    env.APP_DB.connection.execute(
        "CREATE TRIGGER fail_integration BEFORE INSERT ON cf_candidate_integration_outbox "
        "BEGIN SELECT RAISE(ABORT,'integration storage unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match='integration storage unavailable'):
        accept(env, candidate)
    assert effects(env) == before


@pytest.mark.parametrize(
    'action,change,status,completed',
    [
        ('update', {'due_at': None, 'priority': 'high'}, 'active', 0),
        ('complete', {'status': 'completed'}, 'completed', 1),
        ('cancel', {'status': 'cancelled'}, 'cancelled', 0),
        ('supersede', {'status': 'superseded', 'superseded_by': 'next-task'}, 'superseded', 0),
    ],
)
def test_accept_mutation_uses_original_policy_and_monotonic_physical_revision(env, action, change, status, completed):
    original = create(env)
    original_receipt = accept(env, original)
    env.APP_DB.connection.execute(
        'UPDATE cf_action_items SET due_at=? WHERE id=?', (1900000000, original_receipt.task_id)
    )
    candidate = create(env, mutation(original_receipt.task_id, change, action=action), key='mutation')
    receipt = accept(env, candidate)
    current = effects(env)
    task = current['cf_action_items'][0]
    assert receipt.task_id == original_receipt.task_id
    assert task['status'] == status and task['completed'] == completed
    assert task['updated_at'] == int(NOW.timestamp()) + 1
    assert [ref['id'] for ref in json.loads(task['provenance_json'])] == ['source-1', 'mutation-source']
    assert len(current['cf_candidate_integration_outbox']) == 1
    assert current['cf_vector_projection_outbox'][0]['desired_version'] == task['updated_at']
    if action == 'update':
        assert task['due_at'] is None and task['priority'] == 'high'
    elif action == 'complete':
        assert task['completed_at'] == task['updated_at']
    else:
        assert task['completed_at'] is None
    assert not accept(env, candidate).newly_resolved
    assert effects(env) == current


@pytest.mark.parametrize(
    'bad', ['missing', 'other-owner', 'ended', 'generation', 'workstream-goal', 'workstream-generation']
)
def test_accept_rejects_invalid_goal_workstream_relationships(env, bad):
    if bad != 'missing':
        seed_goal(
            env,
            uid='other' if bad == 'other-owner' else 'owner',
            status='achieved' if bad == 'ended' else 'focused',
            generation=0,
        )
    if bad == 'generation':
        env.APP_DB.connection.execute("UPDATE cf_goals SET account_generation=1 WHERE uid='owner'")
    workstream_id = None
    if bad.startswith('workstream'):
        workstream_id = 'workstream-1'
        seed_workstream(
            env,
            goal_id=None if bad == 'workstream-goal' else 'goal-1',
            generation=1 if bad == 'workstream-generation' else 0,
        )
    candidate = create(env, linked_proposal(goal_id='goal-1', workstream_id=workstream_id))
    before = effects(env)
    with pytest.raises(CandidateConflictError):
        accept(env, candidate)
    assert effects(env) == before


def test_existing_task_can_finish_under_ended_goal_but_new_link_cannot_be_added(env):
    seed_goal(env)
    original = create(env, linked_proposal(goal_id='goal-1'))
    task = accept(env, original).task_id
    env.APP_DB.connection.execute("UPDATE cf_goals SET status='achieved' WHERE uid='owner'")
    candidate = create(env, mutation(task, {'status': 'completed'}, action='complete'), key='complete')
    accept(env, candidate)
    assert effects(env)['cf_action_items'][0]['status'] == 'completed'
    another = create(env, proposal(description='Different task'), key='different')
    task2 = accept(env, another).task_id
    relink = create(env, mutation(task2, {'priority': 'high'}, goal_id='goal-1'), key='relink')
    with pytest.raises(CandidateConflictError, match='ended goal'):
        accept(env, relink)


def test_relationship_changed_between_read_and_commit_is_rechecked(env):
    seed_goal(env)
    seed_workstream(env)
    candidate = create(env, linked_proposal(goal_id='goal-1', workstream_id='workstream-1'))

    def race():
        env.APP_DB.connection.execute("UPDATE cf_workstreams SET goal_id=NULL WHERE uid='owner'")

    env.APP_DB.before_write = race
    with pytest.raises(CandidateConflictError, match='must match'):
        accept(env, candidate)
    stored = asyncio.run(get_candidate(env, 'owner', candidate.candidate_id))
    assert stored.status == CandidateStatus.pending
    assert effects(env)['cf_action_items'] == [] and effects(env)['cf_candidate_write_guard'] == []


def test_concurrent_goal_ending_prevents_new_workstream_acceptance(env):
    seed_goal(env)
    candidate = create(env, linked_proposal(workstream=True, goal_id='goal-1'))
    env.APP_DB.before_write = lambda: env.APP_DB.connection.execute(
        "UPDATE cf_goals SET status='achieved' WHERE uid='owner'"
    )
    with pytest.raises(CandidateConflictError, match='ended goal'):
        accept(env, candidate)
    assert not effects(env)['cf_workstreams'] and not effects(env)['cf_action_items']


def test_account_isolation_generation_and_terminal_state_guard_acceptance(env):
    candidate = create(env)
    before = effects(env)
    with pytest.raises(CandidateNotFoundError):
        accept(env, candidate, uid='other')
    with pytest.raises(CandidateGenerationMismatchError):
        accept(env, candidate, generation=1)
    assert effects(env) == before
    resolve(env, candidate)
    with pytest.raises(CandidateConflictError, match='already rejected'):
        accept(env, candidate)


def test_unmigrated_task_is_adopted_into_current_account_generation(env):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_action_items(uid,id,description,status,created_at,updated_at) VALUES ('owner','legacy-task','Old task','active',1,1)"
    )
    env.APP_DB.connection.execute(
        "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',2,1)"
    )
    candidate = create(env, mutation('legacy-task', {'priority': 'high'}), generation=2)
    accept(env, candidate, generation=2)
    assert effects(env)['cf_action_items'][0]['account_generation'] == 2


def test_user_export_includes_owned_candidate_records_and_omits_internal_outboxes(env):
    from candidate_create import create_candidate
    from user_export_routes import export_user_data
    from test_user_export_routes import FakeRequest, signed_headers

    candidate = create(env)
    accept(env, candidate)
    asyncio.run(create_candidate(env, 'other', proposal(), idempotency_key='other-request', generation=0))
    env.INTERNAL_ASSERTION_SECRET = 'candidate-export-test-secret'
    env.BRAND_RUNTIME_JSON = json.dumps({'brand_id': 'eddy', 'display_name': 'Eddy', 'ai_persona_name': 'Eddy'})
    response = asyncio.run(
        export_user_data(FakeRequest(env, signed_headers(env.INTERNAL_ASSERTION_SECRET, uid='owner')))
    )
    assert response.status_code == 200
    payload = json.loads(response.body)
    records = payload['task_data']['candidates']
    assert len(records) == 1 and records[0]['candidate_id'] == candidate.candidate_id
    assert records[0]['status'] == 'accepted' and records[0]['task_change']['description'] == 'Complete the report'
    assert 'uid' not in records[0] and 'record_json' not in records[0]
    assert 'candidate_integration_outbox' not in payload and 'candidate_integration_outbox' not in payload['task_data']


@pytest.mark.parametrize('canonical', [False, True])
def test_native_goal_created_in_current_generation_can_receive_candidate_work(env, canonical):
    from goal_routes import create_goal, create_canonical_goal
    from test_goal_routes import FakeRequest, mutation_headers, signed_headers

    env.INTERNAL_ASSERTION_SECRET = 'candidate-goal-test-secret'
    env.APP_DB.connection.execute(
        "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',2,1)"
    )
    headers = (
        mutation_headers(env.INTERNAL_ASSERTION_SECRET, 'goal-create', generation=2, uid='owner')
        if canonical
        else signed_headers(env.INTERNAL_ASSERTION_SECRET, uid='owner')
    )
    request = FakeRequest(env, headers, {'title': 'New goal', 'desired_outcome': 'Finish this work'})
    result = asyncio.run((create_canonical_goal if canonical else create_goal)(request))
    assert isinstance(result, dict) and result['id'].startswith('goal_')
    goal = env.APP_DB.connection.execute(
        'SELECT account_generation FROM cf_goals WHERE id=?', (result['id'],)
    ).fetchone()
    assert goal[0] == 2
    candidate = create(env, linked_proposal(workstream=True, goal_id=result['id']), generation=2)
    receipt = accept(env, candidate, generation=2)
    assert receipt.newly_resolved and receipt.workstream_id


def test_canonical_goal_rejects_stale_generation_and_late_generation_change(env):
    from goal_routes import create_canonical_goal
    from test_goal_routes import FakeRequest, mutation_headers

    env.INTERNAL_ASSERTION_SECRET = 'candidate-goal-test-secret'
    body = {'title': 'Goal', 'desired_outcome': 'Finish work'}
    stale = FakeRequest(env, mutation_headers(env.INTERNAL_ASSERTION_SECRET, 'stale', generation=1, uid='owner'), body)
    assert asyncio.run(create_canonical_goal(stale)).status_code == 409
    request = FakeRequest(
        env, mutation_headers(env.INTERNAL_ASSERTION_SECRET, 'racing', generation=0, uid='owner'), body
    )
    env.APP_DB.before_write = lambda: env.APP_DB.connection.execute(
        "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
    )
    assert asyncio.run(create_canonical_goal(request)).status_code == 409
    assert env.APP_DB.connection.execute('SELECT count(*) FROM cf_goals').fetchone()[0] == 0
    assert env.APP_DB.connection.execute('SELECT count(*) FROM cf_goal_mutations').fetchone()[0] == 0
