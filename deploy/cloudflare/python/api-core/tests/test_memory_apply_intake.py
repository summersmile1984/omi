"""Native HTTP intake and its D1 transaction, including concurrent admission.

The shared full-migration SQLite fixture enforces the documented D1 batch
rollback contract: https://developers.cloudflare.com/d1/worker-api/d1-database/#batch
Hosted D1 execution is qualified separately; these tests perform no network IO.
"""

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_apply_intake import MODEL_COLUMNS, create_native_memories
from memory_kernel_item import MemoryItem
from memory_kernel_operations import MemoryOperation
from memory_routes import MemoryCreate, _batch_row
from test_memory_mutation_lock import Database, target as target
from test_memory_routes import FakeRequest as MemoryRequest
from memory_routes import create_memories_batch
from test_user_export_routes import FakeAuth, FakeRequest, signed_headers
from user_export_routes import export_user_data

TABLES = (
    'cf_memories',
    'cf_memory_operations',
    'cf_memory_commits',
    'cf_memory_outbox',
    'cf_memory_apply_control',
    'cf_memory_apply_guard',
    'cf_memory_review_queue',
    'cf_vector_projection_outbox',
    'cf_usage_sources',
)


def state(database):
    return {table: [dict(row) for row in database.connection.execute(f'SELECT * FROM {table}')] for table in TABLES}


def rows(uid='owner', content='Native evidence'):
    now = int(time.time())
    return [
        _batch_row(uid, f'memory-{index}', MemoryCreate(content=f'{content} {index}', category='manual'), now)
        for index in range(2)
    ]


def apply(database, values, extra=()):
    return asyncio.run(
        create_native_memories(
            SimpleNamespace(
                APP_DB=database,
                MEMORY_PRIVACY_SECRET='memory-privacy-tests-secret-32-bytes',
            ),
            values[0]['uid'],
            values,
            list(extra),
        )
    )


@pytest.fixture
def database():
    value = Database()
    yield value
    value.connection.close()


def test_http_batch_commits_items_receipts_head_evidence_and_outbox_together(target):
    database, request, _ = target
    response = request(
        'POST',
        '/v3/memories/batch',
        body={
            'memories': [
                {'content': 'I prefer jasmine tea', 'category': 'manual'},
                {'content': 'Learn Japanese', 'category': 'interesting'},
            ]
        },
    )
    assert response.status_code == 200, response.text
    snapshot = state(database)
    assert (
        len(snapshot['cf_memories']) == len(snapshot['cf_memory_operations']) == len(snapshot['cf_memory_commits']) == 2
    )
    control = snapshot['cf_memory_apply_control'][0]
    assert control['account_generation'] == 0  # Already-shipped, unmigrated principal.
    assert control['commit_sequence'] == 2
    assert snapshot['cf_memory_apply_guard'] == []
    commits = sorted(snapshot['cf_memory_commits'], key=lambda row: row['commit_sequence'])
    assert commits[0]['parent_commit_id'].startswith('genesis_')
    assert commits[1]['parent_commit_id'] == commits[0]['commit_id']
    assert control['head_commit_id'] == commits[1]['commit_id']
    for row, commit in zip(snapshot['cf_memories'], commits):
        metadata = json.loads(row['canonical_metadata_json'])
        assert not (set(metadata) & set(MODEL_COLUMNS))
        fields = dict(metadata)
        for field, column in MODEL_COLUMNS.items():
            fields[field] = json.loads(row[column]) if column.endswith('_json') else row[column]
        item = MemoryItem.model_validate(fields)
        assert item.content == row['content']
        assert item.item_revision == 1 and item.account_generation == 0
        assert item.tier.value == 'short_term'
        assert item.source_commit_id == commit['commit_id']
        assert item.evidence[0].source_id == item.memory_id
        assert item.evidence[0].content_hash == hashlib.sha256(item.content.encode()).hexdigest()
        assert int(item.expires_at.timestamp()) == row['valid_at'] + 172800
        receipt = next(
            json.loads(r['operation_json'])
            for r in snapshot['cf_memory_operations']
            if r['operation_id'] == commit['operation_id']
        )
        assert receipt['committed_memory_item_ids'] == [item.memory_id]
        # Upstream validation recomputes the operation ID and logical digest
        # from the complete receipt reconstructed inside D1.
        persisted = MemoryOperation.model_validate(receipt)
        assert persisted.logical_payload.memory_text == item.content
        events = [r for r in snapshot['cf_memory_outbox'] if r['commit_id'] == commit['commit_id']]
        assert {event['event_type'] for event in events} == {'projection_sync', 'vector_sync'}
        assert all(event['status'] == 'pending' and event['memory_id'] == item.memory_id for event in events)
        assert set(receipt['committed_outbox_event_ids']) == {event['event_id'] for event in events}
    assert len(snapshot['cf_vector_projection_outbox']) == 2
    assert len(snapshot['cf_usage_sources']) == 2


def test_exact_retry_is_noop_but_changed_metadata_or_mixed_retry_cannot_overwrite(database):
    values = rows()
    apply(database, values)
    before = state(database)
    apply(database, values)
    assert state(database) == before
    changed = [{**row, 'tags_json': '["changed"]'} for row in values]
    with pytest.raises(sqlite3.IntegrityError, match='memory_apply_target_exists'):
        apply(database, changed)
    with pytest.raises(ValueError, match='memory_apply_partial_replay'):
        apply(database, [values[0], {**values[1], 'id': 'fresh'}])
    assert state(database) == before


def test_same_target_ids_are_isolated_by_authenticated_uid(database):
    apply(database, rows('owner'))
    apply(database, rows('another-account'))
    snapshot = state(database)
    assert len(snapshot['cf_memories']) == 4
    assert {row['uid'] for row in snapshot['cf_memory_apply_control']} == {'owner', 'another-account'}
    assert len({row['operation_id'] for row in snapshot['cf_memory_operations']}) == 4


@pytest.mark.parametrize('field', ['head_commit_id', 'source_generation', 'account_generation'])
def test_concurrent_authority_change_aborts_the_entire_batch(database, field):
    apply(database, rows())
    values = [{**row, 'id': 'new-' + row['id']} for row in rows()]
    expected = {}

    def advance():
        if field == 'account_generation':
            database.connection.execute(
                "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
            )
        else:
            control = json.loads(state(database)['cf_memory_apply_control'][0]['control_json'])
            control[field] = 'concurrent-head' if field == 'head_commit_id' else control[field] + 1
            database.connection.execute(
                f'UPDATE cf_memory_apply_control SET {field} = ?, control_json = ? WHERE uid = ?',
                (control[field], json.dumps(control), 'owner'),
            )
        expected.update(state(database))

    database.before_write = advance
    with pytest.raises(sqlite3.IntegrityError, match='memory_apply_(head|generation)_changed'):
        apply(database, values)
    assert state(database) == expected


@pytest.mark.parametrize('timing', ['before_read', 'before_write'])
@pytest.mark.parametrize('fence', ['intent', 'tombstone'])
def test_deleted_principal_cannot_bootstrap_or_commit_intake(database, timing, fence):
    def remove():
        if fence == 'intent':
            database.connection.execute(
                "INSERT INTO cf_account_deletion_intents(uid,job_id,status,phase,next_attempt_at,created_at,updated_at) "
                "VALUES ('owner','deletion','pending','quiescing',1,1,1)"
            )
        else:
            database.connection.execute("INSERT INTO cf_account_deletion_tombstones VALUES ('owner',1,2)")

    if timing == 'before_read':
        remove()
    else:
        database.before_write = remove
    before = state(database)
    with pytest.raises(
        (ValueError, sqlite3.IntegrityError), match='memory_apply_account_deleted|account deletion fence'
    ):
        apply(database, rows())
    assert state(database) == before


@pytest.mark.parametrize('table', ['cf_memory_operations', 'cf_memory_outbox', 'cf_usage_sources'])
def test_http_late_write_failure_rolls_back_memory_genesis_and_all_side_effects(target, table):
    database, request, _ = target
    database.connection.execute(
        f"CREATE TRIGGER fail_intake BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'fault'); END"
    )
    before = state(database)
    response = request('POST', '/v3/memories', body={'content': 'Never partially committed', 'category': 'manual'})
    assert response.status_code == 503, response.text
    assert state(database) == before


def test_target_collision_rolls_back_whole_batch(database):
    values = rows()
    apply(database, [values[1]])
    before = state(database)
    with pytest.raises(sqlite3.IntegrityError, match='memory_apply_target_exists'):
        apply(database, [values[0], {**values[1], 'content': 'Changed'}])
    assert state(database) == before


def test_receipt_cannot_commit_without_its_same_transaction_owned_item(database):
    database.connection.execute(
        "CREATE TRIGGER lose_intake_item AFTER INSERT ON cf_memories BEGIN "
        "DELETE FROM cf_memories WHERE uid = NEW.uid AND id = NEW.id; END"
    )
    before = state(database)
    with pytest.raises(sqlite3.OperationalError, match='malformed JSON'):
        apply(database, rows())
    assert state(database) == before


def test_retry_rejects_removed_source_and_stale_generation(database):
    values = rows()
    apply(database, values)
    database.connection.execute("UPDATE cf_memories SET deleted_at = 1 WHERE uid = 'owner' AND id = 'memory-0'")
    with pytest.raises(ValueError, match='memory_apply_replay_source_unavailable'):
        apply(database, values)
    database.connection.execute(
        "INSERT INTO cf_account_cutover(uid,account_generation,updated_at) VALUES ('owner',1,1)"
    )
    before = state(database)
    with pytest.raises(ValueError, match='memory_apply_generation_changed'):
        apply(database, rows())
    assert state(database) == before


def test_export_contains_only_owned_intake_journal(database):
    for uid in ('export-user', 'other-user'):
        apply(database, rows(uid, content=uid + ' private input'))
    env = SimpleNamespace(APP_DB=database, AUTH=FakeAuth(), INTERNAL_ASSERTION_SECRET='export-secret')
    response = asyncio.run(export_user_data(FakeRequest(env, signed_headers('export-secret'))))
    assert response.status_code == 200
    payload = json.loads(response.body)
    ledger = payload['memory_ledger_data']
    assert len(payload['memories']) == len(ledger['memory_operations']) == len(ledger['memory_commits']) == 2
    assert 'memory_apply_control' not in ledger and 'memory_outbox' not in ledger
    assert 'privacy_receipt' not in response.body.decode()
    assert 'other-user' not in response.body.decode()
    assert ledger['memory_commits'][0]['commit_sequence'] == 2
    assert ledger['memory_operations'][0]['operation']['status'] == 'committed'


def test_streamed_batch_preserves_split_utf8_and_rejects_oversize_before_apply(database):
    env = SimpleNamespace(
        APP_DB=database,
        INTERNAL_ASSERTION_SECRET='stream-secret',
        MEMORY_PRIVACY_SECRET='memory-privacy-tests-secret-32-bytes',
    )

    class SplitRequest(MemoryRequest):
        async def stream(self):
            raw = json.dumps({'memories': [{'content': '茶与😀', 'category': 'manual'}]}, ensure_ascii=False).encode()
            for offset in range(0, len(raw), 2):
                yield raw[offset : offset + 2]

    value = asyncio.run(create_memories_batch(SplitRequest(env, signed_headers('stream-secret'))))
    assert value['created_count'] == 1 and value['memories'][0]['content'] == '茶与😀'
    before = state(database)

    class OversizeRequest(MemoryRequest):
        async def stream(self):
            yield b' ' * 1_000_001
            raise AssertionError('oversize input must stop before the next chunk')

    response = asyncio.run(create_memories_batch(OversizeRequest(env, signed_headers('stream-secret'))))
    assert response.status_code == 413
    assert state(database) == before


@pytest.mark.parametrize('character', ['x', '汉', '😀'])
def test_one_megabyte_batch_boundary_is_atomic_and_preserves_content(database, character):
    # The user selected a 1 MB Cloudflare intake limit after the hosted 8 MB
    # Unicode batch exhausted Worker memory. Count UTF-8 bytes, not characters.
    env = SimpleNamespace(
        APP_DB=database,
        INTERNAL_ASSERTION_SECRET='boundary-secret',
        MEMORY_PRIVACY_SECRET='memory-privacy-tests-secret-32-bytes',
    )
    content = character * (9_950 // len(character.encode()))
    payload = {'memories': [{'content': content + str(i), 'category': 'manual'} for i in range(100)]}
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    assert len(raw) < 1_000_000
    exact = raw + b' ' * (1_000_000 - len(raw))

    class BoundaryRequest(MemoryRequest):
        async def stream(self):
            for offset in range(0, len(exact), 16_383):
                yield exact[offset : offset + 16_383]

    value = asyncio.run(create_memories_batch(BoundaryRequest(env, signed_headers('boundary-secret'))))
    assert value['created_count'] == 100
    assert [item['content'] for item in value['memories']] == [item['content'] for item in payload['memories']]
    before = state(database)

    class TooLargeRequest(BoundaryRequest):
        async def stream(self):
            async for chunk in super().stream():
                yield chunk
            yield b' '
            raise AssertionError('request must stop at its first excess byte')

    response = asyncio.run(create_memories_batch(TooLargeRequest(env, signed_headers('boundary-secret'))))
    assert response.status_code == 413
    assert json.loads(response.body) == {'error': 'memory_batch_too_large', 'max_bytes': 1_000_000, 'max_memories': 100}
    assert state(database) == before
