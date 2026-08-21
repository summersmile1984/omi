"""Provider-neutral embedding boundary used by vector-backed retrieval.

The first migration phase deliberately keeps the existing synchronous calling
contract. Callers depend on ``embed_query``/``embed_documents`` while provider
construction and model identity move behind this interface.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from langchain_openai import OpenAIEmbeddings

from utils.llm.providers import ModelProviderConfigurationError, get_openai_compatible_provider_config


@runtime_checkable
class EmbeddingProvider(Protocol):
    provider_id: str
    model_id: str
    dimension: int | None

    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class LangChainEmbeddingProviderAdapter:
    """Adapt the existing OpenAI/BYOK proxy without changing its behavior."""

    def __init__(
        self,
        client: Any,
        *,
        provider_id: str,
        model_id: str,
        dimension: int | None = None,
    ) -> None:
        self._client = client
        self.provider_id = provider_id
        self.model_id = model_id
        self.dimension = dimension

    def embed_query(self, text: str) -> list[float]:
        return list(self._client.embed_query(text))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [list(vector) for vector in self._client.embed_documents(texts)]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class OpenAICompatibleEmbeddingProviderAdapter:
    """Embedding adapter for any configured OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        provider_id: str,
        model_id: str,
        dimension: int | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        config = get_openai_compatible_provider_config(provider_id)
        api_key = os.getenv(config.api_key_env, '').strip()
        if not api_key:
            raise ModelProviderConfigurationError(
                config.name,
                'credential_not_configured',
                setting=config.api_key_env,
            )
        try:
            base_url = config.resolved_base_url()
        except ValueError as exc:
            raise ModelProviderConfigurationError(
                config.name,
                'endpoint_not_configured',
                setting=config.base_url_env,
            ) from exc
        kwargs: dict[str, Any] = {'model': model_id, 'api_key': api_key}
        if base_url:
            kwargs['base_url'] = base_url
        if config.name == 'generic':
            # OpenAIEmbeddings tokenizes long inputs client-side by default and
            # sends integer token arrays. OpenAI-compatible servers such as
            # Ollama commonly implement the documented string-input wire shape
            # only, so keep tokenization behind the configured endpoint.
            kwargs['check_embedding_ctx_length'] = False
        if dimension is not None:
            kwargs['dimensions'] = dimension
        if client_factory is None:
            self._client = OpenAIEmbeddings(**kwargs)
        else:
            self._client = client_factory(**kwargs)
        self.provider_id = config.name
        self.model_id = model_id
        self.dimension = dimension
        self.credential_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()

    def embed_query(self, text: str) -> list[float]:
        return list(self._client.embed_query(text))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [list(vector) for vector in self._client.embed_documents(texts)]


class GeminiEmbeddingProviderAdapter:
    """Adapt the existing Gemini query embedding seam to the common contract."""

    provider_id = 'gemini'

    def __init__(
        self,
        embed_query: Callable[[str], Sequence[float]],
        *,
        embed_document: Callable[[str], Sequence[float]] | None = None,
        model_id: str = 'embedding-001',
        dimension: int | None = 3072,
    ) -> None:
        self._embed_query = embed_query
        self._embed_document = embed_document or embed_query
        self.model_id = model_id
        self.dimension = dimension

    def embed_query(self, text: str) -> list[float]:
        return list(self._embed_query(text))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [list(self._embed_document(text)) for text in texts]


class ConfiguredEmbeddingProviderProxy:
    """Resolve the deployment embedding provider at the call boundary."""

    def __init__(
        self,
        default: EmbeddingProvider,
        *,
        gemini_factory: Callable[[], EmbeddingProvider] | None = None,
    ) -> None:
        self._default = default
        self._gemini_factory = gemini_factory
        self._cache: dict[tuple[str, str, int | None, str, str], EmbeddingProvider] = {}

    def _resolve(self) -> EmbeddingProvider:
        provider = os.getenv('EMBEDDING_PROVIDER', self._default.provider_id).strip().lower()
        model = os.getenv('EMBEDDING_MODEL', '').strip()
        dimension_raw = os.getenv('EMBEDDING_DIMENSION', '').strip()
        dimension = int(dimension_raw) if dimension_raw else None
        if provider == self._default.provider_id and not model and dimension is None:
            return self._default
        if provider == 'gemini':
            if self._gemini_factory is None:
                raise ValueError('Gemini embedding adapter is not configured')
            return self._gemini_factory()
        if provider not in {'openai', 'openrouter', 'generic', 'deepseek', 'mimo'}:
            raise ValueError(f"Unsupported EMBEDDING_PROVIDER '{provider}'")
        if not model:
            raise ValueError('EMBEDDING_MODEL is required for a non-default embedding provider')
        config = get_openai_compatible_provider_config(provider)
        api_key = os.getenv(config.api_key_env, '').strip()
        if not api_key:
            raise ModelProviderConfigurationError(
                config.name,
                'credential_not_configured',
                setting=config.api_key_env,
            )
        try:
            base_url = config.resolved_base_url() or ''
        except ValueError as exc:
            raise ModelProviderConfigurationError(
                config.name,
                'endpoint_not_configured',
                setting=config.base_url_env,
            ) from exc
        key = (provider, model, dimension, base_url, hashlib.sha256(api_key.encode()).hexdigest())
        instance = self._cache.get(key)
        if instance is None:
            instance = OpenAICompatibleEmbeddingProviderAdapter(
                provider_id=provider,
                model_id=model,
                dimension=dimension,
            )
            self._cache[key] = instance
        return instance

    @property
    def provider_id(self) -> str:
        return self._resolve().provider_id

    @property
    def model_id(self) -> str:
        return self._resolve().model_id

    @property
    def dimension(self) -> int | None:
        return self._resolve().dimension

    def embed_query(self, text: str) -> list[float]:
        return self._resolve().embed_query(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._resolve().embed_documents(texts)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)
