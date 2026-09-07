"""Public deletion, final transaction and durable scope continuation.

Final expectations follow backend/database/memory_ledger.py's
finalize_canonical_privacy_tombstones and canonical_memory_adapter's default
scope: remove physical history after provider absence and retain Archive.
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from memory_privacy_delete import continue_memory_scope, delete_memory_scope, delete_selected_memories
from memory_privacy_finalize import finalize_privacy_deletion
from memory_privacy_receipts import privacy_receipt_id
from test_memory_apply_intake import apply, database, rows, state  # noqa: F401
from test_memory_mutation_lock import target  # noqa: F401
from test_memory_privacy_apply import prepare, full_state
from test_memory_privacy_receipts import SECRET


def env(database):
    async def send(message):
        database.published.append(message)

    return SimpleNamespace(APP_DB=database, MEMORY_PRIVACY_SECRET=SECRET, JOBS=SimpleNamespace(send=send))


def artifact(database, uid, memory_id, *, writer_done=0):
    revision = database.connection.execute(
        'SELECT item_revision FROM cf_memories WHERE uid=? AND id=?', (uid, memory_id)
    ).fetchone()[0]
    database.connection.execute(
        'INSERT INTO cf_memory_vector_artifacts(vector_id,uid,source_id,attempt_id,sub_id,source_version,model,writer_until,writer_done) '
        "VALUES (?, ?, ?, ?, '0', ?, 'bge-m3', unixepoch()+900, ?)",
        (uid + memory_id, uid, memory_id, uid + memory_id, revision, writer_done),
    )


@pytest.mark.parametrize('path', ['/v3/memories/{id}', '/v1/mcp/memories/{id}', '/v1/dev/user/memories/{id}'])
def test_public_delete_finalizes_history_and_is_idempotent_for_its_owner(target, path):
    database, request, create = target
    memory_id = create(content='Privacy target')
    retained = create(content='Retained content')
    assert request('DELETE', path.format(id=memory_id), uid='other').status_code == 404
    result = request('DELETE', path.format(id=memory_id))
    assert result.status_code == 200, result.text
    assert database.row(memory_id) is None
    assert database.row(retained)['content'] == 'Retained content'
    assert all('Privacy target' not in row['operation_json'] for row in state(database)['cf_memory_operations'])
    assert request('DELETE', path.format(id=memory_id)).status_code == 200
    assert len(state(database)['cf_memory_operations']) == 1
    assert database.connection.execute('SELECT count(*) FROM cf_memory_privacy_receipts').fetchone()[0] == 1


def test_pending_external_writer_cannot_be_acknowledged_then_retry_finalizes(target):
    database, request, create = target
    memory_id = create()
    artifact(database, 'owner', memory_id)
    response = request('DELETE', f'/v3/memories/{memory_id}')
    assert response.status_code == 503
    assert response.json() == {'error': 'memory_cleanup_pending'}
    assert response.headers['retry-after'] == '2'
    assert database.row(memory_id)['content'] is None
    assert len(state(database)['cf_memory_operations']) == 2
    assert database.published[-1]['kind'] == 'memory_privacy_cleanup'
    before = full_state(database)
    assert request('DELETE', f'/v3/memories/{memory_id}').status_code == 503
    assert full_state(database) == before
    # The Jobs suite owns real provider observation; this seam represents its
    # completed journal cleanup, not acceptance of a provider delete request.
    database.connection.execute('DELETE FROM cf_memory_vector_artifacts')
    assert request('DELETE', f'/v3/memories/{memory_id}').status_code == 200
    assert database.row(memory_id) is None
    assert state(database)['cf_memory_operations'] == []
    assert state(database)['cf_vector_projection_outbox'] == []
    assert database.connection.execute('SELECT count(*) FROM cf_memory_privacy_deletions').fetchone()[0] == 0


def test_finalization_rechecks_provider_and_legal_authority_inside_transaction(database):
    apply(database, rows())
    inventory = prepare(database)

    def late_artifact():
        artifact(database, 'owner', 'memory-0')

    database.before_write = late_artifact
    with pytest.raises(sqlite3.IntegrityError, match='provider_pending'):
        asyncio.run(finalize_privacy_deletion(env(database), 'owner', inventory['token'], int(time.time())))
    assert database.row('memory-0') is not None
    assert len(state(database)['cf_memory_operations']) == 3
    assert database.connection.execute('SELECT count(*) FROM cf_memory_privacy_finalize_guard').fetchone()[0] == 0


def test_late_history_failure_rolls_back_physical_and_receipt_finalization(database):
    apply(database, rows())
    inventory = prepare(database)
    database.connection.execute(
        "CREATE TRIGGER fail_history BEFORE DELETE ON cf_memory_operations BEGIN SELECT RAISE(ABORT, 'history unavailable'); END"
    )
    before = full_state(database)
    with pytest.raises(sqlite3.IntegrityError, match='history unavailable'):
        asyncio.run(finalize_privacy_deletion(env(database), 'owner', inventory['token'], int(time.time())))
    assert full_state(database) == before


def test_pending_inventory_blocks_late_writes_after_receipt_expiry(database):
    apply(database, rows())
    inventory = prepare(database)
    database.connection.execute('DELETE FROM cf_memory_privacy_receipts')
    for sql in [
        "UPDATE cf_memories SET content='late replay',status='active',source_state='active',deleted_at=NULL WHERE id='memory-0'",
        "DELETE FROM cf_memories WHERE id='memory-0'",
    ]:
        with pytest.raises(sqlite3.IntegrityError, match='cleanup_pending'):
            database.connection.execute(sql)
    assert inventory['token']


def test_default_scope_retains_archive_even_when_linked_and_all_finishes_it(target):
    database, request, create = target
    short = create(content='Short-term private')
    archive = create(content='Archived private')
    database.connection.execute(
        "UPDATE cf_memories SET memory_tier='archive',superseded_by=? WHERE id=?", (short, archive)
    )
    response = request('DELETE', '/v3/memories?scope=default')
    assert response.status_code == 200, response.text
    assert database.row(short) is None and database.row(archive)['content'] == 'Archived private'
    assert request('DELETE', '/v3/memories?scope=all').status_code == 200
    assert database.row(archive) is None
    assert database.connection.execute('SELECT count(*) FROM cf_memory_privacy_scopes').fetchone()[0] == 0


def test_scope_continues_after_a_bounded_batch_and_isolated_owner_retains_data(database):
    values = [{**rows()[0], 'id': f'memory-{index:03}'} for index in range(102)]
    apply(database, values[:100])
    apply(database, values[100:])
    apply(database, rows('other'))
    assert asyncio.run(delete_memory_scope(env(database), 'owner', 'all')) is False
    assert database.connection.execute("SELECT count(*) FROM cf_memories WHERE uid='owner'").fetchone()[0] == 2
    scope = dict(database.connection.execute('SELECT * FROM cf_memory_privacy_scopes').fetchone())
    assert database.published[-1]['payload']['scope'] is True
    assert asyncio.run(continue_memory_scope(env(database), 'owner', scope['token'])) is True
    assert database.connection.execute("SELECT count(*) FROM cf_memories WHERE uid='owner'").fetchone()[0] == 0
    assert database.connection.execute("SELECT count(*) FROM cf_memories WHERE uid='other'").fetchone()[0] == 2
    assert asyncio.run(continue_memory_scope(env(database), 'owner', scope['token'])) is True


def test_private_continuation_requires_request_bound_internal_authority(database):
    from fastapi import FastAPI
    import httpx
    from internal_auth import create_request_context
    from memory_privacy_routes import router

    apply(database, rows())
    inventory = prepare(database)
    environment = env(database)
    environment.INTERNAL_ASSERTION_SECRET = 'privacy-private-test-secret-32-bytes'
    app = FastAPI()
    app.include_router(router)

    @app.middleware('http')
    async def attach(request, call_next):
        request.scope['env'] = environment
        return await call_next(request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://api-core') as client:
            path = '/internal/memory-privacy/resume'
            body = {'token': inventory['token']}
            assert (await client.post(path, json=body)).status_code == 404
            for authority in ('better-auth', 'internal'):
                encoded, signature = create_request_context(
                    'owner',
                    environment.INTERNAL_ASSERTION_SECRET,
                    audience='api-core',
                    method='POST',
                    path=path,
                    request_id='privacy',
                    authority=authority,
                )
                headers = {'x-omi-auth-context': encoded, 'x-omi-internal-signature': signature}
                response = await client.post(path, headers=headers, json=body)
                assert response.status_code == (200 if authority == 'internal' else 404)
            assert response.headers['cache-control'] == 'no-store'
            assert response.json()['targets'][0]['id'] == 'memory-0'
            # A valid signature for resume cannot authorize finalize.
            assert (
                await client.post('/internal/memory-privacy/finalize', headers=headers, json=body)
            ).status_code == 404
            malformed = await client.post(path, headers=headers, json={**body, 'uid': 'other'})
            assert malformed.status_code == 422

    asyncio.run(run())


def test_review_reference_values_are_scrubbed_and_unrelated_keys_are_retained(database):
    apply(database, rows())
    db = database.connection
    for review_id, candidate in [
        ('linked', {'nested': ['memory-0', 'private review']}),
        ('retained', {'memory-0': 'safe value'}),
    ]:
        db.execute(
            'INSERT INTO cf_memory_review_queue(uid,review_id,fact_id,candidate_json,source_commit_id,source_item_revision,source_content_hash,created_at,updated_at,expires_at) '
            "VALUES ('owner',?,'memory-1',?,'review-source',1,'source-hash',1,1,9999999999)",
            (review_id, json.dumps(candidate)),
        )
    assert asyncio.run(delete_selected_memories(env(database), 'owner', ['memory-0'])) is True
    assert [row[0] for row in db.execute('SELECT review_id FROM cf_memory_review_queue')] == ['retained']
    assert database.row('memory-1') is not None


def test_new_hold_after_abandoned_gate_prevents_final_erasure(database):
    apply(database, rows())
    inventory = prepare(database)
    database.connection.execute('UPDATE cf_destructive_operation_gates SET started_at=unixepoch()-21601')
    database.connection.execute("INSERT INTO cf_legal_holds VALUES ('owner','legal_hold.v1','admin',1,unixepoch())")
    before = full_state(database)
    with pytest.raises(sqlite3.IntegrityError, match='legal_hold_active'):
        asyncio.run(finalize_privacy_deletion(env(database), 'owner', inventory['token'], int(time.time())))
    assert full_state(database) == before


def test_mixed_archive_and_lifecycle_history_is_removed_without_deleting_survivors(database):
    apply(database, rows())
    apply(database, rows('other'))
    db = database.connection
    for index, (uid, memory_ids) in enumerate(
        [
            ('owner', ['memory-0', 'memory-1']),
            ('owner', ['memory-1']),
            ('other', ['memory-0', 'memory-1']),
        ],
        1,
    ):
        review_id = str(index).zfill(36)
        digest = str(index) * 64
        run_id = 'run-' + str(index)
        db.execute(
            'INSERT INTO cf_memory_archive_review_batches '
            '(review_id,uid,manifest_sha256,entry_count,status,reviewed_at,expires_at,updated_at) '
            "VALUES (?,?,?,?,'approved',1,9999999999,1)",
            (review_id, uid, digest, len(memory_ids)),
        )
        db.execute(
            'INSERT INTO cf_memory_short_term_lifecycle_runs '
            '(uid,run_id,request_fingerprint,evaluated_at,requested_limit,status,next_attempt_at,account_generation,created_at,updated_at) '
            "VALUES (?,?,?,1,100,'completed',1,0,1,1)",
            (uid, run_id, digest),
        )
        for item_index, memory_id in enumerate(memory_ids, 1):
            db.execute(
                'INSERT INTO cf_memory_archive_review_items '
                '(review_id,uid,memory_id,import_id,source_fingerprint,source_row_sha256,plan_hash,account_generation,row_json,created_at,updated_at) '
                'VALUES (?,?,?,?,?,?,?,0,?,1,1)',
                (
                    review_id,
                    uid,
                    memory_id,
                    str(item_index) * 64,
                    digest,
                    digest,
                    digest,
                    json.dumps({'memory_id': memory_id, 'content': 'Private archival review'}),
                ),
            )
            db.execute(
                'INSERT INTO cf_memory_short_term_lifecycle_transitions '
                '(uid,transition_id,memory_id,run_id,outcome,reason,evaluated_at,audit_metadata_json,idempotency_key,fingerprint,account_generation,created_at) '
                "VALUES (?,?,?,?,'remain_short_term','Private decision','now','{}',?,?,0,1)",
                (uid, run_id + memory_id, memory_id, run_id, run_id + memory_id, digest),
            )
    assert asyncio.run(delete_selected_memories(env(database), 'owner', ['memory-0'])) is True
    assert db.execute("SELECT id FROM cf_memories WHERE uid='owner' ORDER BY id").fetchall()[0][0] == 'memory-1'
    assert db.execute("SELECT count(*) FROM cf_memories WHERE uid='owner'").fetchone()[0] == 1
    for table in ('cf_memory_archive_review_batches', 'cf_memory_archive_review_items'):
        assert [row[0] for row in db.execute('SELECT review_id FROM ' + table + " WHERE uid='owner'")] == [
            str(2).zfill(36)
        ]
    for table in ('cf_memory_short_term_lifecycle_runs', 'cf_memory_short_term_lifecycle_transitions'):
        assert [row[0] for row in db.execute('SELECT run_id FROM ' + table + " WHERE uid='owner'")] == ['run-2']
    assert db.execute("SELECT count(*) FROM cf_memory_archive_review_items WHERE uid='other'").fetchone()[0] == 2
    assert (
        db.execute("SELECT count(*) FROM cf_memory_short_term_lifecycle_transitions WHERE uid='other'").fetchone()[0]
        == 2
    )
    assert db.execute("SELECT count(*) FROM cf_memories WHERE uid='other'").fetchone()[0] == 2
