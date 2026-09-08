"""Actual ASGI/D1 consumers; migration receipts are explicit controlled fixtures."""

import asyncio
from datetime import datetime, timezone
import json

import pytest

from test_candidate_entry import api
from test_candidate_routes import call
from test_jit_proactivity_reservations import env
from test_memory_revert_routes import seed
from memory_apply_item import read_item
from memory_apply_intake import control_statement
from memory_history_wire import memory_item_to_memorydb
from memory_kernel_apply import MemoryControlState
from jit_ledger_snapshot_kernel import (
    LedgerMigrationCompletion,
    LedgerPromptProjectionReceipt,
    _bounded_prompt_projection,
    _prompt_eligible,
    _decode_cursor,
)
import jit_ledger_snapshot_store as storage
import memory_history_wire as wire

PROMPT = '/v1/jit/knowledge-ledger/prompt-snapshot'
MIRROR = '/v1/jit/knowledge-ledger/mirror-snapshot'
SECRET = 'controlled-independent-ledger-cursor-secret'


@pytest.fixture(autouse=True)
def cursor_key(env):
    env.MEMORY_V3_CURSOR_SECRET = SECRET


def publish_fixture(env, *, row_ids=None):
    raw = env.APP_DB.connection.execute(
        "SELECT control_json FROM cf_memory_apply_control WHERE uid='owner'"
    ).fetchone()[0]
    control = MemoryControlState.model_validate({**json.loads(raw), 'writer_mode': 'ledger', 'writer_epoch': 1})
    asyncio.run(env.APP_DB.batch([control_statement(env.APP_DB, 'owner', control)]))
    rows = [dict(r) for r in env.APP_DB.connection.execute("SELECT * FROM cf_memories WHERE uid='owner' ORDER BY id")]
    projected = [memory_item_to_memorydb(read_item(row)) for row in rows if row_ids is None or row['id'] in row_ids]
    projected = [row for row in projected if _prompt_eligible(row)]
    now = datetime.now(timezone.utc)
    completion = LedgerMigrationCompletion(
        completed_at=now,
        source_head_commit_id=control.head_commit_id,
        writer_epoch=1,
        migrated_long_term_count=len(rows),
        adjudicated_short_term_count=0,
    )
    receipt = LedgerPromptProjectionReceipt(
        uid='owner',
        generated_at=now,
        source_head_commit_id=control.head_commit_id,
        account_generation=control.account_generation,
        source_generation=control.source_generation,
        writer_epoch=1,
        scanned_row_count=len(rows),
        rows=_bounded_prompt_projection(projected),
    )
    env.APP_DB.connection.execute(
        'INSERT OR REPLACE INTO cf_knowledge_ledger_snapshots VALUES (?,?,?)',
        ('owner', completion.model_dump_json(), receipt.model_dump_json()),
    )
    return receipt


def setup(api, env, count=3):
    for number in range(count):
        seed(api, env, identity='memory-' + str(number), status='active', meta={'valid_to': None})
    return publish_fixture(env)


def read(api, path, **kwargs):
    return call(api, 'GET', path, **kwargs)


def test_absent_authority_preserves_disabled_and_compatibility_without_writes(api, env):
    assert read(api, PROMPT, uid=None).status_code == 401
    assert read(api, MIRROR, uid=None).status_code == 401
    assert read(api, PROMPT).json()['mode'] == 'disabled'
    assert read(api, MIRROR).json()['failure_reason'] == 'rollout_not_enabled'
    env.APP_DB.connection.execute("INSERT INTO cf_jit_flags VALUES ('owner',1,0,1)")
    response = read(api, PROMPT)
    assert response.status_code == 200 and response.json()['mode'] == 'compatibility'
    assert response.json()['reason'] == 'migration_incomplete' and response.json()['rows'] == []
    assert read(api, MIRROR).json()['failure_reason'] == 'migration_not_authoritative'
    assert env.APP_DB.connection.execute('SELECT COUNT(*) FROM cf_knowledge_ledger_snapshots').fetchone()[0] == 0
    assert env.APP_DB.connection.execute('SELECT COUNT(*) FROM cf_memory_apply_control').fetchone()[0] == 0


def test_prompt_requires_current_proof_and_preserves_original_projection(api, env):
    receipt = setup(api, env)
    before = env.APP_DB.connection.execute(
        "SELECT control_json FROM cf_memory_apply_control WHERE uid='owner'"
    ).fetchone()[0]
    response = read(api, PROMPT + '?uid=another')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['mode'] == 'enabled' and result['source_head_commit_id'] == receipt.source_head_commit_id
    assert result['rows'] == [row.model_dump(mode='json') for row in receipt.rows]
    assert all(not row['evidence'] and row['body'] is None for row in result['rows'])
    assert response.headers['cache-control'] == 'no-store'
    assert (
        env.APP_DB.connection.execute("SELECT control_json FROM cf_memory_apply_control WHERE uid='owner'").fetchone()[
            0
        ]
        == before
    )
    assert read(api, PROMPT, uid='another').json()['rows'] == []


def test_mirror_cursor_chain_is_complete_signed_and_owner_bound(api, env):
    setup(api, env)
    response = read(api, MIRROR + '?page_size=2')
    assert response.status_code == 200, response.text
    first = response.json()
    assert first['failure_reason'] is None and not first['final_page']
    assert [row['memory_id'] for row in first['rows']] == ['memory-0', 'memory-1']
    assert first['scanned_count'] == 2 and first['projected_count'] == 2
    cursor = first['next_cursor']
    decoded = _decode_cursor(cursor, uid='owner', secret=SECRET.encode())
    assert decoded.epoch_id == first['epoch_id'] and decoded.chain_revision == first['chain_revision']
    second = read(api, MIRROR + '?page_size=2&cursor=' + cursor).json()
    assert second['failure_reason'] is None and second['final_page'] and second['next_cursor'] is None
    assert second['scanned_count'] == 3 and second['projected_count'] == 3
    assert [row['memory_id'] for row in second['rows']] == ['memory-2']
    assert second['epoch_id'] == first['epoch_id'] and second['chain_revision'] != first['chain_revision']
    assert read(api, MIRROR + '?cursor=' + cursor + 'tampered').json()['failure_reason'] == 'invalid_cursor'
    env.APP_DB.connection.execute("INSERT INTO cf_jit_flags VALUES ('another',1,0,1)")
    assert read(api, MIRROR + '?cursor=' + cursor, uid='another').json()['failure_reason'] == 'invalid_cursor'


@pytest.mark.parametrize('mutation', ['mode', 'epoch', 'head', 'source', 'projection_owner'])
def test_stale_proofs_cannot_enable_snapshots(api, env, mutation):
    setup(api, env)
    if mutation in {'mode', 'epoch', 'head', 'source'}:
        row = env.APP_DB.connection.execute(
            "SELECT control_json FROM cf_memory_apply_control WHERE uid='owner'"
        ).fetchone()[0]
        control = MemoryControlState.model_validate_json(row)
        values = {
            'mode': {'writer_mode': 'compatibility'},
            'epoch': {'writer_epoch': 2},
            'head': {'head_commit_id': 'new-head'},
            'source': {'source_generation': 2},
        }[mutation]
        control = MemoryControlState.model_validate({**control.model_dump(), **values})
        asyncio.run(env.APP_DB.batch([control_statement(env.APP_DB, 'owner', control)]))
    else:
        env.APP_DB.connection.execute(
            "UPDATE cf_knowledge_ledger_snapshots SET projection_json=json_set(projection_json,'$.account_generation',99)"
        )
    response = read(api, PROMPT)
    assert response.status_code == 200, response.text
    assert response.json()['mode'] == 'compatibility' and response.json()['rows'] == []
    mirror = read(api, MIRROR).json()
    assert (
        mirror['failure_reason'] == 'migration_not_authoritative' and not mirror['final_page'] and mirror['rows'] == []
    )


def test_changed_head_rejects_old_cursor_even_with_new_valid_proof(api, env):
    setup(api, env)
    first = read(api, MIRROR + '?page_size=1').json()
    assert call(api, 'POST', '/v3/memories', json={'content': 'A new native submission.'}).status_code == 200
    publish_fixture(env)
    response = read(api, MIRROR + '?cursor=' + first['next_cursor']).json()
    assert response['failure_reason'] == 'epoch_changed' and not response['final_page'] and response['rows'] == []


@pytest.mark.parametrize('path', [PROMPT, MIRROR])
def test_global_kill_during_read_suppresses_authority(api, env, monkeypatch, path):
    setup(api, env)
    original = storage.LedgerSnapshotStore.authority

    async def change(self):
        value = await original(self)
        env.APP_DB.connection.execute("INSERT OR REPLACE INTO cf_jit_flags VALUES ('',1,1,1)")
        return value

    monkeypatch.setattr(storage.LedgerSnapshotStore, 'authority', change)
    result = read(api, path).json()
    assert result['rows'] == []
    assert result.get('mode') == 'killed' if path == PROMPT else result['failure_reason'] == 'rollout_not_enabled'


def test_mirror_rejects_a_page_changed_before_trailing_fence(api, env, monkeypatch):
    setup(api, env)
    original = storage.LedgerSnapshotStore.rows

    async def change(self, *args):
        result = await original(self, *args)
        env.APP_DB.connection.execute("UPDATE cf_memories SET content='changed' WHERE id='memory-0'")
        return result

    monkeypatch.setattr(storage.LedgerSnapshotStore, 'rows', change)
    result = read(api, MIRROR).json()
    assert result['failure_reason'] == 'authority_changed' and result['rows'] == [] and not result['final_page']


def test_privacy_delete_removes_stored_projection_at_the_same_commit(api, env):
    receipt = setup(api, env)
    memory_id = receipt.rows[0].id
    response = call(api, 'DELETE', '/v3/memories/' + memory_id)
    assert response.status_code == 200, response.text
    assert (
        env.APP_DB.connection.execute(
            "SELECT COUNT(*) FROM cf_knowledge_ledger_snapshots WHERE uid='owner'"
        ).fetchone()[0]
        == 0
    )
    assert read(api, PROMPT).json()['mode'] == 'compatibility'
    assert read(api, MIRROR).json()['failure_reason'] == 'migration_not_authoritative'


@pytest.mark.parametrize('size', [0, 501, -1])
def test_original_mirror_page_limits_are_not_silently_shrunk(api, env, size):
    setup(api, env)
    result = read(api, MIRROR + '?page_size=' + str(size)).json()
    assert result['failure_reason'] == 'invalid_page_size' and result['rows'] == []


def test_time_dependent_belief_does_not_invalidate_unchanged_projection(api, env, monkeypatch):
    monkeypatch.setenv(wire.MEMORY_BELIEF_MODEL_ENABLED_ENV, 'true')
    receipt = setup(api, env, count=1)
    original = storage.memory_item_to_memorydb

    def later(item):
        projected = original(item)
        return projected.model_copy(update={'currency': 0.0, 'currency_band': 'history'})

    monkeypatch.setattr(storage, 'memory_item_to_memorydb', later)
    response = read(api, PROMPT)
    assert response.status_code == 200 and response.json()['mode'] == 'enabled'
    assert response.json()['rows'] == [row.model_dump(mode='json') for row in receipt.rows]
    env.APP_DB.connection.execute("UPDATE cf_memories SET content='changed' WHERE id='memory-0'")
    assert read(api, PROMPT).json()['reason'] == 'projection_receipt_stale'


def test_prompt_source_change_during_read_cannot_return_stale_text(api, env, monkeypatch):
    setup(api, env, count=1)
    original = storage.LedgerSnapshotStore.projection_rows

    async def change(self, receipt):
        await original(self, receipt)
        env.APP_DB.connection.execute("UPDATE cf_memories SET content='changed' WHERE id='memory-0'")

    monkeypatch.setattr(storage.LedgerSnapshotStore, 'projection_rows', change)
    response = read(api, PROMPT)
    assert response.status_code == 503 and response.headers['cache-control'] == 'no-store'
    assert 'Previously lived' not in response.text and 'changed' not in response.text


def test_mirror_keeps_both_closed_lineage_alias_reasons(api, env):
    seed(api, env, identity='current', status='active', meta={'valid_to': None})
    seed(api, env, identity='prior', superseded_by='current', meta={'canonical_memory_id': 'current'})
    publish_fixture(env)
    result = read(api, MIRROR).json()
    assert result['failure_reason'] is None and result['final_page']
    assert {row['memory_id'] for row in result['rows']} == {'current', 'prior'}
    assert {alias['reason'] for alias in result['aliases']} == {'canonical_memory_id', 'superseded_by'}
    assert all(
        alias['alias_memory_id'] == 'prior' and alias['canonical_memory_id'] == 'current' for alias in result['aliases']
    )


@pytest.mark.parametrize('corrupt', ['metadata', 'alias', 'generation', 'unscrubbed_delete'])
def test_invalid_mirror_row_discards_entire_page(api, env, corrupt):
    setup(api, env)
    updates = {
        'metadata': "canonical_metadata_json=json_set(canonical_metadata_json,'$.uid','forged')",
        'alias': "canonical_metadata_json=json_set(canonical_metadata_json,'$.canonical_memory_id','foreign/path')",
        'generation': 'account_generation=9',
        'unscrubbed_delete': 'deleted_at=1',
    }
    # Save and restore only the controlled proof; a real privacy write removes it.
    proof = tuple(env.APP_DB.connection.execute('SELECT * FROM cf_knowledge_ledger_snapshots').fetchone())
    env.APP_DB.connection.execute('UPDATE cf_memories SET ' + updates[corrupt] + " WHERE id='memory-0'")
    env.APP_DB.connection.execute('INSERT OR REPLACE INTO cf_knowledge_ledger_snapshots VALUES (?,?,?)', proof)
    result = read(api, MIRROR).json()
    assert result['failure_reason'] is not None and result['rows'] == [] and not result['final_page']


def test_mirror_does_not_truncate_a_500_row_page(api, env):
    setup(api, env, count=1)
    row = dict(env.APP_DB.row('memory-0'))
    columns = list(row)
    statement = 'INSERT INTO cf_memories (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
    env.APP_DB.connection.executemany(statement, [tuple({**row, 'id': f'row-{i:04d}'}.values()) for i in range(501)])
    publish_fixture(env)
    first = read(api, MIRROR + '?page_size=500').json()
    assert first['failure_reason'] is None and len(first['rows']) == 500 and not first['final_page']
    final = read(api, MIRROR + '?page_size=500&cursor=' + first['next_cursor']).json()
    assert final['failure_reason'] is None and len(final['rows']) == 2 and final['final_page']
    assert final['scanned_count'] == 502 and final['projected_count'] == 502
    assert len({row['memory_id'] for row in first['rows'] + final['rows']}) == 502


def test_missing_and_expired_cursor_authority_never_returns_a_complete_page(api, env, monkeypatch):
    import jit_ledger_snapshot_kernel as kernel

    setup(api, env)
    first = read(api, MIRROR + '?page_size=1').json()
    monkeypatch.setattr(kernel.time, 'time', lambda: 10_000_000_000)
    expired = read(api, MIRROR + '?cursor=' + first['next_cursor']).json()
    assert expired['failure_reason'] == 'invalid_cursor' and not expired['final_page']
    env.MEMORY_V3_CURSOR_SECRET = ''
    missing = read(api, MIRROR).json()
    assert missing['failure_reason'] == 'invalid_cursor' and missing['rows'] == [] and not missing['final_page']


def test_sql_bounds_oversized_source_before_transferring_it_to_python(api, env):
    setup(api, env, count=1)
    env.APP_DB.connection.execute(
        "UPDATE cf_memories SET canonical_metadata_json=json_set(canonical_metadata_json,'$.body',?) WHERE id='memory-0'",
        ('x' * 1_000_001,),
    )
    for statement, values in [
        (storage.PAGE, ('owner', None, None, 200)),
        (storage.PROJECTION, ('owner', '["memory-0"]')),
    ]:
        row = env.APP_DB.connection.execute(statement, values).fetchone()
        assert row['id'] == 'memory-0' and row['document'] is None
    prompt = read(api, PROMPT).json()
    assert prompt['mode'] == 'compatibility' and prompt['rows'] == []
    mirror = read(api, MIRROR).json()
    assert mirror['failure_reason'] is not None and mirror['rows'] == [] and not mirror['final_page']
