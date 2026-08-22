"""Provider-neutral vector-store boundary with Pinecone and Qdrant adapters.

The interface intentionally mirrors the subset of the Pinecone index contract
used by ``database.vector_db``. This lets the existing call sites migrate as one
unit while a Qdrant deployment can serve the same namespaces and record shape.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

try:
    from qdrant_client import QdrantClient, models  # pyright: ignore[reportMissingImports]
except ModuleNotFoundError:  # Qdrant is an optional deployment adapter.
    QdrantClient: Any = None
    models: Any = None


def _load_qdrant_models() -> Any:
    """Load the optional Qdrant SDK only when a Qdrant operation is selected."""

    global QdrantClient, models
    if models is not None and QdrantClient is not None:
        return models
    try:
        from qdrant_client import QdrantClient as qdrant_client, models as qdrant_models
    except ModuleNotFoundError as exc:
        raise RuntimeError('Qdrant vector-store adapter requires the qdrant-client package') from exc
    QdrantClient = qdrant_client
    models = qdrant_models
    return models


_ORIGINAL_ID_PAYLOAD_KEY = '__omi_vector_id'
_COLLECTION_COMPONENT = re.compile(r'[^a-zA-Z0-9_-]+')


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, *, vectors: Sequence[Mapping[str, Any]], namespace: str) -> Any: ...

    def query(
        self,
        *,
        vector: Sequence[float],
        top_k: int,
        namespace: str,
        include_metadata: bool = False,
        include_values: bool = False,
        filter: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...

    def delete(
        self,
        *,
        namespace: str,
        ids: Sequence[str] | None = None,
        filter: Mapping[str, Any] | None = None,
    ) -> Any: ...

    def update(self, vector_id: str, *, set_metadata: Mapping[str, Any], namespace: str) -> Any: ...

    def list(self, *, prefix: str, namespace: str) -> Iterator[Sequence[str]]: ...

    def count(self, *, namespace: str, filter: Mapping[str, Any]) -> int: ...

    def fetch(self, *, ids: Sequence[str], namespace: str) -> Mapping[str, Any]: ...


class PineconeVectorStoreAdapter:
    """Typed adapter over the existing Pinecone Index instance."""

    provider_id = 'pinecone'

    def __init__(self, index: Any) -> None:
        self._index = index

    def upsert(self, *, vectors: Sequence[Mapping[str, Any]], namespace: str) -> Any:
        return self._index.upsert(vectors=vectors, namespace=namespace)

    def query(
        self,
        *,
        vector: Sequence[float],
        top_k: int,
        namespace: str,
        include_metadata: bool = False,
        include_values: bool = False,
        filter: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return self._index.query(
            vector=vector,
            top_k=top_k,
            namespace=namespace,
            include_metadata=include_metadata,
            include_values=include_values,
            filter=filter,
        )

    def delete(
        self,
        *,
        namespace: str,
        ids: Sequence[str] | None = None,
        filter: Mapping[str, Any] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {'namespace': namespace}
        if ids is not None:
            kwargs['ids'] = ids
        if filter is not None:
            kwargs['filter'] = filter
        return self._index.delete(**kwargs)

    def update(self, vector_id: str, *, set_metadata: Mapping[str, Any], namespace: str) -> Any:
        return self._index.update(vector_id, set_metadata=set_metadata, namespace=namespace)

    def list(self, *, prefix: str, namespace: str) -> Iterator[Sequence[str]]:
        return self._index.list(prefix=prefix, namespace=namespace)

    def count(self, *, namespace: str, filter: Mapping[str, Any]) -> int:
        """Count filtered vectors without issuing a similarity query."""

        stats = self._index.describe_index_stats(**({'filter': dict(filter)} if filter else {}))
        namespaces = stats.get('namespaces', {}) if isinstance(stats, Mapping) else getattr(stats, 'namespaces', {})
        namespace_stats = namespaces.get(namespace, {}) if isinstance(namespaces, Mapping) else {}
        if isinstance(namespace_stats, Mapping):
            return int(namespace_stats.get('vector_count', 0))
        return int(getattr(namespace_stats, 'vector_count', 0))

    def fetch(self, *, ids: Sequence[str], namespace: str) -> Mapping[str, Any]:
        return self._index.fetch(ids=list(ids), namespace=namespace)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._index, name)


class QdrantVectorStoreAdapter:
    """Qdrant implementation of the vector-store contract.

    Arbitrary string ids are mapped to deterministic UUIDs because Qdrant point
    ids accept integers/UUIDs. The original id remains in payload and is returned
    to callers, so repository identity does not change across stores.
    """

    provider_id = 'qdrant'

    def __init__(self, client: Any, *, collection_prefix: str = 'omi') -> None:
        self._client = client
        self._collection_prefix = _collection_name(collection_prefix)

    def _collection(self, namespace: str) -> str:
        return f'{self._collection_prefix}_{_collection_name(namespace)}'

    def _point_id(self, collection: str, vector_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f'omi-vector:{collection}:{vector_id}'))

    def _ensure_collection(self, collection: str, dimension: int) -> None:
        qdrant_models = _load_qdrant_models()
        if self._client.collection_exists(collection):
            return
        self._client.create_collection(
            collection_name=collection,
            vectors_config=qdrant_models.VectorParams(size=dimension, distance=qdrant_models.Distance.COSINE),
        )

    def upsert(self, *, vectors: Sequence[Mapping[str, Any]], namespace: str) -> Any:
        if not vectors:
            return {'upserted_count': 0}
        collection = self._collection(namespace)
        first_values = list(vectors[0].get('values') or [])
        if not first_values:
            raise ValueError('Qdrant vector upsert requires non-empty values')
        self._ensure_collection(collection, len(first_values))
        points = []
        for vector in vectors:
            vector_id = str(vector['id'])
            values = list(vector['values'])
            if len(values) != len(first_values):
                raise ValueError('Qdrant vector batch contains inconsistent dimensions')
            payload = dict(vector.get('metadata') or {})
            payload[_ORIGINAL_ID_PAYLOAD_KEY] = vector_id
            points.append(
                models.PointStruct(
                    id=self._point_id(collection, vector_id),
                    vector=values,
                    payload=payload,
                )
            )
        result = self._client.upsert(collection_name=collection, points=points, wait=True)
        return {'upserted_count': len(points), 'status': str(result.status)}

    def query(
        self,
        *,
        vector: Sequence[float],
        top_k: int,
        namespace: str,
        include_metadata: bool = False,
        include_values: bool = False,
        filter: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        _load_qdrant_models()
        collection = self._collection(namespace)
        if not self._client.collection_exists(collection):
            return {'matches': []}
        matches = self._client.search(
            collection_name=collection,
            query_vector=list(vector),
            query_filter=_qdrant_filter(filter),
            limit=top_k,
            # Payload is always needed to translate the deterministic Qdrant UUID
            # back to the repository's original string id. Metadata is stripped
            # from the returned match when the caller did not request it.
            with_payload=True,
            with_vectors=include_values,
        )
        return {
            'matches': [
                _qdrant_match(match, include_metadata=include_metadata, include_values=include_values)
                for match in matches
            ]
        }

    def delete(
        self,
        *,
        namespace: str,
        ids: Sequence[str] | None = None,
        filter: Mapping[str, Any] | None = None,
    ) -> Any:
        qdrant_models = _load_qdrant_models()
        collection = self._collection(namespace)
        if not self._client.collection_exists(collection):
            return {'deleted_count': 0}
        if ids is not None:
            selector: Any = qdrant_models.PointIdsList(
                points=[self._point_id(collection, str(vector_id)) for vector_id in ids]
            )
        elif filter is not None:
            selector = qdrant_models.FilterSelector(filter=_qdrant_filter(filter) or qdrant_models.Filter())
        else:
            raise ValueError('Qdrant delete requires ids or filter')
        result = self._client.delete(collection_name=collection, points_selector=selector, wait=True)
        return {'status': str(result.status)}

    def update(self, vector_id: str, *, set_metadata: Mapping[str, Any], namespace: str) -> Any:
        collection = self._collection(namespace)
        if not self._client.collection_exists(collection):
            return {'updated_count': 0}
        payload = dict(set_metadata)
        payload[_ORIGINAL_ID_PAYLOAD_KEY] = vector_id
        result = self._client.set_payload(
            collection_name=collection,
            payload=payload,
            points=[self._point_id(collection, vector_id)],
            wait=True,
        )
        return {'updated_count': 1, 'status': str(result.status)}

    def list(self, *, prefix: str, namespace: str) -> Iterator[Sequence[str]]:
        collection = self._collection(namespace)
        if not self._client.collection_exists(collection):
            return
        offset: Any = None
        while True:
            records, offset = self._client.scroll(
                collection_name=collection,
                limit=256,
                offset=offset,
                with_payload=[_ORIGINAL_ID_PAYLOAD_KEY],
                with_vectors=False,
            )
            ids = [
                str(record.payload.get(_ORIGINAL_ID_PAYLOAD_KEY))
                for record in records
                if record.payload and str(record.payload.get(_ORIGINAL_ID_PAYLOAD_KEY, '')).startswith(prefix)
            ]
            if ids:
                yield ids
            if offset is None:
                break

    def count(self, *, namespace: str, filter: Mapping[str, Any]) -> int:
        _load_qdrant_models()
        collection = self._collection(namespace)
        if not self._client.collection_exists(collection):
            return 0
        result = self._client.count(
            collection_name=collection,
            count_filter=_qdrant_filter(filter),
            exact=True,
        )
        return int(result.count)

    def fetch(self, *, ids: Sequence[str], namespace: str) -> Mapping[str, Any]:
        collection = self._collection(namespace)
        if not ids or not self._client.collection_exists(collection):
            return {'vectors': {}}
        records = self._client.retrieve(
            collection_name=collection,
            ids=[self._point_id(collection, str(vector_id)) for vector_id in ids],
            with_payload=True,
            with_vectors=True,
        )
        vectors: dict[str, Any] = {}
        for record in records:
            payload = dict(record.payload or {})
            original_id = str(payload.pop(_ORIGINAL_ID_PAYLOAD_KEY, record.id))
            vectors[original_id] = {
                'id': original_id,
                'values': list(record.vector or []),
                'metadata': payload,
            }
        return {'vectors': vectors}


def create_qdrant_vector_store_from_env() -> QdrantVectorStoreAdapter:
    _load_qdrant_models()
    url = os.getenv('QDRANT_URL', '').strip()
    path = os.getenv('QDRANT_PATH', '').strip()
    api_key = os.getenv('QDRANT_API_KEY', '').strip() or None
    if url:
        client = QdrantClient(url=url, api_key=api_key)
    elif path:
        client = QdrantClient(path=path)
    else:
        raise ValueError('VECTOR_STORE_PROVIDER=qdrant requires QDRANT_URL or QDRANT_PATH')
    return QdrantVectorStoreAdapter(
        client,
        collection_prefix=os.getenv('QDRANT_COLLECTION_PREFIX', 'omi').strip() or 'omi',
    )


def _collection_name(value: str) -> str:
    normalized = _COLLECTION_COMPONENT.sub('_', value.strip()).strip('_')
    if not normalized:
        raise ValueError('vector-store collection component cannot be empty')
    return normalized


def _qdrant_filter(raw: Mapping[str, Any] | None) -> Any | None:
    qdrant_models = _load_qdrant_models()
    if not raw:
        return None
    must: list[Any] = []
    should: list[Any] = []
    must_not: list[Any] = []
    for key, value in raw.items():
        if key in {'$and', '$or'}:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ValueError(f'{key} vector filter requires a list of filter objects')
            target = must if key == '$and' else should
            for item in value:
                if not isinstance(item, Mapping):
                    raise ValueError(f'{key} vector filter entries must be filter objects')
                nested = _qdrant_filter(item)
                if nested is not None:
                    target.append(nested)
            continue
        condition, negative = _qdrant_field_condition(key, value)
        (must_not if negative else must).append(condition)
    return qdrant_models.Filter(must=must or None, should=should or None, must_not=must_not or None)


def _qdrant_field_condition(key: str, value: Any) -> tuple[Any, bool]:
    qdrant_models = _load_qdrant_models()
    if not isinstance(value, Mapping):
        return qdrant_models.FieldCondition(key=key, match=qdrant_models.MatchValue(value=value)), False
    if '$in' in value:
        return qdrant_models.FieldCondition(key=key, match=qdrant_models.MatchAny(any=list(value['$in']))), False
    if '$nin' in value:
        return qdrant_models.FieldCondition(key=key, match=qdrant_models.MatchAny(any=list(value['$nin']))), True
    if '$ne' in value:
        return qdrant_models.FieldCondition(key=key, match=qdrant_models.MatchValue(value=value['$ne'])), True
    if '$eq' in value:
        return qdrant_models.FieldCondition(key=key, match=qdrant_models.MatchValue(value=value['$eq'])), False
    if '$exists' in value:
        exists = value['$exists']
        if not isinstance(exists, bool):
            raise ValueError(f'$exists vector filter for {key!r} requires a boolean')
        condition = qdrant_models.IsEmptyCondition(is_empty=qdrant_models.PayloadField(key=key))
        return condition, exists
    range_values = {
        target: value[source]
        for source, target in (('$gt', 'gt'), ('$gte', 'gte'), ('$lt', 'lt'), ('$lte', 'lte'))
        if source in value
    }
    if range_values:
        return qdrant_models.FieldCondition(key=key, range=qdrant_models.Range(**range_values)), False
    raise ValueError(f'Unsupported vector filter operator for {key!r}')


def _qdrant_match(match: Any, *, include_metadata: bool, include_values: bool) -> dict[str, Any]:
    payload = dict(match.payload or {})
    vector_id = str(payload.pop(_ORIGINAL_ID_PAYLOAD_KEY, match.id))
    result: dict[str, Any] = {'id': vector_id, 'score': float(match.score)}
    if include_metadata:
        result['metadata'] = payload
    if include_values:
        result['values'] = list(match.vector or [])
    return result
