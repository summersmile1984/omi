"""Actual Core ASGI and migrated D1 ledger append, replay and failure boundaries."""

import asyncio
import json
import uuid

import pytest
from test_candidate_entry import api
from test_candidate_routes import call
from test_jit_proactivity_reservations import env
from test_memory_history_routes import seed as history_seed


def seed(api, env, *, identity, meta=None, **physical):
    history_seed(api, env, identity=identity)
    row = env.APP_DB.row(identity)
    metadata = json.loads(row["canonical_metadata_json"])
    metadata['valid_from'] = '2019-01-01T00:00:00Z'
    metadata.update(meta or {})
    updates = {**physical, "canonical_metadata_json": json.dumps(metadata)}
    env.APP_DB.connection.execute(
        "UPDATE cf_memories SET " + ",".join(k + "=?" for k in updates) + " WHERE id=?", (*updates.values(), identity)
    )


def restore(api, memory_id='old', operation=None, **kwargs):
    return call(
        api,
        'POST',
        '/v3/memories/' + memory_id + '/revert',
        json={'operation_id': operation or str(uuid.uuid4())},
        **kwargs
    )


def chain(api, env):
    seed(
        api,
        env,
        identity='old',
        superseded_by='tail',
        meta={'canonical_memory_id': 'tail', 'valid_to': '2020-01-01T00:00:00Z'},
    )
    seed(
        api,
        env,
        identity='tail',
        status='active',
        content='Now lives in Beijing.',
        meta={'valid_to': None, 'valid_from': '2020-01-01T00:00:00Z'},
    )
    env.APP_DB.connection.commit()


def counts(env):
    return [
        env.APP_DB.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
        for table in [
            'cf_memories',
            'cf_memory_operations',
            'cf_memory_commits',
            'cf_memory_outbox',
            'cf_memory_ledger_reopens',
        ]
    ]


def test_revert_appends_and_closes_current_tail_with_exact_replay(api, env):
    chain(api, env)
    before = dict(env.APP_DB.row('old'))
    operation = str(uuid.uuid4())
    response = restore(api, operation=operation)
    assert response.status_code == 200, response.text
    row = response.json()['memory']
    assert row['content'] == before['content'] and row['id'] not in {'old', 'tail'}
    assert response.headers['cache-control'] == 'no-store'
    assert env.APP_DB.row('tail')['superseded_by'] == row['id']
    assert env.APP_DB.row('tail')['status'] == 'superseded'
    assert dict(env.APP_DB.row('old')) == before
    after = counts(env)
    again = restore(api, operation=operation)
    assert again.status_code == 200, again.text
    assert again.json() == response.json() and counts(env) == after
    assert restore(api).status_code == 409


def test_standalone_reopen_preserves_closed_source_and_one_receipt(api, env):
    seed(api, env, identity='old', meta={'valid_to': '2020-01-01T00:00:00Z'})
    env.APP_DB.connection.commit()
    source = dict(env.APP_DB.row('old'))
    operation = str(uuid.uuid4())
    first = restore(api, operation=operation)
    assert first.status_code == 200, first.text
    assert dict(env.APP_DB.row('old')) == source
    assert counts(env)[-1] == 1
    again = restore(api, operation=operation)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert restore(api).status_code == 409
    assert counts(env)[-1] == 1


@pytest.mark.parametrize(
    'kind,status', [('locked', 402), ('deleted', 409), ('sensitive', 409), ('pending', 409), ('missing', 404)]
)
def test_unrestorable_source_never_writes(api, env, kind, status):
    chain(api, env)
    sql = {
        'locked': 'is_locked=1',
        'deleted': 'deleted_at=1',
        'sensitive': '''sensitivity_labels_json='["credential"]' ''',
        'pending': "processing_state='pending'",
    }
    if kind in sql:
        env.APP_DB.connection.execute('UPDATE cf_memories SET ' + sql[kind] + " WHERE id='old'")
        env.APP_DB.connection.commit()
    before = counts(env)
    response = restore(api, memory_id='missing' if kind == 'missing' else 'old')
    assert response.status_code == status, response.text
    assert counts(env) == before


def test_final_statement_failure_rolls_back_every_ledger_surface(api, env):
    chain(api, env)
    env.APP_DB.connection.execute(
        "CREATE TRIGGER fail_restore BEFORE DELETE ON cf_memory_apply_guard BEGIN SELECT RAISE(ABORT,'injected final failure'); END"
    )
    env.APP_DB.connection.commit()
    before = counts(env)
    snapshot = dict(env.APP_DB.row('tail'))
    response = restore(api)
    assert response.status_code == 503, response.text
    assert counts(env) == before and dict(env.APP_DB.row('tail')) == snapshot
    for table in ['cf_memory_apply_guard', 'cf_memory_ledger_read_guard']:
        assert env.APP_DB.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0


def test_authentication_and_owner_isolation(api, env):
    chain(api, env)
    assert restore(api, uid=None).status_code == 401
    assert restore(api, uid='another').status_code == 404
    assert restore(api, operation='invalid').status_code == 422


@pytest.mark.parametrize('standalone', [False, True])
def test_two_simultaneous_requests_commit_once(api, env, standalone):
    if standalone:
        seed(api, env, identity='old', meta={'valid_to': '2020-01-01T00:00:00Z'})
    else:
        chain(api, env)
    before = counts(env)
    operation = str(uuid.uuid4())
    original_batch = env.APP_DB.batch

    async def race():
        entered = 0
        ready = asyncio.Event()

        async def batch(statements):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            return await original_batch(statements)

        env.APP_DB.batch = batch
        return await asyncio.wait_for(
            asyncio.gather(
                *[api('POST', '/v3/memories/old/revert', json={'operation_id': operation}) for _ in range(2)]
            ),
            timeout=5,
        )

    responses = asyncio.run(race())
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    after = counts(env)
    assert after[0] == before[0] + 1 and after[1] == before[1] + 1


@pytest.mark.parametrize('change,status', [('lock', 402), ('text', 409), ('delete', 409)])
def test_closed_source_change_at_atomic_boundary_cannot_copy_stale_text(api, env, change, status):
    chain(api, env)
    before = counts(env)
    sql = {'lock': 'is_locked=1', 'text': "content='Now lives in Beijing.'", 'delete': 'deleted_at=1'}[change]
    env.APP_DB.before_write = lambda: env.APP_DB.connection.execute("UPDATE cf_memories SET " + sql + " WHERE id='old'")
    response = restore(api)
    assert response.status_code == status, response.text
    assert counts(env) == before
    assert env.APP_DB.row('tail')['status'] == 'active'


def test_new_operation_after_standalone_reopen_cannot_create_second_tail(api, env):
    seed(api, env, identity='old', meta={'valid_to': '2020-01-01T00:00:00Z'})
    original_batch = env.APP_DB.batch

    async def race():
        entered = 0
        ready = asyncio.Event()

        async def batch(statements):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            return await original_batch(statements)

        env.APP_DB.batch = batch
        return await asyncio.wait_for(
            asyncio.gather(
                *[api('POST', '/v3/memories/old/revert', json={'operation_id': str(uuid.uuid4())}) for _ in range(2)]
            ),
            timeout=5,
        )

    results = asyncio.run(race())
    assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
    assert counts(env)[0] == 2 and counts(env)[-1] == 1


@pytest.mark.parametrize('deleted', ['source', 'replacement'])
def test_public_privacy_delete_removes_reopen_receipt_and_blocks_old_retry(api, env, deleted):
    seed(api, env, identity='old', meta={'valid_to': '2020-01-01T00:00:00Z'})
    operation = str(uuid.uuid4())
    first = restore(api, operation=operation)
    assert first.status_code == 200, first.text
    replacement = first.json()['memory']['id']
    key = 'old' if deleted == 'source' else replacement
    response = call(api, 'DELETE', '/v3/memories/' + key)
    assert response.status_code == 200, response.text
    assert counts(env)[-1] == 0
    retry = restore(api, operation=operation)
    assert retry.status_code in {404, 409}, retry.text


def test_unrelated_erasure_does_not_prevent_new_restore(api, env):
    chain(api, env)
    other = call(api, 'POST', '/v3/memories', json={'content': 'Unrelated removable fixture.'}).json()['id']
    assert call(api, 'DELETE', '/v3/memories/' + other).status_code == 200
    response = restore(api)
    assert response.status_code == 200, response.text
    assert env.APP_DB.row(response.json()['memory']['id'])['privacy_receipt_id']
