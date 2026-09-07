"""Actual HTTP and App SQL for universal capability and generation-bound writes."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import pytest
from test_candidate_routes import call
from test_candidate_entry import api
from test_candidate_create import env, proposal


@pytest.mark.parametrize('generation', [0, 7])
def test_control_is_universal_for_unmigrated_and_current_accounts_without_writes(api, env, generation):
    db = env.APP_DB.connection
    if generation:
        db.execute(
            'INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES (?,?,1)', ('owner', generation)
        )
    before = db.total_changes
    response = call(api, 'GET', '/v1/candidates/control')
    assert response.status_code == 200, response.text
    assert response.json() == {'workflow_mode': 'read', 'account_generation': generation, 'chat_first_ui': True}
    assert db.total_changes == before
    assert call(api, 'GET', '/v1/candidates/control', uid='other').json() == {
        'workflow_mode': 'read',
        'account_generation': 0,
        'chat_first_ui': True,
    }


def test_sampled_generation_drives_candidate_create_accept_and_invalidates_old_requests(api, env):
    control = call(api, 'GET', '/v1/candidates/control').json()
    assert control['chat_first_ui'] and control['workflow_mode'] == 'read'
    created = call(
        api,
        'POST',
        '/v1/candidates',
        generation=control['account_generation'],
        key='before-cutover',
        json=proposal().model_dump(mode='json'),
    )
    assert created.status_code == 200, created.text
    identity = created.json()['candidate_id']
    env.APP_DB.connection.execute(
        "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
    )
    assert (
        call(
            api, 'POST', '/v1/candidates/' + identity + '/accept', generation=control['account_generation']
        ).status_code
        == 409
    )
    fresh = call(api, 'GET', '/v1/candidates/control').json()
    assert fresh == {'workflow_mode': 'read', 'account_generation': 1, 'chat_first_ui': True}
    assert call(api, 'POST', '/v1/candidates/' + identity + '/accept', generation=1).status_code == 409
    created = call(
        api, 'POST', '/v1/candidates', generation=1, key='after-cutover', json=proposal().model_dump(mode='json')
    )
    assert created.status_code == 200, created.text
    accepted = call(
        api,
        'POST',
        '/v1/candidates/' + created.json()['candidate_id'] + '/accept',
        generation=fresh['account_generation'],
    )
    assert accepted.status_code == 200, accepted.text
    task = call(api, 'GET', '/v1/action-items/' + accepted.json()['task_id']).json()
    assert task['description'] == 'Complete the report'


@pytest.mark.parametrize('fence', ['intent', 'tombstone', 'database'])
def test_unavailable_or_erasing_accounts_fail_closed_without_leaking_errors(api, env, capsys, fence):
    db = env.APP_DB.connection
    if fence == 'intent':
        db.execute(
            "INSERT INTO cf_account_deletion_intents(uid,job_id,status,phase,next_attempt_at,created_at,updated_at) "
            "VALUES ('owner','deletion-job','pending','quiescing',1,1,1)"
        )
    elif fence == 'tombstone':
        db.execute(
            "INSERT INTO cf_account_deletion_tombstones(uid,completed_at,expires_at) VALUES ('owner',1,9999999999)"
        )
    else:
        env.APP_DB.fail = True
    response = call(api, 'GET', '/v1/candidates/control')
    assert response.status_code == 200
    assert response.json() == {'workflow_mode': 'off', 'account_generation': 0, 'chat_first_ui': False}
    emitted = capsys.readouterr().out
    assert 'task_workflow_control' in emitted and 'legacy_shell' in emitted and 'owner' not in emitted
    assert 'private storage detail' not in response.text + emitted


def test_client_headers_cannot_override_control_generation_or_capability(api, env):
    response = call(
        api,
        'GET',
        '/v1/candidates/control',
        generation=99,
        request_headers={'x-workflow-mode': 'off', 'x-chat-first-ui': 'false'},
    )
    assert response.json() == {'workflow_mode': 'read', 'account_generation': 0, 'chat_first_ui': True}
    assert call(api, 'GET', '/v1/candidates/control', uid=None).status_code == 401
