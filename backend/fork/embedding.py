"""Selected local-model embedding authority; no BYOK, gateway or vendor fallback."""

import math
import os
import time
from urllib.parse import urlsplit

import httpx

from .model_contract import validate
from .profile import current
from .egress_policy import assert_http_endpoint_allowed


class EmbeddingUnavailable(RuntimeError):
    """The selected model could not establish identity or produce valid vectors."""


def selected_contract():
    row = current()
    if row.get('target') != 'self_hosted':
        raise ValueError('local embedding requires the self-host profile')
    contract = validate(row.get('embedding'))
    if row.get('capabilities', {}).get('embedding_dims') != contract.dimension:
        raise ValueError('profile embedding capability differs from its model contract')
    for setting, value in {
        'EMBEDDING_PROVIDER': contract.provider,
        'EMBEDDING_MODEL': contract.model,
        'EMBEDDING_DIMENSION': str(contract.dimension),
    }.items():
        if setting in os.environ and os.environ[setting] != value:
            raise ValueError(f'{setting} conflicts with the generated model contract')
    return contract


class OllamaEmbeddings:
    def __init__(self, contract, endpoint, *, transport=None, timeout=60):
        self.contract = contract
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in ('http', 'https')
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ('', '/')
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError('EMBEDDING_ENDPOINT must be an explicit service origin')
        assert_http_endpoint_allowed(endpoint)
        self.endpoint = endpoint.rstrip('/')
        self.transport = transport
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError('embedding timeout must be finite and within 300 seconds')
        self.timeout = timeout

    def _request(self, client, method, path, deadline, data=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EmbeddingUnavailable('embedding request deadline exceeded')
        try:
            response = client.request(method, self.endpoint + path, json=data, timeout=remaining)
            if response.status_code != 200:
                raise EmbeddingUnavailable(f'embedding authority rejected request (HTTP {response.status_code})')
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError('invalid envelope')
            return result
        except (httpx.HTTPError, ValueError) as error:
            raise EmbeddingUnavailable('embedding authority unavailable or malformed') from error

    def _identity(self, client, deadline):
        tags = self._request(client, 'GET', '/api/tags', deadline)
        if not isinstance(tags.get('models'), list):
            raise EmbeddingUnavailable('embedding model inventory is malformed')
        models = [item for item in tags['models'] if isinstance(item, dict) and item.get('name') == self.contract.model]
        if len(models) != 1 or models[0].get('digest') != self.contract.manifest_digest.removeprefix('sha256:'):
            raise EmbeddingUnavailable('selected embedding manifest identity differs')
        info = self._request(client, 'POST', '/api/show', deadline, {'model': self.contract.model})
        metadata = info.get('model_info')
        if (
            not isinstance(metadata, dict)
            or not isinstance(info.get('modelfile'), str)
            or not isinstance(info.get('capabilities'), list)
        ):
            raise EmbeddingUnavailable('embedding model description is malformed')
        dimensions = [value for key, value in metadata.items() if key.endswith('.embedding_length')]
        contexts = [value for key, value in metadata.items() if key.endswith('.context_length')]
        artifact = self.contract.artifact_digest.replace(':', '-')
        sources = [
            line.removeprefix('FROM ').strip()
            for line in info.get('modelfile', '').splitlines()
            if line.startswith('FROM ')
        ]
        if (
            dimensions != [self.contract.dimension]
            or contexts != [self.contract.context_length]
            or len(sources) != 1
            or sources[0].rsplit('/', 1)[-1] != artifact
            or 'embedding' not in info.get('capabilities', [])
        ):
            raise EmbeddingUnavailable('selected embedding artifact or model metadata differs')

    def embed_documents(self, texts):
        if not isinstance(texts, list) or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError('embedding input must be a list of nonempty text strings')
        if not texts:
            return []
        if len(texts) > 64 or sum(len(text.encode()) for text in texts) > 1024 * 1024:
            raise ValueError('embedding batch exceeds the bounded request size')
        deadline = time.monotonic() + self.timeout
        with httpx.Client(transport=self.transport, follow_redirects=False) as client:
            self._identity(client, deadline)
            result = self._request(
                client,
                'POST',
                '/api/embed',
                deadline,
                {
                    'model': self.contract.model,
                    'input': texts,
                    'truncate': False,
                    'keep_alive': 0,
                    'options': {
                        'num_ctx': self.contract.context_length,
                        'num_thread': 4,
                        'num_batch': 128,
                        'use_mmap': True,
                    },
                },
            )
        vectors = result.get('embeddings')
        if result.get('model') != self.contract.model or not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingUnavailable('embedding result identity or cardinality differs')
        for vector in vectors:
            if (
                not isinstance(vector, list)
                or len(vector) != self.contract.dimension
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
                or not any(value != 0 for value in vector)
            ):
                raise EmbeddingUnavailable('embedding result contains an invalid vector')
        return vectors

    def embed_query(self, text):
        return self.embed_documents([text])[0]

    async def aembed_documents(self, texts):
        # Reuse the repository-owned bounded provider executor; never block the
        # event loop or introduce an unbounded private thread pool.
        from utils.executors import run_blocking, llm_executor

        return await run_blocking(llm_executor, self.embed_documents, texts)

    async def aembed_query(self, text):
        return (await self.aembed_documents([text]))[0]

    def check(self):
        self.embed_query('Local embedding readiness check')
        return self


def build():
    return OllamaEmbeddings(selected_contract(), os.environ.get('EMBEDDING_ENDPOINT', '')).check()
