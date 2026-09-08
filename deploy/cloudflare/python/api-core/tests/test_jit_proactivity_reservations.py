"""Original JIT reservation policy through production ASGI and App migration SQL."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_candidate_entry import api
from test_candidate_routes import call
from test_memory_mutation_lock import Database
import jit_proactivity_store as store

PATH = '/v1/jit/proactivity/reservations'
NOW = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)


def identity(value):
    return hashlib.sha256(value.encode()).hexdigest()


def request(event='one', operation='ambient_notification', **extra):
    return dict(
        event_id=identity(event),
        candidate_id=identity('candidate'),
        device_id=identity('device'),
        operation=operation,
        account_generation=0,
        **extra
    )


@pytest.fixture
def env(monkeypatch):
    db = Database()
    monkeypatch.setattr(store, 'clock', lambda: NOW)
    yield SimpleNamespace(APP_DB=db, MEMORY_PRIVACY_SECRET='jit-offline-fixture-secret-32-bytes')
    db.connection.close()


def setup(api, env, zone='Asia/Shanghai'):
    env.APP_DB.connection.execute("INSERT INTO cf_jit_flags(uid,rollout,kill_switch,updated_at) VALUES ('owner',1,0,1)")
    response = call(api, 'POST', '/v1/users/fcm-token', json={'fcm_token': 'fixture-token', 'time_zone': zone})
    assert response.status_code == 200, response.text
    response = call(api, 'POST', '/v3/memories', json={'content': 'Synthetic reminder preference.'})
    assert response.status_code in {200, 201}, response.text
    return response.json()['id']


def send(api, value=None, **kwargs):
    return call(api, 'POST', PATH, json=value or request(), **kwargs)


def rows(env, table='cf_jit_proactivity_events'):
    return [dict(row) for row in env.APP_DB.connection.execute('SELECT * FROM ' + table)]


def test_actual_entry_reservation_replay_and_changed_payload(api, env):
    setup(api, env)
    first = send(api)
    assert first.status_code == 200, first.text
    replay = send(api)
    assert replay.status_code == 200 and replay.json()['reserved'] is False, replay.text
    assert replay.json()['receipt'] == first.json()['receipt']
    changed = {**request(), 'device_id': identity('other-device')}
    assert send(api, changed).status_code == 409
    assert len(rows(env)) == 1
    assert json.loads(rows(env, 'cf_jit_proactivity_daily_budgets')[0]['record_json'])['total_notifications'] == 1


@pytest.mark.parametrize(
    'operation,limit,field', [('ambient_notification', 3, 'total_notifications'), ('nano_triage', 8, 'nano_triages')]
)
def test_cross_device_daily_limit(api, env, operation, limit, field):
    setup(api, env)
    for number in range(limit + 1):
        value = {**request(str(number), operation), 'device_id': identity(str(number))}
        response = send(api, value)
        assert response.status_code == (200 if number < limit else 409), response.text
    assert len(rows(env)) == limit
    assert json.loads(rows(env, 'cf_jit_proactivity_daily_budgets')[0]['record_json'])[field] == limit


def test_full_turn_requires_same_parent_and_only_once_per_candidate(api, env):
    setup(api, env)
    assert send(api).status_code == 200
    value = request('turn', 'full_turn', parent_event_id=identity('one'))
    assert send(api, {**value, 'device_id': identity('wrong-device')}).status_code == 409
    first = send(api, value)
    assert first.status_code == 200, first.text
    assert send(api, value).json()['reserved'] is False
    assert send(api, {**value, 'event_id': identity('second-turn')}).status_code == 409
    assert len(rows(env, 'cf_jit_proactivity_candidate_turns')) == 1


@pytest.mark.parametrize('shape', ['missing_timezone', 'invalid_timezone', 'no_control', 'disabled'])
def test_legacy_and_unavailable_authority_fail_closed(api, env, shape):
    setup(api, env)
    sql = {
        'missing_timezone': 'DELETE FROM cf_user_fcm_tokens',
        'invalid_timezone': "UPDATE cf_user_fcm_tokens SET time_zone='Invalid/Timezone'",
        'no_control': 'DELETE FROM cf_memory_apply_control',
        'disabled': 'UPDATE cf_jit_flags SET rollout=0',
    }
    env.APP_DB.connection.execute(sql[shape])
    response = send(api)
    assert (
        response.status_code
        == {'missing_timezone': 409, 'invalid_timezone': 409, 'no_control': 503, 'disabled': 403}[shape]
    ), response.text
    assert not rows(env)


def test_failed_last_write_rolls_back_all_budget_state(api, env):
    setup(api, env)
    env.APP_DB.connection.executescript(
        "CREATE TRIGGER fail_jit_receipt BEFORE INSERT ON cf_jit_proactivity_events BEGIN SELECT RAISE(ABORT,'controlled final write failure'); END;"
    )
    response = send(api)
    assert response.status_code == 503, response.text
    for table in (
        'cf_jit_proactivity_events',
        'cf_jit_proactivity_daily_budgets',
        'cf_jit_proactivity_budget_controls',
        'cf_candidate_write_guard',
        'cf_jit_reservation_guard',
    ):
        assert not rows(env, table), table


def test_kill_switch_changed_during_commit_cannot_publish(api, env):
    setup(api, env)
    env.APP_DB.before_write = lambda: env.APP_DB.connection.execute('UPDATE cf_jit_flags SET kill_switch=1')
    response = send(api)
    assert response.status_code == 403, response.text
    assert not rows(env)


def test_replay_rechecks_current_switch(api, env):
    setup(api, env)
    assert send(api).status_code == 200
    env.APP_DB.before_write = lambda: env.APP_DB.connection.execute('UPDATE cf_jit_flags SET kill_switch=1')
    assert send(api).status_code == 403
    assert len(rows(env)) == 1


def test_timezone_cannot_reset_active_window(api, env, monkeypatch):
    setup(api, env)
    assert send(api).status_code == 200
    env.APP_DB.connection.execute("UPDATE cf_user_fcm_tokens SET time_zone='America/Los_Angeles'")
    assert send(api, request('second')).status_code == 409
    monkeypatch.setattr(store, 'clock', lambda: NOW + timedelta(days=1))
    assert send(api, request('third')).status_code == 200


def test_dst_midnight_uses_original_zoneinfo_policy(api, env, monkeypatch):
    setup(api, env, 'America/New_York')
    monkeypatch.setattr(store, 'clock', lambda: datetime(2026, 3, 8, 5, tzinfo=timezone.utc))
    assert send(api).status_code == 200
    control = json.loads(rows(env, 'cf_jit_proactivity_budget_controls')[0]['record_json'])
    assert control['window_ends_at'] == '2026-03-09T04:00:00+00:00'


def test_auth_invalid_body_and_owner_export(api, env):
    setup(api, env)
    assert send(api, uid=None).status_code == 401
    assert send(api, {**request(), 'event_id': 'raw private text'}).status_code == 422
    assert send(api, {**request(), 'extra': 'x' * 9000}).status_code == 422
    assert send(api).status_code == 200
    exported = call(api, 'GET', '/v1/users/export')
    assert exported.status_code == 200, exported.text
    assert len(exported.json()['jit_data']['jit_proactivity_events']) == 1
    other = call(api, 'GET', '/v1/users/export', uid='other')
    assert other.status_code == 200 and other.json()['jit_data']['jit_proactivity_events'] == []


def trigger_fixture(api, env):
    memory_id = setup(api, env)
    row = env.APP_DB.row(memory_id)
    metadata = json.loads(row['canonical_metadata_json'])
    metadata.update(
        kind='trigger',
        ledger_schema_version='knowledge_ledger.v1',
        subject_scope='primary_user',
        intent_backed=True,
        write_reason='standing_trigger',
        trigger_condition={
            'keywords': ['release'],
            'action': {'type': 'agent_prompt', 'prompt': 'Find the next release step.'},
        },
    )
    env.APP_DB.connection.execute(
        "UPDATE cf_memories SET processing_state='processed',memory_tier='long_term',canonical_metadata_json=?,arguments_json=? WHERE id=?",
        (json.dumps(metadata), json.dumps({'wakeup_budget_per_day': 1}), memory_id),
    )
    return memory_id, env.APP_DB.row(memory_id)['item_revision']


def test_planned_trigger_budget_and_stale_revision(api, env):
    memory_id, revision = trigger_fixture(api, env)
    value = request('planned', 'planned_notification', trigger_memory_id=memory_id, trigger_revision=revision)
    response = send(api, value)
    assert response.status_code == 200, response.text
    assert send(api, {**value, 'event_id': identity('second')}).status_code == 409
    assert send(api, {**value, 'trigger_revision': revision + 1}).status_code == 409
    assert len(rows(env)) == 1


@pytest.mark.parametrize('change', ['snooze', 'metadata', 'privacy'])
def test_trigger_changed_before_commit_revokes_admission(api, env, change):
    memory_id, revision = trigger_fixture(api, env)
    value = request('planned', 'planned_notification', trigger_memory_id=memory_id, trigger_revision=revision)

    def revoke():
        if change == 'snooze':
            args = {
                'wakeup_budget_per_day': 1,
                'jit_trigger_feedback': {'snoozed_until': (NOW + timedelta(days=1)).isoformat()},
            }
            env.APP_DB.connection.execute(
                'UPDATE cf_memories SET arguments_json=? WHERE id=?', (json.dumps(args), memory_id)
            )
        elif change == 'metadata':
            metadata = json.loads(env.APP_DB.row(memory_id)['canonical_metadata_json'])
            # Keep a valid MemoryItem while revoking its paid-work action.
            # MemoryItem.validate_tier_invariants rejects an intent-backed
            # write reason paired with intent_backed=False as malformed (503).
            metadata['trigger_condition']['action']['prompt'] = ' '
            env.APP_DB.connection.execute(
                'UPDATE cf_memories SET canonical_metadata_json=? WHERE id=?', (json.dumps(metadata), memory_id)
            )
        else:
            env.APP_DB.connection.execute(
                "UPDATE cf_memories SET sensitivity_labels_json='[\"restricted\"]' WHERE id=?", (memory_id,)
            )

    env.APP_DB.before_write = revoke
    response = send(api, value)
    assert response.status_code == 409, response.text
    assert not rows(env)


def test_competing_reservation_is_rechecked_before_committing_last_slot(api, env):
    setup(api, env)
    assert send(api).status_code == 200
    assert send(api, request('two')).status_code == 200

    def competing():
        # Model a peer's committed quota write through the actual SQL guard.
        row = rows(env, 'cf_jit_proactivity_daily_budgets')[0]
        budget = json.loads(row['record_json'])
        budget['total_notifications'] = 3
        db = env.APP_DB.connection
        db.execute('BEGIN')
        db.execute(
            'INSERT INTO cf_candidate_write_guard(uid,account_generation,jit_budgets_json) VALUES (?,?,?)',
            ('owner', 0, json.dumps([{'id': row['budget_day'], 'before': row['record_json']}])),
        )
        db.execute('UPDATE cf_jit_proactivity_daily_budgets SET record_json=?', (json.dumps(budget),))
        db.execute('DELETE FROM cf_candidate_write_guard')
        db.execute('COMMIT')

    env.APP_DB.before_write = competing
    assert send(api, request('three')).status_code == 409
    assert len(rows(env)) == 2


def test_retry_preserves_original_server_timestamp(api, env, monkeypatch):
    setup(api, env)
    env.APP_DB.connection.execute("INSERT INTO cf_jit_flags(uid,rollout,kill_switch,updated_at) VALUES ('',1,0,1)")
    calls = []

    def now():
        calls.append(True)
        return NOW + timedelta(days=len(calls) - 1)

    monkeypatch.setattr(store, 'clock', now)
    env.APP_DB.before_write = lambda: env.APP_DB.connection.execute(
        "UPDATE cf_jit_flags SET rollout=NULL WHERE uid='owner'"
    )
    response = send(api)
    assert response.status_code == 200, response.text
    assert datetime.fromisoformat(response.json()['receipt']['created_at']) == NOW
    assert len(calls) == 1 and len(rows(env)) == 1


def test_generation_becoming_unavailable_returns_authority_conflict(api, env, monkeypatch):
    setup(api, env)

    async def unavailable(*args):
        raise store.CandidateGenerationMismatchError('controlled concurrent account deletion')

    monkeypatch.setattr(store, 'account_generation', unavailable)
    assert send(api).status_code == 409
    assert not rows(env)


def test_malformed_trigger_uses_original_strict_read_failure(api, env):
    memory_id, revision = trigger_fixture(api, env)
    metadata = json.loads(env.APP_DB.row(memory_id)['canonical_metadata_json'])
    metadata['kind'] = 'private-malformed-trigger-kind'
    env.APP_DB.connection.execute(
        'UPDATE cf_memories SET canonical_metadata_json=? WHERE id=?', (json.dumps(metadata), memory_id)
    )
    response = send(
        api, request('planned', 'planned_notification', trigger_memory_id=memory_id, trigger_revision=revision)
    )
    assert response.status_code == 503, response.text
    assert 'private-malformed' not in response.text
    assert not rows(env)
