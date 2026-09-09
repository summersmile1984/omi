"""Original feedback policy through production ASGI and the complete App SQL."""

from datetime import datetime, timedelta
import json
import sqlite3

import pytest

from test_candidate_entry import api
from test_candidate_routes import call
from test_jit_proactivity_reservations import env, trigger_fixture, request, identity, NOW, rows
from memory_apply_item import read_item
import jit_proactivity_store

PATH = '/v1/jit/trigger-feedback'


def setup(api, env, action='useful'):
    memory_id, revision = trigger_fixture(api, env)
    reservation = request('planned', 'planned_notification', trigger_memory_id=memory_id, trigger_revision=revision)
    response = call(api, 'POST', '/v1/jit/proactivity/reservations', json=reservation)
    assert response.status_code == 200, response.text
    value = dict(
        feedback_id=identity('feedback'),
        event_id=reservation['event_id'],
        trigger_memory_id=memory_id,
        account_generation=0,
        trigger_revision=revision,
        action=action,
        recorded_at=NOW.isoformat(),
    )
    if action == 'snooze':
        value['snoozed_until'] = (NOW + timedelta(hours=2)).isoformat()
    return value


def send(api, value, **kwargs):
    return call(api, 'POST', PATH, json=value, **kwargs)


def state(env):
    return {
        table: [dict(row) for row in env.APP_DB.connection.execute('SELECT * FROM ' + table)]
        for table in (
            'cf_memories',
            'cf_memory_apply_control',
            'cf_memory_operations',
            'cf_memory_commits',
            'cf_memory_outbox',
            'cf_jit_proactivity_events',
            'cf_jit_trigger_feedback',
        )
    }


@pytest.mark.parametrize(
    'action,weight,status',
    [
        ('useful', 1, 'active'),
        ('false_positive', -1, 'active'),
        ('missed_or_late', 0, 'active'),
        ('snooze', 0, 'active'),
        ('disable', 0, 'hidden'),
    ],
)
def test_upstream_actions_atomic_head_and_replay(api, env, action, weight, status):
    value = setup(api, env, action)
    before = state(env)
    response = send(api, value)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['applied'] and result['trigger_revision'] == value['trigger_revision'] + 1
    assert result['trigger_status'] == status
    row = env.APP_DB.row(value['trigger_memory_id'])
    assert read_item(row).curation_weight == read_item(before['cf_memories'][0]).curation_weight + weight
    assert row['status'] == status
    assert row['content'] == before['cf_memories'][0]['content']
    assert (
        json.loads(row['canonical_metadata_json'])['trigger_condition']
        == json.loads(before['cf_memories'][0]['canonical_metadata_json'])['trigger_condition']
    )
    feedback = json.loads(row['arguments_json'])['jit_trigger_feedback']
    assert feedback['feedback_count'] == 1 and feedback['last_action'] == action
    if action == 'snooze':
        assert feedback['snoozed_until'] == value['snoozed_until']
    after = state(env)
    assert (
        after['cf_memory_apply_control'][0]['commit_sequence']
        == before['cf_memory_apply_control'][0]['commit_sequence'] + 1
    )
    assert len(after['cf_memory_operations']) == len(before['cf_memory_operations']) + 1
    assert json.loads(after['cf_memory_operations'][-1]['operation_json'])['operation_type'] == 'ledger_mutation'
    assert json.loads(after['cf_jit_proactivity_events'][0]['record_json'])['feedback_id'] == value['feedback_id']
    replay = send(api, value)
    assert replay.status_code == 200 and replay.json()['applied'] is False, replay.text
    assert replay.json()['receipt'] == result['receipt']
    assert state(env) == after
    snapshot = call(api, 'GET', '/v1/jit/trigger-snapshot').json()
    assert snapshot['complete'], snapshot
    if action == 'disable':
        assert snapshot['rows'] == []
    elif action == 'snooze':
        assert datetime.fromisoformat(snapshot['rows'][0]['snoozed_until']) == datetime.fromisoformat(
            value['snoozed_until']
        )


@pytest.mark.parametrize('flags', [(0, 0), (1, 1)])
def test_user_feedback_remains_available_with_rollout_disabled_or_killed(api, env, flags):
    value = setup(api, env, 'disable')
    env.APP_DB.connection.execute('UPDATE cf_jit_flags SET rollout=?,kill_switch=?', flags)
    assert send(api, value).status_code == 200
    assert send(api, value).json()['applied'] is False


@pytest.mark.parametrize('change', ['revision', 'generation', 'event', 'owner', 'non_trigger', 'no_ledger', 'deleted'])
def test_stale_or_missing_authority_never_mutates(api, env, change):
    value = setup(api, env)
    uid = 'owner'
    if change == 'revision':
        value['trigger_revision'] += 1
    elif change == 'generation':
        value['account_generation'] += 1
    elif change == 'event':
        value['event_id'] = identity('missing-event')
    elif change == 'owner':
        uid = 'other'
    elif change in {'non_trigger', 'no_ledger'}:
        metadata = json.loads(env.APP_DB.row(value['trigger_memory_id'])['canonical_metadata_json'])
        metadata['kind' if change == 'non_trigger' else 'ledger_schema_version'] = (
            'fact' if change == 'non_trigger' else None
        )
        env.APP_DB.connection.execute(
            'UPDATE cf_memories SET canonical_metadata_json=? WHERE id=?',
            (json.dumps(metadata), value['trigger_memory_id']),
        )
    else:
        env.APP_DB.connection.execute(
            "INSERT INTO cf_account_deletion_tombstones(uid,completed_at,expires_at) VALUES('owner',1,2)"
        )
    before = state(env)
    response = send(api, value, uid=uid)
    assert response.status_code == 409, response.text
    assert state(env) == before


def test_auth_wire_changed_id_and_one_feedback_per_notification(api, env):
    value = setup(api, env)
    assert send(api, value, uid=None).status_code == 401
    for change in (
        {'feedback_id': 'private raw text'},
        {'note': 'private'},
        {'action': 'reinforce'},
        {'recorded_at': '2026-09-08T08:00:00'},
        {'action': 'snooze'},
        {'snoozed_until': NOW.isoformat()},
    ):
        assert send(api, {**value, **change}).status_code == 422
    assert send(api, value).status_code == 200
    before = state(env)
    for change in (
        {'action': 'disable'},
        {'feedback_id': identity('second'), 'trigger_revision': value['trigger_revision'] + 1},
    ):
        assert send(api, {**value, **change}).status_code == 409
    assert state(env) == before


def test_failed_last_receipt_rolls_back_memory_head_event_and_journal(api, env):
    value = setup(api, env)
    before = state(env)
    env.APP_DB.connection.executescript(
        "CREATE TRIGGER controlled_failure BEFORE INSERT ON cf_jit_trigger_feedback BEGIN SELECT RAISE(ABORT,'controlled final write failure'); END;"
    )
    assert send(api, value).status_code == 409
    assert state(env) == before
    for table in ('cf_memory_apply_guard', 'cf_candidate_write_guard'):
        assert rows(env, table) == []
    env.APP_DB.connection.execute('DROP TRIGGER controlled_failure')
    assert send(api, value).status_code == 200


def test_concurrent_identical_submission_returns_one_commit(api, env, monkeypatch):
    value = setup(api, env)
    original = env.APP_DB.batch
    injected = False

    async def competing(statements):
        nonlocal injected
        if not injected:
            injected = True
            await original(statements)
        return await original(statements)

    monkeypatch.setattr(env.APP_DB, 'batch', competing)
    response = send(api, value)
    assert response.status_code == 200 and response.json()['applied'] is False, response.text
    assert len(rows(env, 'cf_jit_trigger_feedback')) == 1
    assert env.APP_DB.row(value['trigger_memory_id'])['item_revision'] == value['trigger_revision'] + 1


def test_replay_rechecks_privacy_revocation_at_commit(api, env):
    value = setup(api, env)
    assert send(api, value).status_code == 200

    def revoke():
        env.APP_DB.connection.execute(
            "UPDATE cf_memories SET status='hidden',source_state='deleted' WHERE id=?", (value['trigger_memory_id'],)
        )

    env.APP_DB.before_write = revoke
    assert send(api, value).status_code == 409


def test_receipt_export_is_owner_scoped_and_cannot_be_written_outside_apply(api, env):
    value = setup(api, env)
    assert send(api, value).status_code == 200
    exported = call(api, 'GET', '/v1/users/export')
    assert exported.status_code == 200, exported.text
    assert len(exported.json()['jit_data']['jit_trigger_feedback']) == 1
    assert call(api, 'GET', '/v1/users/export', uid='other').json()['jit_data']['jit_trigger_feedback'] == []
    row = rows(env, 'cf_jit_trigger_feedback')[0]
    with pytest.raises(sqlite3.IntegrityError, match='candidate_apply_required'):
        env.APP_DB.connection.execute(
            'INSERT INTO cf_jit_trigger_feedback(uid,feedback_id,record_json) VALUES (?,?,?)',
            ('owner', identity('forged'), row['record_json']),
        )


def test_durable_replay_survives_bounded_local_history(api, env, monkeypatch):
    first = setup(api, env)
    assert send(api, first).status_code == 200
    for day in range(1, 34):
        monkeypatch.setattr(jit_proactivity_store, 'clock', lambda d=day: NOW + timedelta(days=d))
        revision = env.APP_DB.row(first['trigger_memory_id'])['item_revision']
        reservation = request(
            'day-' + str(day),
            'planned_notification',
            trigger_memory_id=first['trigger_memory_id'],
            trigger_revision=revision,
        )
        assert call(api, 'POST', '/v1/jit/proactivity/reservations', json=reservation).status_code == 200
        value = {
            **first,
            'feedback_id': identity('day-feedback-' + str(day)),
            'event_id': reservation['event_id'],
            'trigger_revision': revision,
        }
        assert send(api, value).status_code == 200
    feedback = json.loads(env.APP_DB.row(first['trigger_memory_id'])['arguments_json'])['jit_trigger_feedback']
    assert len(feedback['applied_feedback_ids']) == 32 and first['feedback_id'] not in feedback['applied_feedback_ids']
    before = state(env)
    assert send(api, first).json()['applied'] is False
    assert state(env) == before
