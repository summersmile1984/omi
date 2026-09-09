"""Released staged actions use original Candidate policy through real HTTP/SQL.

Wire shape, ascending score order, 0.5 capture/ownership, and read-only history
come from backend/routers/staged_tasks.py and staged_migration.py, not CF mocks.
"""

import json

from test_candidate_create import env
from test_candidate_routes import api, call, new_candidate


def staged(api, **body):
    response = call(api, 'POST', '/v1/staged-tasks', json=body or {'description': 'Prepare the launch'})
    assert response.status_code == 200, response.text
    return response.json()


def listing(api, **kwargs):
    response = call(api, 'GET', '/v1/staged-tasks', **kwargs)
    assert response.status_code == 200, response.text
    return response.json()['items']


def rows(env, table):
    return [dict(row) for row in env.APP_DB.connection.execute('SELECT * FROM ' + table)]


def history(env, identity, *, uid='owner', score=None):
    env.APP_DB.connection.execute(
        "INSERT INTO cf_task_candidates(uid,candidate_id,account_generation,status,description,request_fingerprint,relevance_score,created_at,updated_at) VALUES (?,?,7,'pending',?,?,?,?,?)",
        (uid, identity, 'Historical ' + identity, identity, score, 1, 1),
    )


def test_new_staged_create_retry_uses_canonical_owner_without_cutover_entitlement(api, env):
    value = dict(description='Prepare the launch', metadata='Launch context', category='work', relevance_score=900)
    first = staged(api, **value)
    assert staged(api, **value) == first
    assert rows(env, 'cf_task_candidates') == []
    canonical = call(api, 'GET', '/v1/candidates/' + first['id']).json()
    assert canonical['task_change']['description'] == first['description']
    assert canonical['task_change']['owner'] == 'unknown'
    assert canonical['source_surface'] == first['source'] == 'legacy_staged'
    assert canonical['capture_confidence'] == canonical['ownership_confidence'] == 0.5
    assert canonical['compatibility'] == {'metadata': 'Launch context', 'category': 'work', 'relevance_score': 900}
    assert canonical['account_generation'] == 0
    assert listing(api) == [first] and listing(api, uid='other') == []
    assert call(api, 'POST', '/v1/staged-tasks', uid=None, json=value).status_code == 401
    assert call(api, 'POST', '/v1/staged-tasks', json={'description': 'x' * 5001}).status_code == 422
    assert call(api, 'GET', '/v1/staged-tasks?limit=1001').status_code == 422
    assert call(api, 'GET', '/v1/staged-tasks?offset=-1').status_code == 422


def test_read_merges_without_materializing_and_score_changes_only_explicit_history(api, env):
    history(env, 'high-score', score=900)
    history(env, 'low-score', score=10)
    history(env, 'other-only', uid='other', score=0)
    snapshot = rows(env, 'cf_task_candidates')
    assert [item['id'] for item in listing(api)] == ['low-score', 'high-score']
    assert rows(env, 'cf_candidates') == [] and rows(env, 'cf_task_candidates') == snapshot
    response = call(
        api, 'PATCH', '/v1/staged-tasks/batch-scores', json={'scores': [{'id': 'high-score', 'relevance_score': 1}]}
    )
    assert response.status_code == 200, response.text
    projected = listing(api)
    assert len(projected) == 2 and projected[0]['id'] != 'high-score'
    assert projected[0]['description'] == 'Historical high-score' and projected[0]['relevance_score'] == 1
    assert len(rows(env, 'cf_candidates')) == 1 and rows(env, 'cf_task_candidates') == snapshot
    generic = new_candidate(api)
    assert len(listing(api)) == 2
    assert call(api, 'POST', '/v1/staged-tasks/' + generic['candidate_id'] + '/promote').status_code == 404


def test_promote_uses_canonical_task_and_atomic_sync_outboxes(api, env):
    first = staged(api)
    env.APP_DB.connection.execute(
        "CREATE TRIGGER reject_task BEFORE INSERT ON cf_action_items BEGIN SELECT RAISE(ABORT,'task write unavailable'); END"
    )
    path = '/v1/staged-tasks/' + first['id'] + '/promote'
    failed = call(api, 'POST', path)
    assert failed.status_code == 503, failed.text
    assert rows(env, 'cf_action_items') == rows(env, 'cf_candidate_integration_outbox') == []
    assert call(api, 'GET', '/v1/candidates/' + first['id']).json()['status'] == 'pending'
    env.APP_DB.connection.execute('DROP TRIGGER reject_task')
    promoted = call(api, 'POST', '/v1/staged-tasks/promote')
    assert promoted.status_code == 200, promoted.text
    result = promoted.json()
    assert result['promoted'] and result['promoted_task']['description'] == first['description']
    assert result['promoted_task']['owner'] == 'unknown'
    repeat = call(api, 'POST', path)
    assert repeat.status_code == 200 and repeat.json() == result
    assert len(rows(env, 'cf_action_items')) == len(rows(env, 'cf_candidate_integration_outbox')) == 1
    assert len(rows(env, 'cf_vector_projection_outbox')) == 1
    assert listing(api) == []
    assert call(api, 'POST', '/v1/staged-tasks/promote').json()['promoted'] is False
    assert call(api, 'POST', path, uid='other').status_code == 404


def test_historical_cleanup_failure_does_not_duplicate_task_or_redisplay_suggestion(api, env):
    history(env, 'original')
    env.APP_DB.connection.execute(
        "CREATE TRIGGER delay_cleanup BEFORE DELETE ON cf_task_candidates BEGIN SELECT RAISE(ABORT,'cleanup unavailable'); END"
    )
    path = '/v1/staged-tasks/original/promote'
    first = call(api, 'POST', path)
    assert first.status_code == 503, first.text
    assert len(rows(env, 'cf_action_items')) == len(rows(env, 'cf_task_candidates')) == 1
    accepted = json.loads(rows(env, 'cf_candidates')[0]['record_json'])
    assert accepted['status'] == 'accepted' and listing(api) == []
    env.APP_DB.connection.execute('DROP TRIGGER delay_cleanup')
    replay = call(api, 'POST', path)
    assert replay.status_code == 200, replay.text
    assert replay.json()['promoted_task']['id'] == accepted['result_task_id']
    assert len(rows(env, 'cf_action_items')) == 1 and rows(env, 'cf_task_candidates') == []
    assert json.loads(rows(env, 'cf_candidates')[0]['record_json']) == accepted


def test_delete_and_clear_keep_canonical_terminal_decisions_and_ignore_terminal_scores(api, env):
    first = staged(api)
    assert call(api, 'DELETE', '/v1/staged-tasks/' + first['id']).status_code == 200
    before = rows(env, 'cf_candidates')
    response = call(
        api,
        'PATCH',
        '/v1/staged-tasks/batch-scores',
        json={'scores': [{'id': first['id'], 'relevance_score': 10}, {'id': 'missing', 'relevance_score': 20}]},
    )
    assert response.status_code == 200 and rows(env, 'cf_candidates') == before
    second = staged(api, description='Another task')
    history(env, 'historical')
    history(env, 'other', uid='other')
    generic = new_candidate(api)
    result = call(api, 'DELETE', '/v1/staged-tasks')
    assert result.status_code == 200 and result.json()['deleted_count'] == 2, result.text
    assert listing(api) == [] and len(listing(api, uid='other')) == 1
    assert call(api, 'GET', '/v1/candidates/' + second['id']).json()['status'] == 'rejected'
    assert call(api, 'GET', '/v1/candidates/' + generic['candidate_id']).json()['status'] == 'pending'
    assert call(api, 'DELETE', '/v1/staged-tasks').json()['deleted_count'] == 0


def test_generation_race_cannot_accept_or_update_score(api, env):
    first = staged(api)

    def cutover():
        env.APP_DB.connection.execute(
            "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
        )

    env.APP_DB.before_write = cutover
    response = call(api, 'POST', '/v1/staged-tasks/' + first['id'] + '/promote')
    assert response.status_code == 409, response.text
    assert rows(env, 'cf_action_items') == []
    assert json.loads(rows(env, 'cf_candidates')[0]['record_json'])['status'] == 'pending'
    assert listing(api) == []
