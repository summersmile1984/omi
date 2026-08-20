"""Versioned vector projection routing and migration tooling.

Authoritative product documents remain outside this module. Vector records are
rebuildable projections, so provider/model/dimension/schema/namespace identity
is stamped on every new write and migration transitions are explicit.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from database.vector_store import VectorStore

PROJECTION_PROVIDER_KEY = 'projection_provider'
PROJECTION_MODEL_KEY = 'projection_model'
PROJECTION_DIMENSION_KEY = 'projection_dimension'
PROJECTION_SCHEMA_VERSION_KEY = 'projection_schema_version'
PROJECTION_NAMESPACE_VERSION_KEY = 'projection_namespace_version'
PROJECTION_LOGICAL_NAMESPACE_KEY = 'projection_logical_namespace'

PROJECTION_MODE_ENV = 'VECTOR_PROJECTION_MODE'
PROJECTION_MODE_SINGLE = 'single'
PROJECTION_MODE_DUAL_WRITE = 'dual_write'
PROJECTION_ACTIVE_VERSION_ENV = 'VECTOR_PROJECTION_ACTIVE_VERSION'
PROJECTION_TARGET_VERSION_ENV = 'VECTOR_PROJECTION_TARGET_VERSION'
PROJECTION_SCHEMA_VERSION_ENV = 'VECTOR_PROJECTION_SCHEMA_VERSION'
PROJECTION_DELETE_VERSIONS_ENV = 'VECTOR_PROJECTION_DELETE_VERSIONS'
VECTOR_PROJECTION_LOGICAL_NAMESPACES = (
    'ns1',
    'ns2',
    'workstream-association-v1',
    'ns_x',
    'ns3',
    'ns4',
    'ns_tchunks',
)

_VERSION_COMPONENT = re.compile(r'[^a-zA-Z0-9_-]+')


class EmbeddingIdentity(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int | None: ...


class ProjectionUnavailableError(RuntimeError):
    """Typed provider-backed projection failure; never equivalent to no hits."""

    def __init__(self, capability: str, reason: str, *, retryable: bool = True) -> None:
        self.capability = capability
        self.reason = reason
        self.retryable = retryable
        super().__init__(f'{capability} projection unavailable: {reason}')

    def as_dict(self) -> dict[str, Any]:
        return {
            'code': 'projection_unavailable',
            'capability': self.capability,
            'reason': self.reason,
            'retryable': self.retryable,
        }


@dataclass(frozen=True)
class ProjectionDescriptor:
    provider: str
    model: str
    dimension: int
    schema_version: int
    namespace_version: str

    def metadata(self, logical_namespace: str) -> dict[str, Any]:
        return {
            PROJECTION_PROVIDER_KEY: self.provider,
            PROJECTION_MODEL_KEY: self.model,
            PROJECTION_DIMENSION_KEY: self.dimension,
            PROJECTION_SCHEMA_VERSION_KEY: self.schema_version,
            PROJECTION_NAMESPACE_VERSION_KEY: self.namespace_version,
            PROJECTION_LOGICAL_NAMESPACE_KEY: logical_namespace,
        }


def describe_projection(
    embeddings: EmbeddingIdentity,
    *,
    dimension: int,
    namespace_version: str,
    schema_version: int,
    capability: str = 'vector_write',
) -> ProjectionDescriptor:
    """Build one validated projection identity for writes and API responses."""

    configured_dimension = embeddings.dimension
    if configured_dimension is not None and configured_dimension != dimension:
        raise ProjectionUnavailableError(
            capability,
            f'embedding dimension mismatch configured={configured_dimension} actual={dimension}',
            retryable=False,
        )
    if schema_version < 1:
        raise ProjectionUnavailableError(capability, 'projection schema version must be positive', retryable=False)
    return ProjectionDescriptor(
        provider=embeddings.provider_id,
        model=embeddings.model_id,
        dimension=dimension,
        schema_version=schema_version,
        namespace_version=_normalize_version(namespace_version),
    )


def describe_active_projection(
    embeddings: EmbeddingIdentity,
    *,
    dimension: int,
    capability: str = 'embedding',
    env: Mapping[str, str] | None = None,
) -> ProjectionDescriptor:
    """Resolve explicit active projection metadata without constructing a store.

    Capability endpoints use this boundary so returning an embedding can never
    silently invent the schema/namespace version that a consumer will persist.
    """

    values = os.environ if env is None else env
    active_raw = values.get(PROJECTION_ACTIVE_VERSION_ENV, '').strip()
    schema_raw = values.get(PROJECTION_SCHEMA_VERSION_ENV, '').strip()
    if not active_raw or not schema_raw:
        raise ProjectionUnavailableError(
            capability,
            'active projection version and schema version must be configured',
            retryable=False,
        )
    try:
        schema_version = int(schema_raw)
    except ValueError as error:
        raise ProjectionUnavailableError(capability, 'invalid projection schema version', retryable=False) from error
    return describe_projection(
        embeddings,
        dimension=dimension,
        namespace_version=active_raw,
        schema_version=schema_version,
        capability=capability,
    )


@dataclass(frozen=True)
class ProjectionRecord:
    id: str
    values: tuple[float, ...]
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class BackfillReport:
    namespace: str
    namespace_version: str
    attempted: int
    written: int
    batches: int


@dataclass(frozen=True)
class VerificationReport:
    namespace: str
    namespace_version: str
    expected: int
    present: int
    missing_ids: tuple[str, ...]
    mismatched_ids: tuple[str, ...]
    unexpected_count: int = 0

    @property
    def ready_to_switch(self) -> bool:
        return (
            self.expected == self.present
            and not self.missing_ids
            and not self.mismatched_ids
            and self.unexpected_count == 0
        )


class VersionedVectorStoreAdapter:
    """Route reads to one projection version and optionally dual-write another."""

    provider_id = 'versioned'

    def __init__(self, store: VectorStore, embeddings: EmbeddingIdentity) -> None:
        self._store = store
        self._embeddings = embeddings

    def _runtime(self) -> tuple[str, str, str | None, int]:
        mode = os.getenv(PROJECTION_MODE_ENV, PROJECTION_MODE_SINGLE).strip().lower() or PROJECTION_MODE_SINGLE
        if mode not in {PROJECTION_MODE_SINGLE, PROJECTION_MODE_DUAL_WRITE}:
            raise ProjectionUnavailableError('vector_write', f'unsupported projection mode {mode!r}', retryable=False)
        active = _normalize_version(os.getenv(PROJECTION_ACTIVE_VERSION_ENV, 'v1'))
        target_raw = os.getenv(PROJECTION_TARGET_VERSION_ENV, '').strip()
        target = _normalize_version(target_raw) if target_raw else None
        if mode == PROJECTION_MODE_DUAL_WRITE and (target is None or target == active):
            raise ProjectionUnavailableError(
                'vector_write', 'dual_write requires a distinct VECTOR_PROJECTION_TARGET_VERSION', retryable=False
            )
        try:
            schema_version = int(os.getenv(PROJECTION_SCHEMA_VERSION_ENV, '1'))
        except ValueError as error:
            raise ProjectionUnavailableError(
                'vector_write', 'invalid projection schema version', retryable=False
            ) from error
        if schema_version < 1:
            raise ProjectionUnavailableError(
                'vector_write', 'projection schema version must be positive', retryable=False
            )
        return mode, active, target, schema_version

    def descriptor(
        self, *, dimension: int, namespace_version: str, schema_version: int | None = None
    ) -> ProjectionDescriptor:
        _, _, _, runtime_schema = self._runtime()
        return describe_projection(
            self._embeddings,
            dimension=dimension,
            schema_version=schema_version or runtime_schema,
            namespace_version=_normalize_version(namespace_version),
        )

    def upsert(self, *, vectors: Sequence[Mapping[str, Any]], namespace: str) -> Any:
        if not vectors:
            return {'upserted_count': 0}
        mode, active, target, schema_version = self._runtime()
        result = self.write_version(
            vectors=vectors,
            namespace=namespace,
            namespace_version=active,
            schema_version=schema_version,
        )
        if mode == PROJECTION_MODE_DUAL_WRITE and target is not None:
            self.write_version(
                vectors=vectors,
                namespace=namespace,
                namespace_version=target,
                schema_version=schema_version,
            )
        return result

    def write_version(
        self,
        *,
        vectors: Sequence[Mapping[str, Any]],
        namespace: str,
        namespace_version: str,
        schema_version: int | None = None,
    ) -> Any:
        if not vectors:
            return {'upserted_count': 0}
        first_values = tuple(float(value) for value in vectors[0].get('values') or ())
        if not first_values:
            raise ProjectionUnavailableError('vector_write', 'vector values are empty', retryable=False)
        descriptor = self.descriptor(
            dimension=len(first_values),
            namespace_version=namespace_version,
            schema_version=schema_version,
        )
        enriched = []
        for vector in vectors:
            values = tuple(float(value) for value in vector.get('values') or ())
            if len(values) != descriptor.dimension:
                raise ProjectionUnavailableError(
                    'vector_write', 'vector batch dimensions are inconsistent', retryable=False
                )
            enriched.append(
                {
                    **dict(vector),
                    'values': list(values),
                    'metadata': {
                        **dict(vector.get('metadata') or {}),
                        **descriptor.metadata(namespace),
                    },
                }
            )
        return self._store.upsert(
            vectors=enriched,
            namespace=physical_namespace(namespace, descriptor.namespace_version),
        )

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
        _, active, _, _ = self._runtime()
        return self._store.query(
            vector=vector,
            top_k=top_k,
            namespace=physical_namespace(namespace, active),
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
        versions = self._deletion_versions()
        result: Any = None
        for version in versions:
            result = self._store.delete(
                namespace=physical_namespace(namespace, version),
                ids=ids,
                filter=filter,
            )
        return result

    def update(self, vector_id: str, *, set_metadata: Mapping[str, Any], namespace: str) -> Any:
        mode, active, target, schema_version = self._runtime()
        versions = [active, *([target] if mode == PROJECTION_MODE_DUAL_WRITE and target else [])]
        result: Any = None
        for version in versions:
            metadata = {
                **dict(set_metadata),
                PROJECTION_PROVIDER_KEY: self._embeddings.provider_id,
                PROJECTION_MODEL_KEY: self._embeddings.model_id,
                PROJECTION_SCHEMA_VERSION_KEY: schema_version,
                PROJECTION_NAMESPACE_VERSION_KEY: version,
                PROJECTION_LOGICAL_NAMESPACE_KEY: namespace,
            }
            if self._embeddings.dimension is not None:
                metadata[PROJECTION_DIMENSION_KEY] = self._embeddings.dimension
            result = self._store.update(
                vector_id,
                set_metadata=metadata,
                namespace=physical_namespace(namespace, version),
            )
        return result

    def list(self, *, prefix: str, namespace: str) -> Iterator[Sequence[str]]:
        _, active, _, _ = self._runtime()
        return self._store.list(prefix=prefix, namespace=physical_namespace(namespace, active))

    def fetch(self, *, ids: Sequence[str], namespace: str) -> Mapping[str, Any]:
        _, active, _, _ = self._runtime()
        return self.fetch_version(ids=ids, namespace=namespace, namespace_version=active)

    def count(self, *, namespace: str, filter: Mapping[str, Any]) -> int:
        _, active, _, _ = self._runtime()
        return self._store.count(
            namespace=physical_namespace(namespace, active),
            filter=filter,
        )

    def count_deletion_versions(self, *, namespace: str, filter: Mapping[str, Any]) -> int:
        """Count every projection version covered by privacy deletion."""

        return sum(
            self._store.count(
                namespace=physical_namespace(namespace, version),
                filter=filter,
            )
            for version in self._deletion_versions()
        )

    def fetch_version(self, *, ids: Sequence[str], namespace: str, namespace_version: str) -> Mapping[str, Any]:
        return self._store.fetch(ids=ids, namespace=physical_namespace(namespace, namespace_version))

    def count_version(self, *, namespace: str, namespace_version: str) -> int:
        return self._store.count(
            namespace=physical_namespace(namespace, namespace_version),
            filter={},
        )

    def _deletion_versions(self) -> tuple[str, ...]:
        _, active, target, _ = self._runtime()
        configured = [value.strip() for value in os.getenv(PROJECTION_DELETE_VERSIONS_ENV, 'v1').split(',')]
        versions = {_normalize_version(value) for value in configured if value}
        versions.add(active)
        if target:
            versions.add(target)
        return tuple(sorted(versions))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


class ProjectionMigrationTool:
    """Backfill, verify, switch, and roll back one logical namespace."""

    def __init__(self, store: VersionedVectorStoreAdapter) -> None:
        self._store = store

    def backfill(
        self,
        records: Iterable[ProjectionRecord],
        *,
        namespace: str,
        target_version: str,
        batch_size: int = 100,
    ) -> BackfillReport:
        if batch_size < 1:
            raise ValueError('batch_size must be positive')
        attempted = 0
        written = 0
        batches = 0
        batch: list[dict[str, Any]] = []
        for record in records:
            attempted += 1
            batch.append({'id': record.id, 'values': list(record.values), 'metadata': dict(record.metadata)})
            if len(batch) >= batch_size:
                self._store.write_version(vectors=batch, namespace=namespace, namespace_version=target_version)
                written += len(batch)
                batches += 1
                batch = []
        if batch:
            self._store.write_version(vectors=batch, namespace=namespace, namespace_version=target_version)
            written += len(batch)
            batches += 1
        return BackfillReport(namespace, _normalize_version(target_version), attempted, written, batches)

    def verify(
        self,
        records: Sequence[ProjectionRecord],
        *,
        namespace: str,
        target_version: str,
        batch_size: int = 100,
    ) -> VerificationReport:
        if batch_size < 1:
            raise ValueError('batch_size must be positive')
        expected_by_id = {record.id: record for record in records}
        present = 0
        missing: list[str] = []
        mismatched: list[str] = []
        ids = sorted(expected_by_id)
        normalized_target = _normalize_version(target_version)
        for start in range(0, len(ids), batch_size):
            batch_ids = ids[start : start + batch_size]
            fetched = self._store.fetch_version(
                ids=batch_ids,
                namespace=namespace,
                namespace_version=target_version,
            )
            raw_vectors = fetched.get('vectors', {})
            vectors = raw_vectors if isinstance(raw_vectors, Mapping) else {}
            for vector_id in batch_ids:
                actual = vectors.get(vector_id)
                if not isinstance(actual, Mapping):
                    missing.append(vector_id)
                    continue
                present += 1
                expected = expected_by_id[vector_id]
                descriptor = self._store.descriptor(
                    dimension=len(expected.values),
                    namespace_version=normalized_target,
                )
                actual_metadata = dict(actual.get('metadata') or {})
                projection_metadata = descriptor.metadata(namespace)
                projection_identity_matches = all(
                    actual_metadata.get(key) == value for key, value in projection_metadata.items()
                )
                if not projection_identity_matches or not _fetched_record_matches(expected, actual):
                    mismatched.append(vector_id)
        return VerificationReport(
            namespace=namespace,
            namespace_version=normalized_target,
            expected=len(expected_by_id),
            present=present,
            missing_ids=tuple(missing),
            mismatched_ids=tuple(mismatched),
            unexpected_count=max(
                0,
                self._store.count_version(namespace=namespace, namespace_version=normalized_target) - present,
            ),
        )

    @staticmethod
    def switch_manifest(report: VerificationReport) -> dict[str, str]:
        if not report.ready_to_switch:
            raise ProjectionUnavailableError('vector_switch', 'target verification is incomplete', retryable=False)
        current_active = _normalize_version(os.getenv(PROJECTION_ACTIVE_VERSION_ENV, 'v1'))
        return {
            PROJECTION_MODE_ENV: PROJECTION_MODE_SINGLE,
            PROJECTION_ACTIVE_VERSION_ENV: report.namespace_version,
            PROJECTION_TARGET_VERSION_ENV: '',
            PROJECTION_SCHEMA_VERSION_ENV: _schema_version_manifest(),
            PROJECTION_DELETE_VERSIONS_ENV: _deletion_versions_manifest(current_active, report.namespace_version),
        }

    @staticmethod
    def rollback_manifest(previous_version: str, *, abandoned_versions: Sequence[str] = ()) -> dict[str, str]:
        current_active = _normalize_version(os.getenv(PROJECTION_ACTIVE_VERSION_ENV, 'v1'))
        return {
            PROJECTION_MODE_ENV: PROJECTION_MODE_SINGLE,
            PROJECTION_ACTIVE_VERSION_ENV: _normalize_version(previous_version),
            PROJECTION_TARGET_VERSION_ENV: '',
            PROJECTION_SCHEMA_VERSION_ENV: _schema_version_manifest(),
            PROJECTION_DELETE_VERSIONS_ENV: _deletion_versions_manifest(
                previous_version,
                current_active,
                *abandoned_versions,
            ),
        }


def physical_namespace(logical_namespace: str, namespace_version: str) -> str:
    logical = logical_namespace.strip()
    if not logical:
        raise ValueError('logical namespace is required')
    version = _normalize_version(namespace_version)
    return logical if version == 'v1' else f'{logical}__{version}'


def require_projection_store(store: VectorStore | None, capability: str) -> VectorStore:
    if store is None:
        raise ProjectionUnavailableError(capability, 'vector store is not configured')
    return store


def _normalize_version(value: str) -> str:
    normalized = _VERSION_COMPONENT.sub('_', value.strip()).strip('_').lower()
    if not normalized:
        raise ValueError('projection namespace version is required')
    return normalized


def _deletion_versions_manifest(*versions: str) -> str:
    configured = os.getenv(PROJECTION_DELETE_VERSIONS_ENV, 'v1').split(',')
    normalized = {_normalize_version(value) for value in (*configured, *versions) if value.strip()}
    return ','.join(sorted(normalized))


def _schema_version_manifest() -> str:
    raw = os.getenv(PROJECTION_SCHEMA_VERSION_ENV, '1').strip()
    try:
        version = int(raw)
    except ValueError as error:
        raise ProjectionUnavailableError(
            'vector_switch', 'invalid projection schema version', retryable=False
        ) from error
    if version < 1:
        raise ProjectionUnavailableError('vector_switch', 'projection schema version must be positive', retryable=False)
    return str(version)


def _fetched_record_matches(expected: ProjectionRecord, record: Mapping[str, Any]) -> bool:
    metadata = dict(record.get('metadata') or {})
    for key in (
        PROJECTION_PROVIDER_KEY,
        PROJECTION_MODEL_KEY,
        PROJECTION_DIMENSION_KEY,
        PROJECTION_SCHEMA_VERSION_KEY,
        PROJECTION_NAMESPACE_VERSION_KEY,
        PROJECTION_LOGICAL_NAMESPACE_KEY,
    ):
        metadata.pop(key, None)
    try:
        actual_values = tuple(float(value) for value in record.get('values') or ())
    except (TypeError, ValueError):
        return False
    if dict(expected.metadata) != metadata or len(expected.values) != len(actual_values):
        return False
    expected_norm = math.sqrt(sum(value * value for value in expected.values))
    actual_norm = math.sqrt(sum(value * value for value in actual_values))
    if expected_norm == 0 or actual_norm == 0:
        return all(
            math.isclose(expected_value, actual_value, rel_tol=1e-6, abs_tol=1e-7)
            for expected_value, actual_value in zip(expected.values, actual_values)
        )
    return all(
        math.isclose(expected_value / expected_norm, actual_value / actual_norm, rel_tol=1e-6, abs_tol=1e-7)
        for expected_value, actual_value in zip(expected.values, actual_values)
    )
