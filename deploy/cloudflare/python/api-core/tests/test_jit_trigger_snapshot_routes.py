"""Original watchlist contracts through actual Core ASGI and migrated D1 SQL."""

import json

import pytest

from test_candidate_entry import api
from test_candidate_routes import call
from test_jit_proactivity_reservations import env, trigger_fixture
import jit_trigger_snapshot_routes as routes

PATH = '/v1/jit/trigger-snapshot'


def enable(env):
    env.APP_DB.connection.execute("INSERT INTO cf_jit_flags VALUES ('owner',1,0,1)")


def read(api, **kwargs):
    return call(api, 'GET', PATH, **kwargs)


def test_real_entry_disabled_and_proven_empty_are_distinct(api, env):
    disabled = read(api)
    assert disabled.status_code == 200 and disabled.headers['cache-control'] == 'no-store'
    assert disabled.json()['complete'] is False
    assert disabled.json()['failure_reason'] == 'rollout_not_enabled'
    enable(env)
    empty = read(api).json()
    assert empty['complete'] is True and empty['rows'] == [] and empty['failure_reason'] is None
    assert len(empty['snapshot_revision']) == 64
    assert read(api).json()['snapshot_revision'] == empty['snapshot_revision']
    assert read(api, uid='other').json()['complete'] is False
    assert read(api, uid=None).status_code == 401


def test_original_action_policy_revision_and_owner_binding(api, env):
    memory_id, revision = trigger_fixture(api, env)
    response = call(api, 'GET', PATH + '?uid=other&account_generation=9')
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot['owner_id'] == 'owner' and snapshot['account_generation'] == 0 and snapshot['complete']
    assert snapshot['head_commit_id'] and snapshot['commit_sequence'] > 0
    assert len(snapshot['rows']) == 1
    row = snapshot['rows'][0]
    assert row['memory_id'] == memory_id and row['item_revision'] == revision
    assert row['action'] == {'type': 'agent_prompt', 'prompt': 'Find the next release step.'}
    assert row['wakeup_budget_per_day'] == 1
    assert json.loads(row['trigger_condition_json'])['keywords'] == ['release']
    assert snapshot == read(api).json()
    env.APP_DB.connection.execute("INSERT INTO cf_jit_flags VALUES ('other',1,0,1)")
    other_memory = call(api, 'POST', '/v3/memories', uid='other', json={'content': 'Other owner context.'})
    assert other_memory.status_code == 200, other_memory.text
    other = read(api, uid='other').json()
    assert other['owner_id'] == 'other' and other['head_commit_id'] and other['complete'] and other['rows'] == []
    env.APP_DB.connection.execute("UPDATE cf_memories SET status='hidden' WHERE id=?", (memory_id,))
    retired = read(api).json()
    assert retired['complete'] and retired['rows'] == []
    assert retired['snapshot_revision'] != snapshot['snapshot_revision']


@pytest.mark.parametrize('change', ['action', 'malformed', 'generation', 'privacy'])
def test_one_invalid_open_trigger_invalidates_the_entire_watchlist(api, env, change):
    memory_id, _ = trigger_fixture(api, env)
    if change == 'action':
        metadata = json.loads(env.APP_DB.row(memory_id)['canonical_metadata_json'])
        metadata['trigger_condition']['action']['prompt'] = ' '
        env.APP_DB.connection.execute(
            'UPDATE cf_memories SET canonical_metadata_json=? WHERE id=?', (json.dumps(metadata), memory_id)
        )
    elif change == 'malformed':
        env.APP_DB.connection.execute("UPDATE cf_memories SET arguments_json='{}' WHERE id=?", (memory_id,))
    elif change == 'generation':
        env.APP_DB.connection.execute('UPDATE cf_memories SET account_generation=9 WHERE id=?', (memory_id,))
    else:
        env.APP_DB.connection.execute(
            "UPDATE cf_memories SET sensitivity_labels_json='[\"secret\"]' WHERE id=?", (memory_id,)
        )
    snapshot = read(api).json()
    assert not snapshot['complete'] and snapshot['rows'] == [] and snapshot['failure_reason'] == 'row_invalid'


def test_snoozed_trigger_remains_in_original_watchlist_with_its_expiry(api, env):
    memory_id, _ = trigger_fixture(api, env)
    until = '2200-01-01T00:00:00+00:00'
    env.APP_DB.connection.execute(
        'UPDATE cf_memories SET arguments_json=? WHERE id=?',
        (json.dumps({'wakeup_budget_per_day': 1, 'jit_trigger_feedback': {'snoozed_until': until}}), memory_id),
    )
    snapshot = read(api).json()
    assert snapshot['complete'] and snapshot['rows'][0]['snoozed_until'].startswith('2200-01-01T00:00:00')


@pytest.mark.parametrize('change', ['metadata_without_head_update', 'kill_switch', 'delete_account'])
def test_revocation_during_scan_cannot_release_actionable_content(api, env, monkeypatch, change):
    memory_id, _ = trigger_fixture(api, env)
    original = routes.TriggerSnapshotStore.triggers

    async def changed(self, limit):
        rows = await original(self, limit)
        if change == 'metadata_without_head_update':
            env.APP_DB.connection.execute("UPDATE cf_memories SET arguments_json='{}' WHERE id=?", (memory_id,))
        elif change == 'kill_switch':
            env.APP_DB.connection.execute("UPDATE cf_jit_flags SET kill_switch=1 WHERE uid='owner'")
        else:
            env.APP_DB.connection.execute(
                "INSERT INTO cf_account_deletion_tombstones(uid,completed_at,expires_at) VALUES ('owner',1,9999999999)"
            )
        return rows

    monkeypatch.setattr(routes.TriggerSnapshotStore, 'triggers', changed)
    response = read(api)
    if change == 'delete_account':
        assert response.status_code == 404
    else:
        snapshot = response.json()
        assert not snapshot['complete'] and snapshot['rows'] == []
        assert snapshot['failure_reason'] == (
            'authority_changed' if change == 'metadata_without_head_update' else 'rollout_not_enabled'
        )
    assert 'Find the next release step.' not in response.text


@pytest.mark.parametrize('shape', ['missing_control', 'malformed_control', 'query_failed'])
def test_unavailable_authority_is_never_certified_empty(api, env, monkeypatch, shape):
    trigger_fixture(api, env)
    if shape == 'missing_control':
        env.APP_DB.connection.execute('DELETE FROM cf_memory_apply_control')
    elif shape == 'malformed_control':
        env.APP_DB.connection.execute("UPDATE cf_memory_apply_control SET control_json='{}'")
    else:

        async def failed(self, limit):
            raise RuntimeError('private storage detail')

        monkeypatch.setattr(routes.TriggerSnapshotStore, 'raw_triggers', failed)
    response = read(api)
    snapshot = response.json()
    assert not snapshot['complete'] and snapshot['rows'] == []
    assert snapshot['failure_reason'] == ('query_failed' if shape == 'query_failed' else 'generation_unavailable')
    assert 'private storage detail' not in response.text


def test_original_500_row_exhaustiveness_limit_uses_real_sql(api, env):
    memory_id, _ = trigger_fixture(api, env)
    row = env.APP_DB.row(memory_id)
    columns = [r['name'] for r in env.APP_DB.connection.execute('PRAGMA table_info(cf_memories)')]
    sql = 'INSERT INTO cf_memories(' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
    env.APP_DB.connection.executemany(
        sql, [tuple({**row, 'id': 'trigger-' + str(i)}[column] for column in columns) for i in range(499)]
    )
    snapshot = read(api).json()
    assert snapshot['complete'] and len(snapshot['rows']) == 500
    env.APP_DB.connection.execute(sql, tuple({**row, 'id': 'trigger-overflow'}[column] for column in columns))
    snapshot = read(api).json()
    assert not snapshot['complete'] and snapshot['rows'] == []
    assert snapshot['failure_reason'] == 'trigger_limit_exceeded'
