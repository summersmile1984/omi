"""ANN publication lag, durable continuation and canonical source races."""

import asyncio
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from memory_apply_item import read_item
from memory_vector_readiness import ConsolidationIndexPending, ensure_memory_index_ready
from test_memory_consolidation_context import services, long_term, project, invoke_context
from test_memory_consolidation_apply import decision
from test_memory_mutation_lock import target
from test_memory_review_routes import journal


def test_indexing_wait_never_calls_ai_or_spends_a_business_decision_then_resumes(target):
    database, request, create = target
    old = long_term(database, create, 'I drink jasmine tea every morning')
    project(database, old, 'a' * 64)
    source = create(content='I drink jasmine tea every morning', subject_entity_id='user', subject_attribution='user')
    output = decision(read_item(database.row(source)), 'archive', reconciliation='duplicate', target_memory_id=old)
    env = services(database, output={'decisions': [output.model_dump(mode='json')]})
    env.MEMORY_VECTORS.matches = [{'id': 'a' * 64, 'score': 1.0}]
    env.MEMORY_VECTORS.visible = False
    before = journal(database)
    for _ in range(2):
        with pytest.raises(ConsolidationIndexPending):
            asyncio.run(invoke_context(env, 'owner', [source], run_id='waiting-run'))
    assert env.AI.calls == [] and journal(database) == before
    assert read_item(database.row(source)).processing_state.value == 'pending'
    env.MEMORY_VECTORS.visible = True
    result = asyncio.run(invoke_context(env, 'owner', [source], run_id='waiting-run'))
    assert result[source].promotion['route'] == 'archive'
    assert len(env.AI.calls) == 2
    assert [row['id'] for row in request('GET', '/v3/memories').json()] == [old]
    vector_id, options = env.MEMORY_VECTORS.readiness_calls[-1]
    assert vector_id == 'a' * 64 and options['filter'] == {'publication_id': vector_id}
    assert options['topK'] == 1 and options['returnValues'] is False


@pytest.mark.parametrize('shape', ['missing', 'legacy', 'partial'])
def test_incomplete_publication_uses_existing_outbox_and_preserves_backoff(target, shape):
    database, _, create = target
    old = long_term(database, create, 'An existing eligible memory')
    if shape != 'missing':
        project(database, old, 'a' * 64, publication_size=0 if shape == 'legacy' else 2)
    source = create(content='A pending observation')
    env = services(database)
    database.connection.execute('DELETE FROM cf_vector_projection_outbox WHERE source_id = ?', (old,))
    with pytest.raises(ConsolidationIndexPending):
        asyncio.run(ensure_memory_index_ready(env, 'owner', [source]))
    outbox = database.connection.execute(
        'SELECT * FROM cf_vector_projection_outbox WHERE source_id = ?', (old,)
    ).fetchone()
    assert outbox['operation'] == 'upsert' and outbox['desired_version'] == database.row(old)['item_revision']
    database.connection.execute(
        'UPDATE cf_vector_projection_outbox SET attempts=3, next_attempt_at=9999999999 WHERE source_id=?', (old,)
    )
    with pytest.raises(ConsolidationIndexPending):
        asyncio.run(ensure_memory_index_ready(env, 'owner', [source]))
    row = database.connection.execute(
        'SELECT attempts, next_attempt_at FROM cf_vector_projection_outbox WHERE source_id = ?', (old,)
    ).fetchone()
    assert tuple(row) == (3, 9999999999)
    assert env.AI.calls == [] and env.MEMORY_VECTORS.readiness_calls == []


def test_bounded_query_proofs_continue_across_invocations(target):
    database, _, create = target
    old = long_term(database, create, 'A long eligible source split across many vectors')
    ids = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(21)]
    for vector_id in ids:
        project(database, old, vector_id, publication_size=21)
    database.connection.execute("UPDATE cf_memory_vector_artifacts SET attempt_id='one-publication'")
    source = create(content='A pending observation')
    env = services(database)
    with pytest.raises(ConsolidationIndexPending):
        asyncio.run(ensure_memory_index_ready(env, 'owner', [source]))
    assert len(env.MEMORY_VECTORS.readiness_calls) == 20
    assert database.connection.execute('SELECT SUM(query_ready) FROM cf_memory_vector_artifacts').fetchone()[0] == 20
    asyncio.run(ensure_memory_index_ready(env, 'owner', [source]))
    assert len(env.MEMORY_VECTORS.readiness_calls) == 21
    asyncio.run(ensure_memory_index_ready(env, 'owner', [source]))
    assert len(env.MEMORY_VECTORS.readiness_calls) == 21


def test_mapping_change_during_retrieval_invalidates_even_a_completed_replacement(target):
    database, _, create = target
    old = long_term(database, create, 'An existing memory')
    project(database, old, 'a' * 64)
    source = create(content='A pending observation')
    env = services(database)

    async def replace():
        database.connection.execute('DELETE FROM cf_vector_projection_state WHERE vector_id = ?', ('a' * 64,))
        project(database, old, 'b' * 64)
        database.connection.execute(
            'UPDATE cf_memory_vector_artifacts SET query_ready=1 WHERE vector_id=?', ('b' * 64,)
        )

    env.AI.on_embedding = replace
    env.MEMORY_VECTORS.matches = []
    before = journal(database)
    with pytest.raises(ConsolidationIndexPending, match='index_changed'):
        asyncio.run(invoke_context(env, 'owner', [source], run_id='replacement'))
    before['cf_memory_apply_control'][0]['projection_sequence'] += 2
    assert len(env.AI.calls) == 1 and journal(database) == before


@pytest.mark.parametrize('fault', ['error', 'wrong_id', 'nan', 'retracted'])
def test_invalid_or_revoked_query_proof_keeps_source_pending(target, fault):
    database, _, create = target
    old = long_term(database, create, 'An existing memory')
    project(database, old, 'a' * 64)
    source = create(content='A pending observation')
    env = services(database)

    async def query(vector_id, options):
        if fault == 'error':
            raise RuntimeError('synthetic dependency outage')
        if fault == 'retracted':
            database.connection.execute('DELETE FROM cf_vector_projection_state WHERE vector_id = ?', (vector_id,))
        return {
            'matches': [
                {'id': 'b' * 64 if fault == 'wrong_id' else vector_id, 'score': float('nan') if fault == 'nan' else 1.0}
            ]
        }

    env.MEMORY_VECTORS.queryById = query
    before = journal(database)
    with pytest.raises(ConsolidationIndexPending):
        asyncio.run(invoke_context(env, 'owner', [source], run_id='invalid-proof'))
    if fault == 'retracted':
        before['cf_memory_apply_control'][0]['projection_sequence'] += 1
    assert env.AI.calls == [] and journal(database) == before
    assert (
        database.connection.execute(
            'SELECT query_ready FROM cf_memory_vector_artifacts WHERE vector_id=?', ('a' * 64,)
        ).fetchone()[0]
        == 0
    )
