"""Canonical preparation + complete D1 schema; no provider erasure claim."""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_apply_edit import read_item
from memory_privacy_apply import lineage_ids, prepare_privacy_deletion
from memory_privacy_plan import privacy_lineage_ids
from test_memory_apply_intake import apply, database, rows, state  # noqa: F401
from test_memory_privacy_receipts import SECRET, receipt


def prepare(database, ids=('memory-0',), uid='owner'):
    return asyncio.run(
        prepare_privacy_deletion(
            SimpleNamespace(APP_DB=database, MEMORY_PRIVACY_SECRET=SECRET),
            uid,
            list(ids),
            int(time.time()),
        )
    )


def full_state(database):
    return {
        **state(database),
        **{
            table: [dict(row) for row in database.connection.execute('SELECT * FROM ' + table)]
            for table in (
                'cf_memory_privacy_deletions',
                'cf_memory_privacy_receipts',
                'cf_memory_privacy_apply_guard',
                'cf_destructive_operation_gates',
            )
        },
    }


def link(database, memory_id, *, canonical=None, superseded=None):
    database.connection.execute(
        "UPDATE cf_memories SET canonical_metadata_json = json_set(canonical_metadata_json, '$.canonical_memory_id', ?), "
        "superseded_by = ? WHERE uid = 'owner' AND id = ?",
        (canonical, superseded, memory_id),
    )


def test_preparation_scrubs_complete_lineage_and_seals_in_one_transaction(database):
    apply(database, rows())
    apply(database, [{**rows()[0], 'id': 'retained'}])
    link(database, 'memory-1', canonical='memory-0')
    database.connection.execute(
        "UPDATE cf_memories SET headline='secret headline', tags_json='[\"private tag\"]', "
        "scoring='private score', conversation_id='private source', app_id='private app' WHERE id = 'memory-0'"
    )
    old = full_state(database)
    inventory = prepare(database)
    snapshot = full_state(database)
    assert {r['id'] for r in json.loads(inventory['targets_json'])} == {'memory-0', 'memory-1'}
    assert snapshot['cf_memory_privacy_apply_guard'] == []
    assert len(snapshot['cf_memory_privacy_receipts']) == 2
    assert len(snapshot['cf_memory_privacy_deletions']) == 1
    assert len(snapshot['cf_memory_operations']) == len(old['cf_memory_operations']) + 1
    current = snapshot['cf_memory_apply_control'][0]
    assert current['commit_sequence'] == old['cf_memory_apply_control'][0]['commit_sequence'] + 1
    deletion_commit = next(
        row for row in snapshot['cf_memory_commits'] if row['commit_id'] == current['head_commit_id']
    )
    assert deletion_commit['parent_commit_id'] == current['head_commit_id']
    operation = next(
        json.loads(row['operation_json'])
        for row in snapshot['cf_memory_operations']
        if row['operation_id'] == deletion_commit['operation_id']
    )
    assert operation['observed_head_commit_id'] is None
    for memory_id in ('memory-0', 'memory-1'):
        stored = database.row(memory_id)
        item = read_item(stored)
        assert item.content is None and item.content_hash is None and item.promotion is None
        assert item.status.value == item.source_state.value == 'tombstoned'
        assert all(e.source_id is None and e.content_hash is None for e in item.evidence)
        assert stored['privacy_receipt_id'] == receipt(memory_id=memory_id)
        for field in ('headline', 'conversation_id', 'app_id', 'scoring'):
            assert stored[field] is None
        old_row = next(row for row in old['cf_memories'] if row['id'] == memory_id)
        assert stored['item_revision'] == old_row['item_revision'] + 1
        vector_work = next(row for row in snapshot['cf_vector_projection_outbox'] if row['source_id'] == memory_id)
        assert vector_work['operation'] == 'delete' and vector_work['desired_version'] == item.item_revision
        with pytest.raises(sqlite3.IntegrityError, match='memory_privacy_deleted|memory_privacy_cleanup_pending'):
            database.connection.execute("UPDATE cf_memories SET content = 'late result' WHERE id = ?", (memory_id,))
    assert database.row('retained') == next(row for row in old['cf_memories'] if row['id'] == 'retained')
    assert any('Native evidence' in row['operation_json'] for row in snapshot['cf_memory_operations'])


def test_retry_reuses_inventory_without_another_commit_or_receipt(database):
    apply(database, rows())
    first = prepare(database)
    snapshot = full_state(database)
    assert prepare(database) == first
    after = full_state(database)
    assert (
        after['cf_destructive_operation_gates'][0]['started_at']
        >= snapshot['cf_destructive_operation_gates'][0]['started_at']
    )
    snapshot['cf_destructive_operation_gates'][0]['started_at'] = after['cf_destructive_operation_gates'][0][
        'started_at'
    ]
    assert after == snapshot
    with pytest.raises(ValueError, match='cleanup_pending'):
        prepare(database, ['memory-1'])
    assert full_state(database) == after


def test_legacy_principal_and_payment_locked_item_can_delete_but_cannot_bypass_normal_edit(database):
    database.connection.execute(
        "INSERT INTO cf_memories(uid,id,content,memory_tier,valid_at,created_at,updated_at,is_locked) "
        "VALUES ('owner','legacy','private','short_term',unixepoch(),unixepoch(),unixepoch(),1)"
    )
    with pytest.raises(sqlite3.IntegrityError, match='locked_for_mutation'):
        database.connection.execute(
            "UPDATE cf_memories SET content=NULL,status='tombstoned',source_state='tombstoned',deleted_at=1"
        )
    assert prepare(database, ['legacy'])
    assert database.row('legacy')['content'] is None


@pytest.mark.parametrize('mode', ['transitioning_to_ledger', 'transitioning_to_compatibility'])
def test_writer_pause_does_not_block_privacy_preparation(database, mode):
    apply(database, rows())
    database.connection.execute(
        "UPDATE cf_memory_apply_control SET control_json = json_set(control_json, '$.writer_mode', ?, "
        "'$.writer_transition_owner', 'migration-owner')",
        (mode,),
    )
    prepare(database)
    assert database.row('memory-0')['content'] is None


def test_active_hold_and_competing_gate_block_before_any_write(database):
    apply(database, rows())
    db = database.connection
    db.execute("INSERT INTO cf_legal_holds VALUES ('owner','legal_hold.v1','admin',1,unixepoch())")
    before = full_state(database)
    with pytest.raises(sqlite3.IntegrityError, match='legal_hold_active'):
        prepare(database)
    assert full_state(database) == before
    db.execute("DELETE FROM cf_legal_holds")
    db.execute(
        "INSERT INTO cf_destructive_operation_gates VALUES ('owner','other',?,'running',unixepoch(),NULL)", ('b' * 64,)
    )
    before = full_state(database)
    with pytest.raises(sqlite3.IntegrityError, match='destructive_operation_in_progress'):
        prepare(database)
    assert full_state(database) == before
    db.execute("UPDATE cf_destructive_operation_gates SET started_at=unixepoch()-21601")
    prepare(database)
    with pytest.raises(sqlite3.IntegrityError, match='destructive_operation_in_progress'):
        db.execute("INSERT INTO cf_legal_holds VALUES ('owner','legal_hold.v1','admin',1,unixepoch())")


@pytest.mark.parametrize(
    'mutation',
    [
        "UPDATE cf_memories SET content='concurrent edit' WHERE id='memory-0'",
        "UPDATE cf_memories SET canonical_metadata_json=json_set(canonical_metadata_json,'$.canonical_memory_id','elsewhere') WHERE id='memory-0'",
        "UPDATE cf_memory_apply_control SET control_json=json_set(control_json,'$.source_generation',1),source_generation=1",
        "INSERT INTO cf_memories(uid,id,content,memory_tier,valid_at,created_at,updated_at,superseded_by) VALUES ('owner','new-alias','incoming','short_term',1,1,1,'memory-0')",
        "INSERT INTO cf_legal_holds VALUES ('owner','legal_hold.v1','admin',1,unixepoch())",
    ],
)
def test_concurrent_authority_is_checked_inside_the_batch(database, mutation):
    apply(database, rows())
    captured = {}

    def change():
        database.connection.execute(mutation)
        captured.update(full_state(database))

    database.before_write = change
    with pytest.raises(sqlite3.IntegrityError):
        prepare(database)
    assert full_state(database) == captured


def test_late_receipt_failure_rolls_back_scrub_journal_gate_and_inventory(database):
    apply(database, rows())
    database.connection.execute(
        "CREATE TRIGGER fail_privacy_receipt BEFORE INSERT ON cf_memory_privacy_receipts "
        "BEGIN SELECT RAISE(ABORT, 'receipt unavailable'); END"
    )
    before = full_state(database)
    with pytest.raises(sqlite3.IntegrityError, match='receipt unavailable'):
        prepare(database)
    assert full_state(database) == before


@pytest.mark.parametrize(
    'pointers',
    [
        [('memory-1', None), ('memory-2', None), (None, None)],
        [('missing', None), ('missing', None), (None, None)],
        [('memory-1', None), ('memory-0', None), ('memory-1', None)],
        [('memory-1', None), ('\u2003\tmemory-2\u3000', None), (None, None)],
        [('   ', 'memory-1'), (None, None), ('', 'memory-1')],
    ],
)
def test_sql_component_matches_original_lineage_rules(database, pointers):
    values = [{**rows()[0], 'id': f'memory-{index}'} for index in range(3)]
    apply(database, values)
    for index, (canonical, superseded) in enumerate(pointers):
        link(database, f'memory-{index}', canonical=canonical, superseded=superseded)
    items = [read_item(database.row(value['id'])) for value in values]
    for value in values:
        assert asyncio.run(lineage_ids(database, 'owner', [value['id']])) == privacy_lineage_ids(
            'owner', [value['id']], items
        )
    with pytest.raises(ValueError, match='not_found'):
        asyncio.run(lineage_ids(database, 'other', ['memory-0']))


def test_retry_cannot_reopen_a_gate_after_concurrent_finalization(database):
    apply(database, rows())
    prepare(database)

    def finalize():
        database.connection.execute("DELETE FROM cf_memory_privacy_deletions WHERE uid='owner'")
        database.connection.execute(
            "UPDATE cf_destructive_operation_gates SET state='completed',finished_at=unixepoch()"
        )

    database.before_write = finalize
    with pytest.raises(ValueError, match='inventory_changed'):
        prepare(database)
    assert database.connection.execute('SELECT state FROM cf_destructive_operation_gates').fetchone()[0] == 'completed'


@pytest.mark.parametrize('timing', ['before_read', 'before_write'])
def test_account_deletion_fence_blocks_privacy_preparation(database, timing):
    apply(database, rows())

    def delete_account():
        database.connection.execute("INSERT INTO cf_account_deletion_tombstones VALUES ('owner',1,2)")

    if timing == 'before_read':
        delete_account()
    else:
        database.before_write = delete_account
    before = full_state(database)
    with pytest.raises((ValueError, sqlite3.IntegrityError), match='account_deleted|account deletion fence'):
        prepare(database)
    assert full_state(database) == before
