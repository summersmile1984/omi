"""Live pgvector behavior against a disposable, caller-supplied PostgreSQL database.

Set PGVECTOR_TEST_DSN to a disposable PostgreSQL+psycopg database with CREATE
EXTENSION privilege to exercise these integration paths. Every test owns its
randomly named tables and removes only those tables on completion.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from fork.model_contract import validate
from fork.vector_pg import Config, NAMESPACES, PgVectorIndex, VectorStoreUnavailable

_MODEL = validate(
    {
        'provider': 'ollama',
        'model': 'synthetic:fixed',
        'dimension': 3,
        'context_length': 8,
        'manifest_digest': 'sha256:' + 'a' * 64,
        'artifact_digest': 'sha256:' + 'b' * 64,
    }
)


@pytest.fixture
def index():
    dsn = os.getenv('PGVECTOR_TEST_DSN')
    if not dsn:
        pytest.skip('PGVECTOR_TEST_DSN must reference a disposable pgvector-capable PostgreSQL database')
    engine = create_engine(dsn, pool_pre_ping=True)
    prefix = 'vec_test_' + uuid.uuid4().hex[:16]
    adapter = PgVectorIndex(Config(dsn, prefix, _MODEL), engine=engine)
    try:
        with pytest.raises(VectorStoreUnavailable, match='not migrated'):
            adapter.check()
        adapter.check(create=True)
        yield adapter
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS public.{prefix}_identity'))
            conn.execute(text(f'DROP TABLE IF EXISTS public.{prefix}_vectors'))
        engine.dispose()


def record(id, uid='alice', *, values=None, **metadata):
    return {'id': id, 'values': values or [1, 0, 0], 'metadata': {'uid': uid, **metadata}}


def test_cosine_filter_isolation_update_and_pagination(index):
    vectors = [
        record('alice-best', tags=['work', 'private'], created_at=20),
        record('alice-near', values=[0.8, 0.2, 0], tags=['work'], created_at=[30, 35]),
        record('alice-old', values=[0, 1, 0], tags=[], created_at=3),
        record('bob-best', 'bob', tags=['work'], created_at=20),
    ]
    assert index.upsert(vectors, namespace='ns2') == {'upserted_count': 4}
    assert index.upsert([record('alice-best', tags=['replaced'], created_at=20)], 'ns2') == {'upserted_count': 1}
    assert index.count_owner('alice', 'ns2') == 3
    query = lambda filter: index.query(
        vector=[1, 0, 0], top_k=10, namespace='ns2', filter=filter, include_metadata=True, include_values=True
    )['matches']
    matches = query(
        {
            '$and': [
                {'uid': {'$eq': 'alice'}},
                {'created_at': {'$gte': 10, '$lte': 30}},
                {'$or': [{'tags': {'$in': ['work']}}, {'tags': {'$eq': 'replaced'}}]},
            ]
        }
    )
    assert [item['id'] for item in matches] == ['alice-best', 'alice-near']
    assert matches[0]['score'] > matches[1]['score']
    assert matches[0]['values'] == [1.0, 0.0, 0.0]
    assert matches[0]['metadata']['tags'] == ['replaced']
    assert query({'uid': 'alice', 'tags': {'$in': []}}) == []
    assert [item['id'] for item in query({'uid': 'alice', 'missing': {'$exists': False}})] == [
        'alice-best',
        'alice-near',
        'alice-old',
    ]
    assert len(query({'uid': 'alice', 'tags': {'$eq': 'work'}})) == 1
    assert len(index.query(vector=[1, 0, 0], top_k=1, namespace='ns2', filter={'uid': 'alice'})['matches']) == 1
    index.update('alice-best', set_metadata={'category': 'kept'}, namespace='ns2')
    assert query({'uid': 'alice', 'category': 'kept'})[0]['metadata']['tags'] == ['replaced']
    with pytest.raises(ValueError, match='ownership'):
        index.update('alice-best', set_metadata={'uid': 'bob'}, namespace='ns2')
    with pytest.raises(ValueError, match='ownership'):
        index.upsert([record('alice-best', 'bob')], 'ns2')
    assert index.count_owner('alice', 'ns2') == 3
    assert index.count_owner('bob', 'ns2') == 1
    index.delete(namespace='ns2', filter={'uid': 'alice', 'created_at': {'$lte': 3}})
    index.delete(namespace='ns2', ids=['alice-near'])
    assert [item['id'] for item in query({'uid': 'alice'})] == ['alice-best']
    assert index.purge_owner('alice') == 1
    assert index.count_owner('alice', 'ns2') == 0
    assert index.count_owner('bob', 'ns2') == 1


def test_cursor_listing_and_full_namespace_purge(index):
    ids = [f'alice-{n:04}' for n in range(520)]
    index.upsert((record(id) for id in ids), 'ns_tchunks')
    index.upsert([record('alice-else')], 'ns1')
    index.upsert([record('bob-stays', 'bob')], 'ns_tchunks')
    assert list(index.list(prefix='alice-', namespace='ns_tchunks')) == [ids[:256], ids[256:512], ids[512:]]
    assert index.purge_owner('alice') == 521
    assert all(index.count_owner('alice', namespace) == 0 for namespace in NAMESPACES)
    assert index.count_owner('bob', 'ns_tchunks') == 1


def test_schema_model_and_namespace_drift_never_relabels_existing_data(index):
    index.upsert([record('alice-best')], 'ns1')
    other = validate({**_MODEL.as_dict(), 'model': 'synthetic:other'})
    with pytest.raises(VectorStoreUnavailable, match='model identity'):
        PgVectorIndex(Config(index.config.dsn, index.config.prefix, other), engine=index.engine).check(create=True)
    with index.engine.begin() as conn:
        conn.execute(
            text(f'UPDATE public.{index.config.prefix}_identity SET namespaces=CAST(:value AS jsonb)'),
            {'value': '["ns1"]'},
        )
    with pytest.raises(VectorStoreUnavailable, match='namespace'):
        index.check()
    with index.engine.begin() as conn:
        conn.execute(
            text(f'UPDATE public.{index.config.prefix}_identity SET namespaces=CAST(:value AS jsonb)'),
            {'value': __import__('json').dumps(NAMESPACES)},
        )
    assert index.count_owner('alice', 'ns1') == 1


def test_unsafe_filter_rejected_before_db_access(index):
    for condition in (
        {'$or': [{'uid': 'alice'}, {'category': 'work'}]},
        {'$and': [{'uid': 'alice'}, {'uid': 'bob'}]},
        {'uid': {'$in': ['alice', 'bob']}},
        {'uid': 'alice', 'created_at': {'$bad': 0}},
        {'uid': 'alice', 'bad); DROP TABLE': 'x'},
    ):
        with pytest.raises(ValueError):
            index.query(vector=[1, 0, 0], top_k=3, filter=condition, namespace='ns1')
        with pytest.raises(ValueError):
            index.delete(filter=condition, namespace='ns1')
    with pytest.raises(ValueError):
        index.query(vector=[1, 0, 0], top_k=10001, filter={'uid': 'alice'}, namespace='ns1')
    with pytest.raises(ValueError):
        index.upsert([record('wrong', values=[1, 0])], 'ns1')
    assert index.count_owner('alice', 'ns1') == 0


def test_profile_selects_exact_authority_and_rejects_unknown(monkeypatch):
    from fork import profile, vector_pg, vector_qdrant
    from fork.patches import vector as vector_patch

    class Pg:
        def __init__(self, config):
            self.config = config

        def check(self):
            return self

    class Qdrant(Pg):
        pass

    monkeypatch.setattr(vector_pg, 'PgVectorIndex', Pg)
    monkeypatch.setattr(vector_qdrant, 'QdrantIndex', Qdrant)
    monkeypatch.setattr(vector_pg.Config, 'from_env', lambda: 'postgres-config')
    monkeypatch.setattr(vector_qdrant.Config, 'from_env', lambda: 'qdrant-config')
    row = {'data_plane': {'vector': 'pgvector'}}
    monkeypatch.setattr(profile, 'current', lambda: row)
    assert type(vector_patch._index(None)) is Pg
    assert vector_patch._index(None).config == 'postgres-config'
    row['data_plane']['vector'] = 'qdrant'
    assert type(vector_patch._index(None)) is Qdrant
    assert vector_patch._index(None).config == 'qdrant-config'
    row['data_plane']['vector'] = 'unknown'
    with pytest.raises(ValueError, match='not supported'):
        vector_patch._index(None)


def test_deletion_guard_rejects_wrong_index_even_if_it_implements_same_methods(monkeypatch):
    from database import vector_db
    from fork import profile, provider_guard, vector_pg, vector_qdrant

    class Pg:
        pass

    class Qdrant:
        pass

    monkeypatch.setattr(vector_pg, 'PgVectorIndex', Pg)
    monkeypatch.setattr(vector_qdrant, 'QdrantIndex', Qdrant)
    row = {'data_plane': {'vector': 'pgvector'}}
    monkeypatch.setattr(profile, 'current', lambda: row)
    monkeypatch.setattr(vector_db, 'index', Qdrant())
    with pytest.raises(RuntimeError, match='selected vector authority'):
        provider_guard.external_index()
    local = Pg()
    monkeypatch.setattr(vector_db, 'index', local)
    assert provider_guard.external_index() is local
    row['data_plane']['vector'] = 'qdrant'
    with pytest.raises(RuntimeError, match='selected vector authority'):
        provider_guard.external_index()
    production = Qdrant()
    monkeypatch.setattr(vector_db, 'index', production)
    assert provider_guard.external_index() is production
