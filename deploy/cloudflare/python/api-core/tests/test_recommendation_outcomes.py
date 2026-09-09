"""Original attribution relationships through public HTTP and the full D1 schema."""

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from test_candidate_create import env
from test_candidate_routes import api, call, intervention, new_candidate
from test_recommendation_routes import due_candidate, model, rows
from recommendation_kernel import _stable_id

PATH = '/v1/task-intelligence/outcomes'


def outcome(api, chain, subject, *, kind='task', code='task_completed', key='outcome-1', **kwargs):
    body = dict(attribution_chain_id=chain, subject_kind=kind, subject_id=subject, outcome_code=code)
    return call(api, 'POST', PATH, key=key, json=body, **kwargs)


def accepted(api, *, workstream=False):
    candidate = new_candidate(api, workstream=workstream)
    response = call(api, 'POST', '/v1/candidates/' + candidate['candidate_id'] + '/accept')
    assert response.status_code == 200, response.text
    return candidate, response.json()


def artifact(env, workstream, identity='artifact-1', *, uid='owner'):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_workstream_artifacts(uid,artifact_id,workstream_id,logical_key,version,kind,uri,content_hash,status,created_at) "
        "VALUES (?,?,?, ?,1,'document','https://artifact.test/report',?,'draft',1)",
        (uid, identity, workstream, identity, 'a' * 64),
    )
    return identity


def test_wmnow_feedback_accept_outcome_replay_and_owned_export(api, env, model):
    ai, clock = model
    candidate = due_candidate(api, clock)
    recommendation = call(api, 'GET', '/v1/what-matters-now').json()['recommendations'][0]
    feedback = call(
        api,
        'POST',
        '/v1/task-intelligence/feedback',
        key='feedback',
        json={
            'subject_kind': 'candidate',
            'subject_id': candidate['candidate_id'],
            'intervention_id': recommendation['intervention_id'],
            'action': 'do_now',
        },
    )
    assert feedback.status_code == 200, feedback.text
    result = call(api, 'POST', '/v1/candidates/' + candidate['candidate_id'] + '/accept').json()
    chain = feedback.json()['attribution_chain_id']
    first = outcome(api, chain, result['task_id'])
    assert first.status_code == 200, first.text
    second = outcome(api, chain, result['task_id'], key='second-event')
    assert second.status_code == 200, second.text
    assert first.json()['outcome_id'] != second.json()['outcome_id']
    assert outcome(api, chain, result['task_id']).json() == first.json()
    data = call(api, 'GET', '/v1/users/export').json()['task_data']['task_outcomes']
    assert {row['outcome_id'] for row in data} == {first.json()['outcome_id'], second.json()['outcome_id']}
    assert all(set(row) == set(first.json()) for row in data)
    assert call(api, 'GET', '/v1/users/export', uid='other').json()['task_data']['task_outcomes'] == []
    assert len(rows(env, 'cf_task_outcomes')) == 2 and len(ai.calls) == 1
    # Upstream attribution records the reported result; task mutation is a separate command.
    assert not call(api, 'GET', '/v1/action-items/' + result['task_id']).json()['completed']


def test_candidate_chain_rejects_unrelated_task_kind_code_and_changed_key(api, env):
    candidate, result = accepted(api, workstream=True)
    shown, _ = intervention(api, candidate['candidate_id'])
    chain = shown['attribution_chain_id']
    other = new_candidate(api, key='unrelated')
    other_task = call(api, 'POST', '/v1/candidates/' + other['candidate_id'] + '/accept').json()['task_id']
    assert outcome(api, chain, other_task).status_code == 409
    assert outcome(api, chain, result['task_id'], code='artifact_approved').status_code == 409
    assert outcome(api, chain, result['task_id']).status_code == 200
    assert (
        outcome(api, chain, result['workstream_id'], kind='workstream', code='workstream_advanced').status_code == 409
    )
    assert len(rows(env, 'cf_task_outcomes')) == 1


@pytest.mark.parametrize('source', ['candidate', 'task', 'workstream'])
def test_related_workstream_and_artifacts_follow_original_source_relationships(api, env, source):
    candidate, result = accepted(api, workstream=True)
    source_id = {
        'candidate': candidate['candidate_id'],
        'task': result['task_id'],
        'workstream': result['workstream_id'],
    }[source]
    shown, _ = intervention(api, source_id, subject=source)
    chain = shown['attribution_chain_id']
    linked = artifact(env, result['workstream_id'])
    unrelated = artifact(env, 'unrelated-workstream', 'unrelated-artifact')
    assert (
        outcome(api, chain, result['workstream_id'], kind='workstream', code='agent_output_applied').status_code == 200
    )
    for code in ('artifact_approved', 'artifact_delivered'):
        response = outcome(api, chain, linked, kind='artifact', code=code, key=code)
        assert response.status_code == 200, response.text
    assert outcome(api, chain, unrelated, kind='artifact', code='artifact_approved', key='unrelated').status_code == 409


def test_feedback_only_chain_and_legacy_principal_need_no_control_record(api, env):
    response = call(
        api,
        'POST',
        '/v1/task-intelligence/feedback',
        key='completed',
        json={
            'subject_kind': 'decision',
            'subject_id': 'decision-1',
            'action': 'complete',
        },
    )
    assert response.status_code == 200, response.text
    chain = response.json()['attribution_chain_id']
    result = outcome(api, chain, 'decision-1', kind='decision', code='decision_resolved')
    assert result.status_code == 200, result.text
    assert not rows(env, 'cf_task_interventions') and not rows(env, 'cf_account_cutover')


def test_auth_missing_cross_owner_generation_and_original_request_shape(api, env):
    shown, _ = intervention(api, 'task-1', subject='task')
    chain = shown['attribution_chain_id']
    assert outcome(api, chain, 'task-1', uid=None).status_code == 401
    assert outcome(api, chain, 'task-1', uid='other').status_code == 404
    assert outcome(api, 'missing-chain', 'task-1').status_code == 404
    assert outcome(api, chain, 'task-1', generation=1).status_code == 409
    body = dict(
        attribution_chain_id=chain,
        subject_kind='task',
        subject_id='task-1',
        outcome_code='task_completed',
        occurred_at='2000-01-01T00:00:00Z',
    )
    assert call(api, 'POST', PATH, key='invalid-time', json=body).status_code == 422
    assert outcome(api, chain, 'task-1', key=' ').status_code == 422
    assert not rows(env, 'cf_task_outcomes')


@pytest.mark.parametrize('changed', ['artifact', 'task'])
def test_relationship_change_during_commit_rechecks_source_before_attributing(api, env, changed):
    _, result = accepted(api, workstream=True)
    shown, _ = intervention(api, result['task_id'], subject='task')
    linked = artifact(env, result['workstream_id'])

    def race():
        if changed == 'artifact':
            env.APP_DB.connection.execute(
                "UPDATE cf_workstream_artifacts SET workstream_id='unrelated' WHERE artifact_id=?", (linked,)
            )
        else:
            env.APP_DB.connection.execute(
                "UPDATE cf_action_items SET workstream_id='unrelated' WHERE id=?", (result['task_id'],)
            )

    env.APP_DB.before_write = race
    response = outcome(api, shown['attribution_chain_id'], linked, kind='artifact', code='artifact_approved')
    assert response.status_code == 409, response.text
    assert not rows(env, 'cf_task_outcomes') and not rows(env, 'cf_candidate_write_guard')


@pytest.mark.parametrize('failure,expected', [('generation', 409), ('insert', 503)])
def test_generation_change_and_late_failure_leave_no_receipt_or_guard(api, env, failure, expected):
    shown, _ = intervention(api, 'task-1', subject='task')
    if failure == 'generation':
        env.APP_DB.before_write = lambda: env.APP_DB.connection.execute(
            "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
        )
    else:
        env.APP_DB.connection.execute(
            "CREATE TRIGGER reject_outcome AFTER INSERT ON cf_task_outcomes BEGIN SELECT RAISE(ABORT,'storage unavailable'); END"
        )
    response = outcome(api, shown['attribution_chain_id'], 'task-1')
    assert response.status_code == expected, response.text
    assert not rows(env, 'cf_task_outcomes') and not rows(env, 'cf_candidate_write_guard')


def test_migration_retains_request_only_receipt_and_original_retry_identity(api, env):
    env.APP_DB.connection.close()
    db = env.APP_DB.connection = sqlite3.connect(':memory:', isolation_level=None)
    db.row_factory = sqlite3.Row
    migrations = sorted((Path(__file__).parents[3] / 'migrations/app').glob('*.sql'))
    for path in migrations:
        if path.name < '0187':
            db.executescript(path.read_text())
    chain = 'legacy-chain'
    body = dict(
        attribution_chain_id=chain, subject_kind='task', subject_id='legacy-task', outcome_code='task_completed'
    )
    identity = _stable_id('outcome', 'owner', 0, 'legacy-key')
    raw = json.dumps(body, sort_keys=True, separators=(',', ':'))
    db.execute(
        "INSERT INTO cf_task_feedback(uid,feedback_id,account_generation,attribution_chain_id,request_fingerprint,payload_json,created_at) VALUES ('owner','legacy-feedback',0,?,'legacy-feedback',?,1)",
        (chain, json.dumps(dict(subject_kind='task', subject_id='legacy-task', action='complete'))),
    )
    db.execute(
        "INSERT INTO cf_task_outcomes VALUES ('owner',?,0,?,?,?,1)",
        (identity, chain, hashlib.sha256(raw.encode()).hexdigest(), raw),
    )
    for path in migrations:
        if path.name >= '0187':
            db.executescript(path.read_text())
    response = outcome(api, chain, 'legacy-task', key='legacy-key')
    assert response.status_code == 200, response.text
    assert response.json()['outcome_id'] == identity and response.json()['occurred_at'] == '1970-01-01T00:00:01Z'
    assert rows(env, 'cf_task_outcomes')[0]['payload_json'] == raw
    assert outcome(api, chain, 'legacy-task', key='new-key').status_code == 200
    assert len(rows(env, 'cf_task_outcomes')) == 2
