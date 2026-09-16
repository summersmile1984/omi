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


class _BoundedEmbeddings:
    """Shared bounded input, executor and result-cardinality contracts.

    Both the local Ollama owner and the hosted OpenAI-compatible owner keep the
    same request bounds and the same vector validation: identity echo, exact
    dimension, finite nonzero floats and exact cardinality.
    """

    def __init__(self, spec, endpoint, *, transport=None, timeout=60):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in ('http', 'https')
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError('EMBEDDING_ENDPOINT must be an explicit service origin')
        self.spec, self.endpoint = spec, endpoint.rstrip('/')
        self.transport = transport
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError('embedding timeout must be finite and within 300 seconds')
        self.timeout = timeout

    def _echo_matches(self, echo):
        """The requested model id, or the serving provider's documented echo of it.

        Routed vendors (OpenRouter's embeddings router) return the serving
        provider's own id, e.g. ``parasail-bge-m3`` for ``baai/bge-m3``. The
        identity check admits the exact id or an echo ending at the model's
        architecture name; anything else fails closed.
        """
        requested = self.spec.embedding_model
        if not isinstance(echo, str) or not echo:
            return False
        if echo == requested:
            return True
        tail = requested.rsplit('/', 1)[-1]
        return echo.endswith('/' + tail) or echo.endswith('-' + tail)

    def _bounded_input(self, texts):
        if not isinstance(texts, list) or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError('embedding input must be a list of nonempty text strings')
        if not texts:
            return []
        if len(texts) > 64 or sum(len(text.encode()) for text in texts) > 1024 * 1024:
            raise ValueError('embedding batch exceeds the bounded request size')
        return texts

    def _validate_vectors(self, echo, vectors, expected):
        if not self._echo_matches(echo) or not isinstance(vectors, list) or len(vectors) != len(expected):
            raise EmbeddingUnavailable('embedding result identity or cardinality differs')
        for vector in vectors:
            if (
                not isinstance(vector, list)
                or len(vector) != self.spec.embedding_dimension
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


class OllamaEmbeddings(_BoundedEmbeddings):
    def __init__(self, contract, endpoint, *, transport=None, timeout=60):
        super().__init__(contract, endpoint, transport=transport, timeout=timeout)
        parsed = urlsplit(endpoint)
        # The local Ollama authority is a bare service origin; a path prefix
        # would silently change which authority /api/tags speaks to.
        if parsed.path not in ('', '/'):
            raise ValueError('EMBEDDING_ENDPOINT must be an explicit service origin')
        assert_http_endpoint_allowed(endpoint)

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
        models = [item for item in tags['models'] if isinstance(item, dict) and item.get('name') == self.spec.model]
        if len(models) != 1 or models[0].get('digest') != self.spec.manifest_digest.removeprefix('sha256:'):
            raise EmbeddingUnavailable('selected embedding manifest identity differs')
        info = self._request(client, 'POST', '/api/show', deadline, {'model': self.spec.model})
        metadata = info.get('model_info')
        if (
            not isinstance(metadata, dict)
            or not isinstance(info.get('modelfile'), str)
            or not isinstance(info.get('capabilities'), list)
        ):
            raise EmbeddingUnavailable('embedding model description is malformed')
        dimensions = [value for key, value in metadata.items() if key.endswith('.embedding_length')]
        contexts = [value for key, value in metadata.items() if key.endswith('.context_length')]
        artifact = self.spec.artifact_digest.replace(':', '-')
        sources = [
            line.removeprefix('FROM ').strip()
            for line in info.get('modelfile', '').splitlines()
            if line.startswith('FROM ')
        ]
        if (
            dimensions != [self.spec.dimension]
            or contexts != [self.spec.context_length]
            or len(sources) != 1
            or sources[0].rsplit('/', 1)[-1] != artifact
            or 'embedding' not in info.get('capabilities', [])
        ):
            raise EmbeddingUnavailable('selected embedding artifact or model metadata differs')

    def embed_documents(self, texts):
        texts = self._bounded_input(texts)
        if not texts:
            return []
        deadline = time.monotonic() + self.timeout
        with httpx.Client(transport=self.transport, follow_redirects=False) as client:
            self._identity(client, deadline)
            result = self._request(
                client,
                'POST',
                '/api/embed',
                deadline,
                {
                    'model': self.spec.model,
                    'input': texts,
                    'truncate': False,
                    'keep_alive': 0,
                    'options': {
                        'num_ctx': self.spec.context_length,
                        'num_thread': 4,
                        'num_batch': 128,
                        'use_mmap': True,
                    },
                },
            )
        vectors = result.get('embeddings')
        if result.get('model') != self.spec.model or not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingUnavailable('embedding result identity or cardinality differs')
        for vector in vectors:
            if (
                not isinstance(vector, list)
                or len(vector) != self.spec.dimension
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
                or not any(value != 0 for value in vector)
            ):
                raise EmbeddingUnavailable('embedding result contains an invalid vector')
        return vectors


class HostedEmbeddings(_BoundedEmbeddings):
    """The hosted OpenAI-compatible `/embeddings` owner.

    The model id and its dimension come from the frozen operator spec, not the
    environment: the request sends no `dimensions` parameter (bge-m3 rejects it
    on some vendors), and the response must echo that model id with exactly the
    declared vector dimension. There is no provider fallback.
    """

    def __init__(self, spec, *, transport=None, timeout=60):
        super().__init__(spec, spec.embedding_base_url, transport=transport, timeout=timeout)
        if not isinstance(self.spec.embedding_dimension, int) or self.spec.embedding_dimension <= 0:
            raise ValueError('hosted embedding dimension must be a positive integer')
        # Egress is asserted on the exact embeddings endpoint, the same grant
        # operator_ai.allows exposes; a bare-origin assertion would widen it.
        assert_http_endpoint_allowed(self.spec.embedding_base_url + '/embeddings')

    def _headers(self):
        from . import operator_ai

        return {'Authorization': 'Bearer ' + operator_ai.embedding_credentials()}

    def embed_documents(self, texts):
        texts = self._bounded_input(texts)
        if not texts:
            return []
        try:
            with httpx.Client(
                transport=self.transport,
                follow_redirects=False,
                headers=self._headers(),
                timeout=self.timeout,
            ) as client:
                with client.stream(
                    'POST',
                    self.endpoint + '/embeddings',
                    json={'model': self.spec.embedding_model, 'input': texts},
                ) as response:
                    if response.status_code != 200:
                        raise EmbeddingUnavailable(
                            f'embedding authority rejected request (HTTP {response.status_code})'
                        )
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 4_000_000:
                            raise EmbeddingUnavailable('embedding result exceeds the bounded response size')
                    import json

                    result = json.loads(body)
        except EmbeddingUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise EmbeddingUnavailable('embedding authority unavailable or malformed') from error
        if not isinstance(result, dict):
            raise EmbeddingUnavailable('embedding result is malformed')
        data = result.get('data')
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise EmbeddingUnavailable('embedding result is malformed')
        ordered = [item.get('embedding') for item in sorted(data, key=lambda item: item.get('index') or 0)]
        return self._validate_vectors(result.get('model'), ordered, texts)


def build():
    from . import operator_ai

    selected = operator_ai.select(current())
    if isinstance(selected, operator_ai.HostedOperatorAI):
        return HostedEmbeddings(selected, timeout=selected.request_timeout_seconds).check()
    return OllamaEmbeddings(selected_contract(), os.environ.get('EMBEDDING_ENDPOINT', '')).check()
