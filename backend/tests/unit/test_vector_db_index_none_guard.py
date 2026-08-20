"""Projection writes stay best-effort, but retrieval never equates unavailable with no hits.

database.vector_db.index is None when no vector provider is configured. Rebuildable writes may
skip and repair later, while memory/task/conversation reads raise a typed unavailable error so a
caller cannot tell the model the user has no data.
"""

import pytest

import database.vector_db as vector_db
from database.vector_projection import ProjectionUnavailableError


def test_query_vectors_by_metadata_returns_typed_unavailable_without_index(monkeypatch):
    monkeypatch.setattr(vector_db, 'index', None)
    with pytest.raises(ProjectionUnavailableError) as error:
        vector_db.query_vectors_by_metadata('uid1', [0.1, 0.2], [], [], [], [], [], limit=5)
    assert error.value.capability == 'conversation_search'


@pytest.mark.parametrize(
    ('operation', 'capability'),
    [
        (lambda: vector_db.query_vectors('query', 'uid1', query_vector=[0.1, 0.2]), 'conversation_search'),
        (lambda: vector_db.find_similar_memories('uid1', 'query'), 'memory_search'),
        (lambda: vector_db.search_memories_by_vector('uid1', 'query'), 'memory_search'),
        (lambda: vector_db.query_memory_vector_candidates('uid1', 'query'), 'memory_search'),
        (lambda: vector_db.search_action_items_by_vector('uid1', 'query'), 'task_search'),
        (lambda: vector_db.find_similar_action_items('uid1', 'query'), 'task_search'),
        (lambda: vector_db.query_workstream_association_candidates('uid1', 'query'), 'task_search'),
        (lambda: vector_db.search_transcript_chunks('uid1', 'query'), 'conversation_search'),
    ],
)
def test_projection_reads_never_return_silent_empty_when_unavailable(monkeypatch, operation, capability):
    monkeypatch.setattr(vector_db, 'index', None)

    with pytest.raises(ProjectionUnavailableError) as error:
        operation()

    assert error.value.as_dict() == {
        'code': 'projection_unavailable',
        'capability': capability,
        'reason': 'vector store is not configured',
        'retryable': True,
    }


def test_upsert_vector2_is_a_noop_without_index(monkeypatch):
    monkeypatch.setattr(vector_db, 'index', None)
    # Must not raise (previously AttributeError on index.upsert).
    assert vector_db.upsert_vector2('uid1', 'conv1', [0.1, 0.2], {'k': 'v'}) is None


def test_update_vector_metadata_returns_empty_without_index(monkeypatch):
    monkeypatch.setattr(vector_db, 'index', None)
    result = vector_db.update_vector_metadata('uid1', 'conv1', {'k': 'v'})
    assert result == {}
