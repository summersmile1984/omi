"""INV-MEM-4: caller metadata cannot admit new memory directly to Long-term.

These public handlers execute the complete D1 migration chain. Authentication
and AI category IO are controlled; persistence, lifecycle defaults and the
revision/outbox transaction run their production implementation.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from test_memory_mutation_lock import developer, mcp, target  # noqa: F401
from memory_external_intake import create_external_memories
from memory_kernel_intake import document_id_from_seed
from memory_routes import MemoryCreate, _batch_row

INTAKE_PATHS = [
    '/v3/memories',
    '/v3/memories/batch',
    '/v1/mcp/memories',
    '/v1/dev/user/memories',
    '/v1/dev/user/memories/batch',
]


def payload(path):
    memory = {
        'content': 'The user prefers jasmine tea.',
        'category': 'manual',
        'durability': 'long_term',
        'memory_tier': 'long_term',
        'tier': 'long_term',
    }
    return {'memories': [memory]} if path.endswith('/batch') else memory


@pytest.fixture
def category_io(monkeypatch):
    async def category(env, content):
        return 'interesting'

    monkeypatch.setattr(mcp, '_memory_category', category)
    monkeypatch.setattr(developer, '_memory_category', category)


@pytest.mark.parametrize('path', INTAKE_PATHS)
def test_all_explicit_intake_starts_short_term_with_atomic_projection(target, category_io, path):
    database, request, _ = target
    response = request('POST', path, body=payload(path))
    assert response.status_code == 200, response.text
    rows = database.connection.execute('SELECT * FROM cf_memories').fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row['memory_tier'] == 'short_term'
    assert row['expires_at'] == row['captured_at'] + 48 * 60 * 60
    assert row['status'] == 'active'
    outbox = database.connection.execute(
        "SELECT * FROM cf_vector_projection_outbox WHERE uid = ? AND source_kind = 'memory' AND source_id = ?",
        (row['uid'], row['id']),
    ).fetchone()
    # Original authority: backend/utils/memory/required_promotion.py and
    # MemoryService.create_external_memory/create_external_memory_batch call
    # required_processing_payload for native, MCP and Developer submissions.
    assert row['processing_state'] == 'pending'
    assert outbox['operation'] == 'delete'
    assert outbox['desired_version'] == row['item_revision']
    metadata = json.loads(row['canonical_metadata_json'])
    assert metadata['promotion']['required'] is True
    assert metadata['promotion']['processing_status'] == 'pending_processing'
    head = database.connection.execute('SELECT * FROM cf_memory_apply_control').fetchone()
    assert head['account_generation'] == 0  # Already-shipped/unmigrated principal.
    assert head['commit_sequence'] == 1 and head['head_commit_id'] == metadata['ledger_commit_id']
    for table in ('cf_memory_commits', 'cf_memory_operations'):
        assert database.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 1
    assert database.connection.execute('SELECT COUNT(*) FROM cf_memory_outbox').fetchone()[0] == 2


@pytest.mark.parametrize('path', INTAKE_PATHS)
def test_failed_intake_never_publishes_or_records_usage(target, category_io, path):
    database, request, _ = target
    # A failure after admission but inside the transaction must roll back every
    # side effect, including lifecycle/revision triggers and usage.
    database.connection.executescript(
        "CREATE TRIGGER fail_intake_usage BEFORE INSERT ON cf_usage_sources "
        "BEGIN SELECT RAISE(ABORT, 'controlled transaction failure'); END;"
    )
    response = request('POST', path, body=payload(path))
    assert response.status_code == 503
    assert 'controlled transaction failure' not in response.text
    assert database.connection.execute('SELECT count(*) FROM cf_memories').fetchone()[0] == 0
    assert database.side_effects() == {
        'published': [],
        'cf_memory_review_queue': [],
        'cf_vector_projection_outbox': [],
        'cf_usage_sources': [],
    }


@pytest.mark.parametrize('path', ['/v1/mcp/memories', '/v1/dev/user/memories'])
def test_external_duplicate_intake_preserves_existing_canonical_tier(target, category_io, path):
    database, request, _ = target
    assert request('POST', path, body=payload(path)).status_code == 200
    # This represents an already-admitted historical Long-term row. New intake
    # must neither promote a short-term row nor demote existing canonical state.
    database.connection.execute(
        "UPDATE cf_memories SET memory_tier = 'long_term', processing_state='processed', expires_at = NULL"
    )
    assert request('POST', path, body=payload(path)).status_code == 200
    rows = database.connection.execute('SELECT * FROM cf_memories').fetchall()
    assert len(rows) == 1
    assert rows[0]['memory_tier'] == 'long_term'
    assert rows[0]['expires_at'] is None
    assert database.connection.execute('SELECT COUNT(*) FROM cf_memory_commits').fetchone()[0] == 1


def test_developer_combined_update_uses_one_canonical_commit(target, category_io):
    database, request, _ = target
    created = request('POST', '/v1/dev/user/memories', body=payload('/v1/dev/user/memories')).json()
    response = request(
        'PATCH',
        '/v1/dev/user/memories/' + created['id'],
        body={
            'content': 'I prefer oolong tea.',
            'visibility': 'public',
            'category': 'interesting',
            'tags': ['tea'],
        },
    )
    assert response.status_code == 200, response.text
    row = database.row(created['id'])
    metadata = json.loads(row['canonical_metadata_json'])
    assert row['content'] == 'I prefer oolong tea.' and row['visibility'] == 'public'
    assert metadata['promotion']['tags'] == ['tea'] and metadata['promotion']['category'] == 'interesting'
    assert row['processing_state'] == 'pending' and row['memory_tier'] == 'short_term'
    assert database.connection.execute('SELECT commit_sequence FROM cf_memory_apply_control').fetchone()[0] == 2
    assert database.connection.execute('SELECT COUNT(*) FROM cf_memory_operations').fetchone()[0] == 2


def test_developer_null_update_is_rejected_before_mutation(target, category_io):
    database, request, _ = target
    created = request('POST', '/v1/dev/user/memories', body=payload('/v1/dev/user/memories')).json()
    before = database.row(created['id'])
    assert request('PATCH', '/v1/dev/user/memories/' + created['id'], body={'content': None}).status_code == 422
    assert database.row(created['id']) == before


@pytest.mark.parametrize('path', ['/v1/mcp/memories', '/v1/dev/user/memories'])
def test_deleted_external_submission_reissues_identity_without_restoring_tombstone(target, category_io, path):
    database, request, _ = target
    assert request('POST', path, body=payload(path)).status_code == 200
    original = dict(database.connection.execute('SELECT * FROM cf_memories').fetchone())
    assert request('DELETE', path + '/' + original['id']).status_code == 200
    assert request('POST', path, body=payload(path)).status_code == 200
    current = dict(database.connection.execute('SELECT * FROM cf_memories').fetchone())
    assert current['id'] != original['id'] and current['content'] == original['content']
    assert current['processing_state'] == 'pending'
    assert (
        json.loads(current['evidence_json'])[0]['evidence_id']
        != json.loads(original['evidence_json'])[0]['evidence_id']
    )
    assert database.connection.execute('SELECT COUNT(*) FROM cf_memory_privacy_receipts').fetchone()[0] == 1


def test_duplicate_and_new_batch_recheck_observed_source_before_any_write(target, category_io):
    database, request, _ = target
    path = '/v1/dev/user/memories'
    created = request('POST', path, body=payload(path)).json()

    def hide():
        database.connection.execute("UPDATE cf_memories SET status='hidden' WHERE id=?", (created['id'],))

    database.before_write = hide
    response = request(
        'POST', path + '/batch', body={'memories': [payload(path), {'content': 'A separate new memory.'}]}
    )
    assert response.status_code == 503
    assert database.connection.execute('SELECT COUNT(*) FROM cf_memories').fetchone()[0] == 1
    assert database.connection.execute('SELECT COUNT(*) FROM cf_memory_commits').fetchone()[0] == 1
    assert database.connection.execute('SELECT COUNT(*) FROM cf_usage_sources').fetchone()[0] == 1
    assert database.connection.execute('SELECT COUNT(*) FROM cf_memory_apply_guard').fetchone()[0] == 0


def test_concurrent_identical_external_requests_commit_once(target):
    database, _, _ = target
    env = SimpleNamespace(APP_DB=database, MEMORY_PRIVACY_SECRET='memory-privacy-tests-secret-32-bytes')
    content = 'Concurrent external submission.'
    row = _batch_row(
        'owner', document_id_from_seed(content), MemoryCreate(content=content, category='manual'), int(time.time())
    )
    original = database.batch

    async def race():
        entered = 0
        ready = asyncio.Event()

        async def batch(statements):
            nonlocal entered
            entered += 1
            if entered <= 2:
                if entered == 2:
                    ready.set()
                await ready.wait()
            return await original(statements)

        database.batch = batch
        return await asyncio.wait_for(
            asyncio.gather(*[create_external_memories(env, 'owner', [row], source_surface='mcp') for _ in range(2)]),
            timeout=5,
        )

    results = asyncio.run(race())
    assert results[0][0]['id'] == results[1][0]['id'] == row['id']
    for table in ('cf_memories', 'cf_memory_commits', 'cf_memory_operations', 'cf_usage_sources'):
        assert database.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 1
