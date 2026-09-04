"""Behavioral model identity/inference contracts; live CPU inference is separate."""

import hashlib
import json
from unittest import mock

import httpx
import pytest

from fork import embedding
from fork.model_contract import validate
from fork.model_store import check
from fork.patches.embedding import patches
from fork.registry import build_registry

CONTRACT = {
    'provider': 'ollama',
    'model': 'test:fixed',
    'manifest_digest': 'sha256:' + 'a' * 64,
    'artifact_digest': 'sha256:' + 'b' * 64,
    'dimension': 3,
    'context_length': 8,
}


def provider(mutate=None):
    def handler(request):
        if request.url.path == '/api/tags':
            data = {'models': [{'name': 'test:fixed', 'digest': CONTRACT['manifest_digest'].removeprefix('sha256:')}]}
        elif request.url.path == '/api/show':
            data = {
                'model_info': {'bert.embedding_length': 3, 'bert.context_length': 8},
                'modelfile': 'FROM /models/blobs/' + CONTRACT['artifact_digest'].replace(':', '-'),
                'capabilities': ['embedding'],
            }
        else:
            body = json.loads(request.content)
            assert body['truncate'] is False
            assert 'dimensions' not in body
            data = {'model': 'test:fixed', 'embeddings': [[1, 0, 0] for _ in body['input']]}
        if mutate:
            mutate(request.url.path, data)
        return httpx.Response(200, json=data)

    return embedding.OllamaEmbeddings(validate(CONTRACT), 'http://localhost', transport=httpx.MockTransport(handler))


def test_actual_owner_supports_sync_async_and_never_requests_dimension_truncation():
    import asyncio

    model = provider()
    assert model.embed_documents(['hello', '你好']) == [[1, 0, 0], [1, 0, 0]]
    assert asyncio.run(model.aembed_query('hello')) == [1, 0, 0]


@pytest.mark.parametrize(
    'path,field,value',
    [
        ('/api/tags', 'models', None),
        ('/api/tags', 'models', []),
        ('/api/show', 'model_info', None),
        ('/api/show', 'model_info', {'bert.embedding_length': 4, 'bert.context_length': 8}),
        ('/api/show', 'modelfile', 'FROM /models/blobs/unknown'),
        ('/api/show', 'capabilities', ['completion']),
        ('/api/embed', 'model', 'another:model'),
        ('/api/embed', 'embeddings', []),
        ('/api/embed', 'embeddings', [[0, 0, 0]]),
        ('/api/embed', 'embeddings', [[True, 0, 0]]),
        ('/api/embed', 'embeddings', [[1, 0]]),
    ],
)
def test_bad_identity_or_result_is_failure_not_empty_success(path, field, value):
    def mutate(actual, data):
        if actual == path:
            data[field] = value

    with pytest.raises(embedding.EmbeddingUnavailable):
        provider(mutate).embed_query('synthetic')


@pytest.mark.parametrize('status', [400, 401, 500, 503])
def test_provider_failure_never_changes_provider(status):
    client = embedding.OllamaEmbeddings(
        validate(CONTRACT),
        'http://localhost',
        transport=httpx.MockTransport(lambda r: httpx.Response(status, text='private diagnostic')),
    )
    with pytest.raises(embedding.EmbeddingUnavailable) as result:
        client.embed_query('test')
    assert 'private' not in str(result.value)


def test_profile_dimension_and_ambient_conflicts_fail_before_network(monkeypatch):
    row = {'target': 'self_hosted', 'embedding': CONTRACT, 'capabilities': {'embedding_dims': 3}}
    monkeypatch.setattr(embedding, 'current', lambda: row)
    monkeypatch.setenv('EMBEDDING_DIMENSION', '1024')
    with pytest.raises(ValueError, match='conflicts'):
        embedding.selected_contract()
    monkeypatch.delenv('EMBEDDING_DIMENSION')
    row['capabilities']['embedding_dims'] = 4
    with pytest.raises(ValueError, match='capability'):
        embedding.selected_contract()


def test_canonical_and_captured_production_consumers_share_one_model():
    import database.vector_db as vector
    import utils.llm.clients as clients

    original = clients.embeddings, vector.embeddings
    model = provider()
    try:
        with mock.patch.object(embedding, 'build', return_value=model) as build:
            build_registry(patches()).apply({'target': 'self_hosted'})
            assert clients.embeddings is vector.embeddings is model
            assert clients.generate_embedding('test') == [1, 0, 0]
            assert build.call_count == 1
    finally:
        clients.embeddings, vector.embeddings = original


def test_readonly_model_store_validates_every_manifest_blob_and_tamper(tmp_path):
    blob = b'synthetic GGUF model bytes'
    digest = 'sha256:' + hashlib.sha256(blob).hexdigest()
    config_blob = b'{}'
    config_digest = 'sha256:' + hashlib.sha256(config_blob).hexdigest()
    manifest = {
        'config': {'digest': config_digest, 'size': 2},
        'layers': [{'digest': digest, 'size': len(blob), 'mediaType': 'application/vnd.ollama.image.model'}],
    }
    raw = json.dumps(manifest).encode()
    contract = validate(
        {**CONTRACT, 'artifact_digest': digest, 'manifest_digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}
    )
    folder = tmp_path / 'manifests/registry.ollama.ai/library/test'
    folder.mkdir(parents=True)
    (folder / 'fixed').write_bytes(raw)
    (tmp_path / 'blobs').mkdir()
    for sha, content in ((digest, blob), (config_digest, config_blob)):
        (tmp_path / 'blobs' / sha.replace(':', '-')).write_bytes(content)
    assert check(tmp_path, contract)['verified_blobs'] == 2
    (tmp_path / 'blobs' / config_digest.replace(':', '-')).write_bytes(b'[]')
    with pytest.raises(ValueError, match='checksum'):
        check(tmp_path, contract)


@pytest.mark.parametrize('model', ['test', '../test:fixed', 'a/../b:fixed', 'a//b:fixed'])
def test_model_identity_never_uses_implicit_tags_or_unsafe_paths(model):
    with pytest.raises(ValueError):
        validate({**CONTRACT, 'model': model})
