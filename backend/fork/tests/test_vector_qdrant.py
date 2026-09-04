"""Hermetic adapter behavior; pinned Qdrant's wire semantics run in the live probe."""

import json
from unittest import mock

import httpx
import pytest

from fork.vector_qdrant import Config, NAMESPACES, QdrantIndex, VectorStoreUnavailable
from fork.vector_filter import translate


def index(handler):
    return QdrantIndex(Config('http://qdrant', 'synthetic-key', 'test', 3), transport=httpx.MockTransport(handler))


def test_batch_identity_filter_update_and_pagination():
    calls = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith('/search'):
            result = [{'id': 'uuid', 'score': 1, 'payload': {'_omi_id': 'owner-memory', 'metadata': {'uid': 'owner'}}}]
        elif request.url.path.endswith('/scroll'):
            result = {
                'points': [{'payload': {'_omi_id': 'owner-memory'}}, {'payload': {'_omi_id': 'other-memory'}}],
                'next_page_offset': None,
            }
        elif request.method == 'POST' and request.url.path.endswith('/points'):
            result = [{'payload': {'_omi_id': 'owner-memory', 'metadata': {'uid': 'owner', 'tags': ['kept']}}}]
        else:
            result = True
        return httpx.Response(200, json={'status': 'ok', 'result': result})

    q = index(handler)
    q.upsert([{'id': 'owner-memory', 'values': [1, 0, 0], 'metadata': {'uid': 'owner'}}], namespace='ns2')
    assert calls[-1][2]['points'][0]['id'] == q._point('owner-memory')
    assert (
        q.query(vector=[1, 0, 0], top_k=3, filter={'uid': 'owner'}, namespace='ns2')['matches'][0]['id']
        == 'owner-memory'
    )
    assert calls[-1][2]['filter'] == translate({'uid': 'owner'})
    q.update('owner-memory', set_metadata={'category': 'fact'}, namespace='ns2')
    assert calls[-1][2]['payload'] == {'category': 'fact'}
    assert list(q.list(prefix='owner-', namespace='ns2')) == [['owner-memory']]
    with pytest.raises(ValueError, match='ownership'):
        q.update('owner-memory', set_metadata={'uid': 'other'}, namespace='ns2')
    q.close()


@pytest.mark.parametrize(
    'operation',
    [
        lambda q: q.delete(namespace='ns2', filter={}),
        lambda q: q.delete(namespace='ns2'),
        lambda q: q.query(vector=[1, 0, 0], top_k=1, filter={'uid': {'$unknown': 'x'}}, namespace='ns2'),
        lambda q: q.upsert([{'id': 'x', 'values': [1, 2], 'metadata': {'uid': 'x'}}], namespace='ns2'),
        lambda q: q.upsert([{'id': 'x', 'values': [1, 2, 3], 'metadata': {}}], namespace='ns2'),
    ],
)
def test_invalid_selection_or_batch_never_reaches_provider(operation):
    network = mock.Mock(side_effect=AssertionError('must not send'))
    q = index(network)
    with pytest.raises(ValueError):
        operation(q)
    network.assert_not_called()
    q.close()


def test_schema_migration_never_silently_changes_existing_dimensions():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(
            200, json={'status': 'ok', 'result': {'config': {'params': {'vectors': {'size': 4, 'distance': 'Cosine'}}}}}
        )

    q = index(handler)
    with pytest.raises(VectorStoreUnavailable, match='schema differs'):
        q.check(create=True)
    assert calls == ['GET']
    q.close()


def test_outage_is_not_empty_query_or_successful_purge():
    q = index(lambda request: httpx.Response(503, text='secret diagnostic'))
    for operation in (lambda: q.check(), lambda: q.purge_owner('owner')):
        with pytest.raises(VectorStoreUnavailable) as error:
            operation()
        assert 'secret' not in str(error.value)
    q.close()


def test_all_namespaces_are_checked_and_empty_membership_is_false():
    q = index(lambda request: httpx.Response(200, json={'status': 'ok', 'result': {'count': 0}}))
    with mock.patch.object(q, 'count_owner', return_value=0) as count, mock.patch.object(q, 'delete') as delete:
        assert q.purge_owner('owner') == 0
        assert count.call_count == len(NAMESPACES) * 2
        assert delete.call_count == len(NAMESPACES)
    assert translate({'uid': {'$in': []}}) == {'must': [{'has_id': []}]}
    q.close()


def test_successful_delete_response_with_residual_count_cannot_complete():
    q = index(lambda request: httpx.Response(200, json={'status': 'ok', 'result': True}))
    with mock.patch.object(q, 'count_owner', return_value=1):
        with pytest.raises(VectorStoreUnavailable, match='residual'):
            q.purge_owner('owner')
    q.close()
