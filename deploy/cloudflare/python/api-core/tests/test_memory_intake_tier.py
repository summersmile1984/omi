"""INV-MEM-4: caller metadata cannot admit new memory directly to Long-term.

These public handlers execute the complete D1 migration chain. Authentication
and AI category IO are controlled; persistence, lifecycle defaults and the
revision/outbox transaction run their production implementation.
"""

import pytest

from test_memory_mutation_lock import developer, mcp, target  # noqa: F401

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
    assert outbox['operation'] == 'upsert'
    assert outbox['desired_version'] == row['item_revision']


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


def test_mcp_duplicate_intake_preserves_existing_canonical_tier(target, category_io):
    database, request, _ = target
    path = '/v1/mcp/memories'
    assert request('POST', path, body=payload(path)).status_code == 200
    # This represents an already-admitted historical Long-term row. New intake
    # must neither promote a short-term row nor demote existing canonical state.
    database.connection.execute("UPDATE cf_memories SET memory_tier = 'long_term', expires_at = NULL")
    assert request('POST', path, body=payload(path)).status_code == 200
    rows = database.connection.execute('SELECT * FROM cf_memories').fetchall()
    assert len(rows) == 1
    assert rows[0]['memory_tier'] == 'long_term'
    assert rows[0]['expires_at'] is None
