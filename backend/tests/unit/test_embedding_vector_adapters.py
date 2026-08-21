from __future__ import annotations

import builtins
import importlib

import pytest
from qdrant_client import QdrantClient

from database.vector_store import (
    PineconeVectorStoreAdapter,
    QdrantVectorStoreAdapter,
    VectorStore,
)
from database.vector_projection import (
    PROJECTION_DIMENSION_KEY,
    PROJECTION_LOGICAL_NAMESPACE_KEY,
    PROJECTION_MODEL_KEY,
    PROJECTION_NAMESPACE_VERSION_KEY,
    PROJECTION_PROVIDER_KEY,
    PROJECTION_SCHEMA_VERSION_KEY,
    ProjectionMigrationTool,
    ProjectionRecord,
    ProjectionUnavailableError,
    VerificationReport,
    VersionedVectorStoreAdapter,
)
from utils.llm.embedding_providers import (
    ConfiguredEmbeddingProviderProxy,
    EmbeddingProvider,
    GeminiEmbeddingProviderAdapter,
    LangChainEmbeddingProviderAdapter,
    OpenAICompatibleEmbeddingProviderAdapter,
)
from utils.llm.providers import ModelProviderConfigurationError


def test_unselected_qdrant_adapter_does_not_expand_backend_import_requirements(monkeypatch):
    import database.vector_store as vector_store

    real_import = builtins.__import__

    def reject_qdrant_import(name, *args, **kwargs):
        if name == 'qdrant_client' or name.startswith('qdrant_client.'):
            raise ModuleNotFoundError("No module named 'qdrant_client'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', reject_qdrant_import)
    importlib.reload(vector_store)
    assert vector_store.PineconeVectorStoreAdapter is not None

    monkeypatch.setenv('QDRANT_PATH', ':memory:')
    with pytest.raises(RuntimeError, match='qdrant-client package'):
        vector_store.create_qdrant_vector_store_from_env()


class FakeEmbeddings:
    def embed_query(self, text):
        return [float(len(text)), 1.0]

    def embed_documents(self, texts):
        return [[float(len(text)), 1.0] for text in texts]


def test_existing_langchain_embeddings_conform_to_the_provider_boundary():
    adapter = LangChainEmbeddingProviderAdapter(
        FakeEmbeddings(),
        provider_id='openai',
        model_id='text-embedding-3-large',
    )

    assert isinstance(adapter, EmbeddingProvider)
    assert adapter.embed_query('abc') == [3.0, 1.0]
    assert adapter.embed_documents(['a', 'abcd']) == [[1.0, 1.0], [4.0, 1.0]]


def test_existing_gemini_query_embedding_conforms_to_the_provider_boundary():
    adapter = GeminiEmbeddingProviderAdapter(lambda text: [float(len(text)), 2.0], dimension=2)

    assert isinstance(adapter, EmbeddingProvider)
    assert adapter.provider_id == 'gemini'
    assert adapter.embed_query('abc') == [3.0, 2.0]
    assert adapter.embed_documents(['a', 'abcd']) == [[1.0, 2.0], [4.0, 2.0]]


def test_generic_openai_compatible_embedding_adapter_uses_only_configured_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setenv('GENERIC_OPENAI_BASE_URL', 'http://127.0.0.1:11434/v1')
    monkeypatch.setenv('GENERIC_OPENAI_API_KEY', 'local-key')

    def factory(**kwargs):
        captured.update(kwargs)
        return FakeEmbeddings()

    adapter = OpenAICompatibleEmbeddingProviderAdapter(
        provider_id='generic',
        model_id='local-embedding',
        dimension=2,
        client_factory=factory,
    )

    assert adapter.embed_query('local') == [5.0, 1.0]
    assert captured == {
        'model': 'local-embedding',
        'api_key': 'local-key',
        'base_url': 'http://127.0.0.1:11434/v1',
        'check_embedding_ctx_length': False,
        'dimensions': 2,
    }


def test_openrouter_embedding_without_credentials_fails_before_client_construction(monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    constructed = []

    with pytest.raises(ModelProviderConfigurationError) as error:
        OpenAICompatibleEmbeddingProviderAdapter(
            provider_id='openrouter',
            model_id='text-embedding-model',
            client_factory=lambda **kwargs: constructed.append(kwargs),
        )

    assert error.value.provider == 'openrouter'
    assert error.value.reason == 'credential_not_configured'
    assert constructed == []


def test_configured_embedding_proxy_switches_to_generic_at_call_boundary(monkeypatch):
    default = LangChainEmbeddingProviderAdapter(
        FakeEmbeddings(),
        provider_id='openai',
        model_id='text-embedding-3-large',
    )
    proxy = ConfiguredEmbeddingProviderProxy(default)
    monkeypatch.setenv('EMBEDDING_PROVIDER', 'generic')
    monkeypatch.setenv('EMBEDDING_MODEL', 'local-embedding')
    monkeypatch.setenv('GENERIC_OPENAI_BASE_URL', 'http://127.0.0.1:11434/v1')
    monkeypatch.setenv('GENERIC_OPENAI_API_KEY', 'local-key')
    monkeypatch.setattr(
        'utils.llm.embedding_providers.OpenAIEmbeddings',
        lambda **_kwargs: FakeEmbeddings(),
    )

    assert proxy.provider_id == 'generic'
    assert proxy.model_id == 'local-embedding'
    assert proxy.embed_documents(['neutral']) == [[7.0, 1.0]]


class FakePineconeIndex:
    def __init__(self):
        self.calls = []

    def upsert(self, **kwargs):
        self.calls.append(('upsert', kwargs))
        return {'ok': True}

    def query(self, **kwargs):
        self.calls.append(('query', kwargs))
        return {'matches': []}

    def delete(self, **kwargs):
        self.calls.append(('delete', kwargs))
        return {'ok': True}

    def update(self, *args, **kwargs):
        self.calls.append(('update', args, kwargs))
        return {'ok': True}

    def list(self, **kwargs):
        self.calls.append(('list', kwargs))
        return iter([['one']])

    def fetch(self, **kwargs):
        self.calls.append(('fetch', kwargs))
        return {'vectors': {}}

    def describe_index_stats(self, **kwargs):
        self.calls.append(('stats', kwargs))
        return {'namespaces': {'ns': {'vector_count': 2}}}


def test_existing_pinecone_index_is_exposed_as_a_vector_store_adapter():
    raw = FakePineconeIndex()
    store = PineconeVectorStoreAdapter(raw)

    assert isinstance(store, VectorStore)
    store.upsert(vectors=[{'id': 'one', 'values': [1.0], 'metadata': {}}], namespace='ns')
    store.query(vector=[1.0], top_k=1, namespace='ns')
    store.update('one', set_metadata={'kind': 'updated'}, namespace='ns')
    assert list(store.list(prefix='o', namespace='ns')) == [['one']]
    store.delete(ids=['one'], namespace='ns')
    store.fetch(ids=['one'], namespace='ns')
    assert [call[0] for call in raw.calls] == ['upsert', 'query', 'update', 'list', 'delete', 'fetch']


def test_pinecone_projection_count_supports_unfiltered_completeness_verification():
    raw = FakePineconeIndex()
    store = PineconeVectorStoreAdapter(raw)

    assert store.count(namespace='ns', filter={}) == 2
    assert raw.calls[-1] == ('stats', {})
    assert store.count(namespace='ns', filter={'uid': 'u1'}) == 2
    assert raw.calls[-1] == ('stats', {'filter': {'uid': 'u1'}})


def test_qdrant_adapter_supports_legacy_ids_filters_updates_and_deletes():
    store = QdrantVectorStoreAdapter(QdrantClient(location=':memory:'), collection_prefix='neutral_test')
    store.upsert(
        vectors=[
            {'id': 'u1-item-a', 'values': [1.0, 0.0], 'metadata': {'uid': 'u1', 'score': 10}},
            {'id': 'u1-item-b', 'values': [0.8, 0.2], 'metadata': {'uid': 'u1', 'score': 2}},
            {'id': 'u2-item-c', 'values': [1.0, 0.0], 'metadata': {'uid': 'u2', 'score': 20}},
        ],
        namespace='memories',
    )

    result = store.query(
        vector=[1.0, 0.0],
        top_k=10,
        namespace='memories',
        include_metadata=True,
        filter={'$and': [{'uid': 'u1'}, {'score': {'$gte': 5}}]},
    )
    assert [match['id'] for match in result['matches']] == ['u1-item-a']
    assert result['matches'][0]['metadata'] == {'uid': 'u1', 'score': 10}

    store.update('u1-item-a', set_metadata={'kind': 'updated'}, namespace='memories')
    updated = store.query(
        vector=[1.0, 0.0],
        top_k=10,
        namespace='memories',
        include_metadata=True,
        filter={'kind': 'updated'},
    )
    assert updated['matches'][0]['id'] == 'u1-item-a'
    assert list(store.list(prefix='u1-', namespace='memories')) == [['u1-item-a', 'u1-item-b']]
    assert store.count(namespace='memories', filter={'uid': {'$eq': 'u1'}}) == 2

    store.delete(ids=['u1-item-a'], namespace='memories')
    after_id_delete = store.query(vector=[1.0, 0.0], top_k=10, namespace='memories', filter={'uid': 'u1'})
    assert [match['id'] for match in after_id_delete['matches']] == ['u1-item-b']
    store.delete(filter={'uid': 'u1'}, namespace='memories')
    assert store.count(namespace='memories', filter={'uid': {'$eq': 'u1'}}) == 0
    assert store.query(vector=[1.0, 0.0], top_k=10, namespace='memories', filter={'uid': 'u1'}) == {'matches': []}


class FakeEmbeddingIdentity:
    provider_id = 'generic'
    model_id = 'local-embedding'
    dimension = 2


def test_versioned_store_stamps_complete_projection_identity_and_dual_writes(monkeypatch):
    raw = QdrantVectorStoreAdapter(QdrantClient(location=':memory:'), collection_prefix='projection_contract')
    store = VersionedVectorStoreAdapter(raw, FakeEmbeddingIdentity())
    monkeypatch.setenv('VECTOR_PROJECTION_MODE', 'dual_write')
    monkeypatch.setenv('VECTOR_PROJECTION_ACTIVE_VERSION', 'v1')
    monkeypatch.setenv('VECTOR_PROJECTION_TARGET_VERSION', 'v2')
    monkeypatch.setenv('VECTOR_PROJECTION_SCHEMA_VERSION', '3')

    store.upsert(
        vectors=[{'id': 'one', 'values': [1.0, 0.0], 'metadata': {'uid': 'u1'}}],
        namespace='memories',
    )

    for version in ('v1', 'v2'):
        fetched = store.fetch_version(ids=['one'], namespace='memories', namespace_version=version)
        metadata = fetched['vectors']['one']['metadata']
        assert metadata == {
            'uid': 'u1',
            PROJECTION_PROVIDER_KEY: 'generic',
            PROJECTION_MODEL_KEY: 'local-embedding',
            PROJECTION_DIMENSION_KEY: 2,
            PROJECTION_SCHEMA_VERSION_KEY: 3,
            PROJECTION_NAMESPACE_VERSION_KEY: version,
            PROJECTION_LOGICAL_NAMESPACE_KEY: 'memories',
        }

    store.delete(ids=['one'], namespace='memories')
    for version in ('v1', 'v2'):
        assert store.fetch_version(ids=['one'], namespace='memories', namespace_version=version) == {'vectors': {}}


def test_privacy_reconciliation_counts_every_configured_projection_version(monkeypatch):
    raw = QdrantVectorStoreAdapter(QdrantClient(location=':memory:'), collection_prefix='projection_privacy')
    store = VersionedVectorStoreAdapter(raw, FakeEmbeddingIdentity())
    monkeypatch.setenv('VECTOR_PROJECTION_MODE', 'single')
    monkeypatch.setenv('VECTOR_PROJECTION_ACTIVE_VERSION', 'v2')
    monkeypatch.setenv('VECTOR_PROJECTION_DELETE_VERSIONS', 'v1,v2')

    for version in ('v1', 'v2'):
        store.write_version(
            vectors=[{'id': f'{version}-item', 'values': [1.0, 0.0], 'metadata': {'uid': 'u1'}}],
            namespace='memories',
            namespace_version=version,
        )

    assert store.count(namespace='memories', filter={'uid': {'$eq': 'u1'}}) == 1
    assert store.count_deletion_versions(namespace='memories', filter={'uid': {'$eq': 'u1'}}) == 2
    store.delete(namespace='memories', filter={'uid': {'$eq': 'u1'}})
    assert store.count_deletion_versions(namespace='memories', filter={'uid': {'$eq': 'u1'}}) == 0


def test_projection_migration_backfill_verify_switch_and_rollback(monkeypatch):
    monkeypatch.setenv('VECTOR_PROJECTION_MODE', 'single')
    monkeypatch.setenv('VECTOR_PROJECTION_ACTIVE_VERSION', 'v1')
    raw = QdrantVectorStoreAdapter(QdrantClient(location=':memory:'), collection_prefix='projection_migration')
    store = VersionedVectorStoreAdapter(raw, FakeEmbeddingIdentity())
    tool = ProjectionMigrationTool(store)
    records = [
        ProjectionRecord(id='one', values=(1.0, 0.0), metadata={'uid': 'u1'}),
        ProjectionRecord(id='two', values=(0.0, 1.0), metadata={'uid': 'u1'}),
    ]
    store.upsert(
        vectors=[
            {'id': record.id, 'values': list(record.values), 'metadata': dict(record.metadata)} for record in records
        ],
        namespace='memories',
    )

    backfill = tool.backfill(records, namespace='memories', target_version='v2', batch_size=1)
    verification = tool.verify(records, namespace='memories', target_version='v2', batch_size=1)

    assert (backfill.attempted, backfill.written, backfill.batches) == (2, 2, 2)
    assert verification.ready_to_switch is True
    assert ProjectionMigrationTool.switch_manifest(verification) == {
        'VECTOR_PROJECTION_MODE': 'single',
        'VECTOR_PROJECTION_ACTIVE_VERSION': 'v2',
        'VECTOR_PROJECTION_TARGET_VERSION': '',
        'VECTOR_PROJECTION_SCHEMA_VERSION': '1',
        'VECTOR_PROJECTION_DELETE_VERSIONS': 'v1,v2',
    }
    assert ProjectionMigrationTool.rollback_manifest('v1', abandoned_versions=('v2',)) == {
        'VECTOR_PROJECTION_MODE': 'single',
        'VECTOR_PROJECTION_ACTIVE_VERSION': 'v1',
        'VECTOR_PROJECTION_TARGET_VERSION': '',
        'VECTOR_PROJECTION_SCHEMA_VERSION': '1',
        'VECTOR_PROJECTION_DELETE_VERSIONS': 'v1,v2',
    }

    for key, value in ProjectionMigrationTool.switch_manifest(verification).items():
        monkeypatch.setenv(key, value)
    assert store.query(vector=[1.0, 0.0], top_k=10, namespace='memories')['matches']

    for key, value in ProjectionMigrationTool.rollback_manifest('v1', abandoned_versions=('v2',)).items():
        monkeypatch.setenv(key, value)
    assert store.query(vector=[1.0, 0.0], top_k=10, namespace='memories')['matches']


def test_projection_switch_fails_closed_until_target_verification_is_complete():
    report = VerificationReport(
        namespace='memories',
        namespace_version='v2',
        expected=2,
        present=1,
        missing_ids=('two',),
        mismatched_ids=(),
    )

    with pytest.raises(ProjectionUnavailableError) as error:
        ProjectionMigrationTool.switch_manifest(report)

    assert error.value.as_dict() == {
        'code': 'projection_unavailable',
        'capability': 'vector_switch',
        'reason': 'target verification is incomplete',
        'retryable': False,
    }


def test_qdrant_adapter_supports_pinecone_exists_filters():
    store = QdrantVectorStoreAdapter(QdrantClient(location=':memory:'), collection_prefix='neutral_exists_test')
    store.upsert(
        vectors=[
            {'id': 'legacy', 'values': [1.0, 0.0], 'metadata': {'uid': 'u1'}},
            {
                'id': 'versioned',
                'values': [1.0, 0.0],
                'metadata': {'uid': 'u1', 'memory_schema_version': 2},
            },
        ],
        namespace='memories',
    )

    missing = store.query(
        vector=[1.0, 0.0],
        top_k=10,
        namespace='memories',
        filter={'$and': [{'uid': {'$eq': 'u1'}}, {'memory_schema_version': {'$exists': False}}]},
    )
    present = store.query(
        vector=[1.0, 0.0],
        top_k=10,
        namespace='memories',
        filter={'memory_schema_version': {'$exists': True}},
    )

    assert [match['id'] for match in missing['matches']] == ['legacy']
    assert [match['id'] for match in present['matches']] == ['versioned']
