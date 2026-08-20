#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Fail-closed operator CLI for versioned vector projection migrations.

The input is an immutable JSONL export from the authoritative product store;
this command never treats the current vector index as authority. Backfill writes
an embedding receipt that binds the exact source bytes, vectors, provider/model,
dimension, schema, namespace, and target version. Verify and switch require both
files, so a stale or edited export cannot authorize a cutover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from database.vector_projection import (
    PROJECTION_ACTIVE_VERSION_ENV,
    PROJECTION_DELETE_VERSIONS_ENV,
    PROJECTION_DIMENSION_KEY,
    PROJECTION_LOGICAL_NAMESPACE_KEY,
    PROJECTION_MODE_ENV,
    PROJECTION_MODEL_KEY,
    PROJECTION_NAMESPACE_VERSION_KEY,
    PROJECTION_PROVIDER_KEY,
    PROJECTION_SCHEMA_VERSION_ENV,
    PROJECTION_SCHEMA_VERSION_KEY,
    PROJECTION_TARGET_VERSION_ENV,
    ProjectionMigrationTool,
    ProjectionRecord,
    ProjectionUnavailableError,
    VerificationReport,
    VersionedVectorStoreAdapter,
)
from database.vector_store import PineconeVectorStoreAdapter, create_qdrant_vector_store_from_env

RECEIPT_FORMAT = 'omi-vector-projection-receipt-v1'
SWITCH_PLAN_FORMAT = 'omi-vector-projection-switch-plan-v1'
PROJECTION_REQUIRED_NAMESPACES_ENV = 'VECTOR_PROJECTION_REQUIRED_NAMESPACES'
AUTHORITY_RECORD_KEYS = frozenset({'id', 'content', 'metadata'})
RECEIPT_HEADER_KEYS = frozenset(
    {
        'type',
        'format',
        'source_sha256',
        'source_record_count',
        'namespace',
        'target_version',
        'embedding_provider',
        'embedding_model',
        'embedding_dimension',
        'projection_schema_version',
        'empty_export_acknowledged',
    }
)
RECEIPT_RECORD_KEYS = frozenset({'type', 'id', 'values', 'metadata'})
SWITCH_PLAN_KEYS = frozenset({'format', 'projections'})
SWITCH_PLAN_ITEM_KEYS = frozenset({'namespace', 'records', 'receipt'})
_SHA256 = re.compile(r'[0-9a-f]{64}')
_PROJECTION_VERSION = re.compile(r'[a-z0-9][a-z0-9_-]*')
PROJECTION_METADATA_KEYS = frozenset(
    {
        PROJECTION_PROVIDER_KEY,
        PROJECTION_MODEL_KEY,
        PROJECTION_DIMENSION_KEY,
        PROJECTION_SCHEMA_VERSION_KEY,
        PROJECTION_NAMESPACE_VERSION_KEY,
        PROJECTION_LOGICAL_NAMESPACE_KEY,
    }
)


class EmbeddingRuntime(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int | None: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class AuthorityRecord:
    id: str
    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ReceiptHeader:
    format: str
    source_sha256: str
    source_record_count: int
    namespace: str
    target_version: str
    embedding_provider: str
    embedding_model: str
    embedding_dimension: int
    projection_schema_version: int
    empty_export_acknowledged: bool


@dataclass(frozen=True)
class ProjectionReceipt:
    header: ReceiptHeader
    records: tuple[ProjectionRecord, ...]


@dataclass(frozen=True)
class SwitchPlanEntry:
    namespace: str
    records_path: Path
    receipt_path: Path


@dataclass(frozen=True)
class _ReceiptEmbeddingIdentity:
    provider_id: str
    model_id: str
    dimension: int


class MigrationCliError(RuntimeError):
    """Safe operator-facing contract error."""


def load_authority_records(
    path: Path,
    *,
    allow_empty: bool = False,
) -> tuple[tuple[AuthorityRecord, ...], str]:
    raw = path.read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    records: list[AuthorityRecord] = []
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(raw.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            value = _strict_json_loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MigrationCliError(f'{path}:{line_number}: invalid UTF-8 JSON object') from error
        if not isinstance(value, dict):
            raise MigrationCliError(f'{path}:{line_number}: expected one JSON object per line')
        unknown = sorted(set(value) - AUTHORITY_RECORD_KEYS)
        if unknown:
            raise MigrationCliError(f'{path}:{line_number}: unknown fields: {", ".join(unknown)}')
        record_id = value.get('id')
        content = value.get('content')
        metadata = value.get('metadata', {})
        if not isinstance(record_id, str) or not record_id.strip():
            raise MigrationCliError(f'{path}:{line_number}: id must be a non-empty string')
        if record_id in seen_ids:
            raise MigrationCliError(f'{path}:{line_number}: duplicate id {record_id!r}')
        if not isinstance(content, str) or not content.strip():
            raise MigrationCliError(f'{path}:{line_number}: content must be a non-empty string')
        if not isinstance(metadata, dict):
            raise MigrationCliError(f'{path}:{line_number}: metadata must be an object')
        forbidden_metadata = sorted(PROJECTION_METADATA_KEYS & set(metadata))
        if forbidden_metadata:
            raise MigrationCliError(
                f'{path}:{line_number}: authority metadata may not set projection fields: '
                f'{", ".join(forbidden_metadata)}'
            )
        seen_ids.add(record_id)
        records.append(AuthorityRecord(id=record_id, content=content, metadata=dict(metadata)))
    if not records and not allow_empty:
        raise MigrationCliError(f'{path}: authoritative export contains no records')
    return tuple(records), source_sha256


def backfill_from_authority(
    *,
    records_path: Path,
    receipt_path: Path,
    namespace: str,
    target_version: str,
    store: VersionedVectorStoreAdapter,
    embeddings: EmbeddingRuntime,
    batch_size: int,
    allow_empty: bool = False,
) -> dict[str, Any]:
    _require_new_output(receipt_path)
    _validate_batch_size(batch_size)
    if not namespace.strip():
        raise MigrationCliError('namespace must be a non-empty string')
    _validate_migration_runtime(target_version)
    _validate_embedding_runtime_identity(embeddings)
    authority, source_sha256 = load_authority_records(records_path, allow_empty=allow_empty)
    configured_dimension = embeddings.dimension
    if authority:
        vectors: list[list[float]] = []
        for start in range(0, len(authority), batch_size):
            batch = authority[start : start + batch_size]
            embedded = embeddings.embed_documents([record.content for record in batch])
            if len(embedded) != len(batch):
                raise MigrationCliError('embedding provider returned a different number of vectors than source records')
            vectors.extend(embedded)
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1 or not dimensions or next(iter(dimensions)) < 1:
            raise MigrationCliError('embedding provider returned empty or inconsistent vector dimensions')
        if any(not _is_finite_number(value) for vector in vectors for value in vector):
            raise MigrationCliError('embedding provider returned a non-finite or non-numeric vector value')
        dimension = next(iter(dimensions))
        if configured_dimension is not None and configured_dimension != dimension:
            raise MigrationCliError(
                f'embedding dimension mismatch configured={configured_dimension} actual={dimension}'
            )
    else:
        if configured_dimension is None:
            raise MigrationCliError('empty export requires an explicit embedding dimension')
        dimension = configured_dimension
        vectors = []
    projection_records = tuple(
        ProjectionRecord(
            id=record.id,
            values=tuple(float(value) for value in vector),
            metadata=record.metadata,
        )
        for record, vector in zip(authority, vectors)
    )
    report = ProjectionMigrationTool(store).backfill(
        projection_records,
        namespace=namespace,
        target_version=target_version,
        batch_size=batch_size,
    )
    schema_version = _positive_int_env(PROJECTION_SCHEMA_VERSION_ENV)
    header = ReceiptHeader(
        format=RECEIPT_FORMAT,
        source_sha256=source_sha256,
        source_record_count=len(authority),
        namespace=report.namespace,
        target_version=report.namespace_version,
        embedding_provider=embeddings.provider_id,
        embedding_model=embeddings.model_id,
        embedding_dimension=dimension,
        projection_schema_version=schema_version,
        empty_export_acknowledged=not authority,
    )
    _write_receipt(receipt_path, ProjectionReceipt(header=header, records=projection_records))
    return {'status': 'backfilled', **asdict(report), 'receipt': str(receipt_path), 'source_sha256': source_sha256}


def verify_receipt(
    *,
    records_path: Path,
    receipt_path: Path,
    store_factory: Any,
    batch_size: int,
) -> VerificationReport:
    _validate_batch_size(batch_size)
    receipt = load_receipt(receipt_path)
    authority, source_sha256 = load_authority_records(
        records_path,
        allow_empty=receipt.header.empty_export_acknowledged,
    )
    if source_sha256 != receipt.header.source_sha256:
        raise MigrationCliError('authoritative export SHA-256 does not match the backfill receipt')
    if len(authority) != receipt.header.source_record_count:
        raise MigrationCliError('authoritative export count does not match the backfill receipt')
    if [record.id for record in authority] != [record.id for record in receipt.records]:
        raise MigrationCliError('authoritative export ids/order do not match the backfill receipt')
    _validate_runtime_projection_contract(receipt.header)
    identity = _ReceiptEmbeddingIdentity(
        provider_id=receipt.header.embedding_provider,
        model_id=receipt.header.embedding_model,
        dimension=receipt.header.embedding_dimension,
    )
    store = store_factory(identity)
    return ProjectionMigrationTool(store).verify(
        receipt.records,
        namespace=receipt.header.namespace,
        target_version=receipt.header.target_version,
        batch_size=batch_size,
    )


def switch_from_receipt(
    *,
    records_path: Path,
    receipt_path: Path,
    env_output: Path,
    store_factory: Any,
    batch_size: int,
) -> dict[str, str]:
    _require_new_output(env_output)
    receipt = load_receipt(receipt_path)
    required_namespaces = _required_projection_namespaces()
    if required_namespaces != {receipt.header.namespace}:
        raise MigrationCliError(
            'single-receipt switch is allowed only when VECTOR_PROJECTION_REQUIRED_NAMESPACES '
            'declares exactly that namespace; use a switch plan for a global projection version'
        )
    report = verify_receipt(
        records_path=records_path,
        receipt_path=receipt_path,
        store_factory=store_factory,
        batch_size=batch_size,
    )
    manifest = ProjectionMigrationTool.switch_manifest(report)
    _write_env_manifest(env_output, manifest)
    return manifest


def switch_from_plan(
    *,
    plan_path: Path,
    env_output: Path,
    store_factory: Any,
    batch_size: int,
) -> dict[str, str]:
    _require_new_output(env_output)
    entries = load_switch_plan(plan_path)
    required_namespaces = _required_projection_namespaces()
    planned_namespaces = {entry.namespace for entry in entries}
    if planned_namespaces != required_namespaces:
        missing = sorted(required_namespaces - planned_namespaces)
        extra = sorted(planned_namespaces - required_namespaces)
        raise MigrationCliError(
            'switch plan namespace set does not match VECTOR_PROJECTION_REQUIRED_NAMESPACES '
            f'(missing={missing}, extra={extra})'
        )
    reports: list[VerificationReport] = []
    target_versions: set[str] = set()
    for entry in entries:
        receipt = load_receipt(entry.receipt_path)
        if receipt.header.namespace != entry.namespace:
            raise MigrationCliError(
                f'switch plan namespace {entry.namespace!r} does not match receipt namespace '
                f'{receipt.header.namespace!r}'
            )
        target_versions.add(receipt.header.target_version)
        reports.append(
            verify_receipt(
                records_path=entry.records_path,
                receipt_path=entry.receipt_path,
                store_factory=store_factory,
                batch_size=batch_size,
            )
        )
    if len(target_versions) != 1:
        raise MigrationCliError('every switch-plan receipt must target the same projection version')
    combined = VerificationReport(
        namespace=','.join(sorted(required_namespaces)),
        namespace_version=next(iter(target_versions)),
        expected=sum(report.expected for report in reports),
        present=sum(report.present for report in reports),
        missing_ids=tuple(f'{report.namespace}:{record_id}' for report in reports for record_id in report.missing_ids),
        mismatched_ids=tuple(
            f'{report.namespace}:{record_id}' for report in reports for record_id in report.mismatched_ids
        ),
        unexpected_count=sum(report.unexpected_count for report in reports),
    )
    manifest = ProjectionMigrationTool.switch_manifest(combined)
    _write_env_manifest(env_output, manifest)
    return manifest


def rollback_manifest(
    *,
    previous_version: str,
    abandoned_versions: Sequence[str],
    env_output: Path,
) -> dict[str, str]:
    _require_new_output(env_output)
    _validate_rollback_runtime(previous_version, abandoned_versions)
    manifest = ProjectionMigrationTool.rollback_manifest(
        previous_version,
        abandoned_versions=tuple(abandoned_versions),
    )
    _write_env_manifest(env_output, manifest)
    return manifest


def load_receipt(path: Path) -> ProjectionReceipt:
    lines = [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not lines:
        raise MigrationCliError(f'{path}: receipt must contain a header')
    try:
        raw_header = _strict_json_loads(lines[0])
        raw_records = [_strict_json_loads(line) for line in lines[1:]]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationCliError(f'{path}: invalid receipt JSONL') from error
    if not isinstance(raw_header, dict) or raw_header.get('type') != 'header':
        raise MigrationCliError(f'{path}: first receipt line must be a header')
    unknown_header = sorted(set(raw_header) - RECEIPT_HEADER_KEYS)
    if unknown_header:
        raise MigrationCliError(f'{path}: unknown receipt header fields: {", ".join(unknown_header)}')
    try:
        header = ReceiptHeader(**{key: value for key, value in raw_header.items() if key != 'type'})
    except TypeError as error:
        raise MigrationCliError(f'{path}: invalid receipt header') from error
    if header.format != RECEIPT_FORMAT:
        raise MigrationCliError(f'{path}: unsupported receipt format {header.format!r}')
    _validate_receipt_header(path, header)
    records: list[ProjectionRecord] = []
    seen_ids: set[str] = set()
    for line_number, value in enumerate(raw_records, 2):
        if not isinstance(value, dict) or value.get('type') != 'record':
            raise MigrationCliError(f'{path}:{line_number}: expected receipt record')
        unknown_record = sorted(set(value) - RECEIPT_RECORD_KEYS)
        if unknown_record:
            raise MigrationCliError(f'{path}:{line_number}: unknown receipt record fields: {", ".join(unknown_record)}')
        record_id = value.get('id')
        values = value.get('values')
        metadata = value.get('metadata')
        if not isinstance(record_id, str) or not record_id or record_id in seen_ids:
            raise MigrationCliError(f'{path}:{line_number}: invalid or duplicate receipt id')
        if not isinstance(values, list) or len(values) != header.embedding_dimension:
            raise MigrationCliError(f'{path}:{line_number}: receipt vector dimension mismatch')
        if not all(
            isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item))
            for item in values
        ):
            raise MigrationCliError(f'{path}:{line_number}: receipt vector values must be finite numbers')
        if not isinstance(metadata, dict):
            raise MigrationCliError(f'{path}:{line_number}: receipt metadata must be an object')
        forbidden_metadata = sorted(PROJECTION_METADATA_KEYS & set(metadata))
        if forbidden_metadata:
            raise MigrationCliError(
                f'{path}:{line_number}: receipt metadata may not set projection fields: '
                f'{", ".join(forbidden_metadata)}'
            )
        seen_ids.add(record_id)
        records.append(
            ProjectionRecord(
                id=record_id,
                values=tuple(float(item) for item in values),
                metadata=dict(metadata),
            )
        )
    if len(records) != header.source_record_count:
        raise MigrationCliError(f'{path}: receipt record count does not match its header')
    return ProjectionReceipt(header=header, records=tuple(records))


def load_switch_plan(path: Path) -> tuple[SwitchPlanEntry, ...]:
    try:
        value = _strict_json_loads(path.read_text(encoding='utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationCliError(f'{path}: invalid switch plan JSON') from error
    if not isinstance(value, dict):
        raise MigrationCliError(f'{path}: switch plan must be one JSON object')
    unknown = sorted(set(value) - SWITCH_PLAN_KEYS)
    if unknown:
        raise MigrationCliError(f'{path}: unknown switch plan fields: {", ".join(unknown)}')
    if value.get('format') != SWITCH_PLAN_FORMAT:
        raise MigrationCliError(f'{path}: unsupported switch plan format')
    projections = value.get('projections')
    if not isinstance(projections, list) or not projections:
        raise MigrationCliError(f'{path}: switch plan projections must be a non-empty list')
    entries: list[SwitchPlanEntry] = []
    seen_namespaces: set[str] = set()
    for index, item in enumerate(projections, 1):
        if not isinstance(item, dict):
            raise MigrationCliError(f'{path}: projection item {index} must be an object')
        unknown_item = sorted(set(item) - SWITCH_PLAN_ITEM_KEYS)
        if unknown_item:
            raise MigrationCliError(f'{path}: projection item {index} has unknown fields: {", ".join(unknown_item)}')
        namespace = item.get('namespace')
        records = item.get('records')
        receipt = item.get('receipt')
        if not _is_nonempty_string(namespace):
            raise MigrationCliError(f'{path}: projection item {index} namespace must be a non-empty string')
        assert isinstance(namespace, str)
        if namespace in seen_namespaces:
            raise MigrationCliError(f'{path}: duplicate switch plan namespace {namespace!r}')
        if not _is_nonempty_string(records) or not _is_nonempty_string(receipt):
            raise MigrationCliError(f'{path}: projection item {index} records/receipt must be non-empty paths')
        assert isinstance(records, str)
        assert isinstance(receipt, str)
        seen_namespaces.add(namespace)
        entries.append(
            SwitchPlanEntry(
                namespace=namespace,
                records_path=_resolve_plan_path(path, records),
                receipt_path=_resolve_plan_path(path, receipt),
            )
        )
    return tuple(entries)


def _write_receipt(path: Path, receipt: ProjectionReceipt) -> None:
    lines = [json.dumps({'type': 'header', **asdict(receipt.header)}, sort_keys=True, separators=(',', ':'))]
    lines.extend(
        json.dumps(
            {'type': 'record', 'id': record.id, 'values': list(record.values), 'metadata': dict(record.metadata)},
            sort_keys=True,
            separators=(',', ':'),
        )
        for record in receipt.records
    )
    _atomic_write(path, '\n'.join(lines) + '\n')


def _write_env_manifest(path: Path, manifest: Mapping[str, str]) -> None:
    ordered_keys = (
        PROJECTION_MODE_ENV,
        PROJECTION_ACTIVE_VERSION_ENV,
        PROJECTION_TARGET_VERSION_ENV,
        PROJECTION_SCHEMA_VERSION_ENV,
        PROJECTION_DELETE_VERSIONS_ENV,
    )
    lines = [f'{key}={manifest[key]}' for key in ordered_keys if key in manifest]
    _atomic_write(path, '\n'.join(lines) + '\n')


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise MigrationCliError(f'refusing to overwrite existing output {path}') from error
        os.unlink(temporary)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _require_new_output(path: Path) -> None:
    if path.exists():
        raise MigrationCliError(f'refusing to overwrite existing output {path}')


def _positive_int_env(name: str) -> int:
    raw = os.getenv(name, '').strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise MigrationCliError(f'{name} must be an explicit positive integer') from error
    if value < 1:
        raise MigrationCliError(f'{name} must be an explicit positive integer')
    return value


def _strict_json_loads(value: str | bytes) -> Any:
    return json.loads(value, parse_constant=lambda constant: _reject_json_constant(constant))


def _reject_json_constant(constant: str) -> Any:
    raise json.JSONDecodeError(f'non-standard numeric constant {constant}', constant, 0)


def _validate_receipt_header(path: Path, header: ReceiptHeader) -> None:
    if not _is_nonempty_string(header.source_sha256) or not _SHA256.fullmatch(header.source_sha256):
        raise MigrationCliError(f'{path}: receipt source_sha256 must be a lowercase SHA-256 digest')
    if not _is_nonnegative_integer(header.source_record_count):
        raise MigrationCliError(f'{path}: receipt source_record_count must be a non-negative integer')
    if not _is_boolean(header.empty_export_acknowledged):
        raise MigrationCliError(f'{path}: receipt empty_export_acknowledged must be a boolean')
    if (header.source_record_count == 0) != header.empty_export_acknowledged:
        raise MigrationCliError(f'{path}: empty-export acknowledgement does not match receipt record count')
    if not _is_nonempty_string(header.namespace):
        raise MigrationCliError(f'{path}: receipt namespace must be a non-empty string')
    _require_projection_version(header.target_version, 'receipt target version')
    for name, value in (
        ('embedding_provider', header.embedding_provider),
        ('embedding_model', header.embedding_model),
    ):
        if not _is_nonempty_string(value):
            raise MigrationCliError(f'{path}: receipt {name} must be a non-empty string')
    for name, value in (
        ('embedding_dimension', header.embedding_dimension),
        ('projection_schema_version', header.projection_schema_version),
    ):
        if not _is_positive_integer(value):
            raise MigrationCliError(f'{path}: receipt {name} must be a positive integer')


def _validate_batch_size(batch_size: int) -> None:
    if batch_size < 1:
        raise MigrationCliError('batch size must be positive')


def _require_projection_version(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _PROJECTION_VERSION.fullmatch(value):
        raise MigrationCliError(f'{label} must match [a-z0-9][a-z0-9_-]*')
    return value


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_positive_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_boolean(value: Any) -> bool:
    return isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _required_env(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise MigrationCliError(f'{name} must be explicitly configured')
    return value


def _required_projection_namespaces() -> set[str]:
    values = [value.strip() for value in _required_env(PROJECTION_REQUIRED_NAMESPACES_ENV).split(',')]
    namespaces = {value for value in values if value}
    if not namespaces or any(not value for value in values) or len(namespaces) != len(values):
        raise MigrationCliError(
            f'{PROJECTION_REQUIRED_NAMESPACES_ENV} must be an explicit duplicate-free namespace list'
        )
    return namespaces


def _resolve_plan_path(plan_path: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    return candidate if candidate.is_absolute() else plan_path.parent / candidate


def _validate_migration_runtime(target_version: str) -> None:
    target = _require_projection_version(target_version, 'target version')
    mode = _required_env(PROJECTION_MODE_ENV).lower()
    if mode != 'dual_write':
        raise MigrationCliError(f'{PROJECTION_MODE_ENV} must be dual_write during backfill and verification')
    active = _require_projection_version(
        _required_env(PROJECTION_ACTIVE_VERSION_ENV),
        PROJECTION_ACTIVE_VERSION_ENV,
    )
    configured_target = _require_projection_version(
        _required_env(PROJECTION_TARGET_VERSION_ENV),
        PROJECTION_TARGET_VERSION_ENV,
    )
    if configured_target != target:
        raise MigrationCliError(f'{PROJECTION_TARGET_VERSION_ENV} must match target version {target!r}')
    if active == target:
        raise MigrationCliError('active and target projection versions must be distinct')
    _positive_int_env(PROJECTION_SCHEMA_VERSION_ENV)
    deletion_versions = {
        _require_projection_version(value.strip(), PROJECTION_DELETE_VERSIONS_ENV)
        for value in _required_env(PROJECTION_DELETE_VERSIONS_ENV).split(',')
        if value.strip()
    }
    if not {active, target}.issubset(deletion_versions):
        raise MigrationCliError(
            f'{PROJECTION_DELETE_VERSIONS_ENV} must retain both active and target versions during migration'
        )


def _validate_embedding_runtime_identity(embeddings: EmbeddingRuntime) -> None:
    configured_provider = _required_env('EMBEDDING_PROVIDER').lower()
    configured_model = _required_env('EMBEDDING_MODEL')
    configured_dimension = _positive_int_env('EMBEDDING_DIMENSION')
    if embeddings.provider_id != configured_provider:
        raise MigrationCliError('configured embedding provider does not match the loaded embedding adapter')
    if embeddings.model_id != configured_model:
        raise MigrationCliError('configured embedding model does not match the loaded embedding adapter')
    if embeddings.dimension is not None and embeddings.dimension != configured_dimension:
        raise MigrationCliError('configured embedding dimension does not match the loaded embedding adapter')


def _validate_rollback_runtime(previous_version: str, abandoned_versions: Sequence[str]) -> None:
    previous = _require_projection_version(previous_version, 'previous version')
    abandoned = {
        _require_projection_version(abandoned_version, 'abandoned version') for abandoned_version in abandoned_versions
    }
    mode = _required_env(PROJECTION_MODE_ENV).lower()
    if mode not in {'single', 'dual_write'}:
        raise MigrationCliError(f'{PROJECTION_MODE_ENV} must be single or dual_write')
    active = _require_projection_version(
        _required_env(PROJECTION_ACTIVE_VERSION_ENV),
        PROJECTION_ACTIVE_VERSION_ENV,
    )
    if PROJECTION_TARGET_VERSION_ENV not in os.environ:
        raise MigrationCliError(f'{PROJECTION_TARGET_VERSION_ENV} must be explicitly declared, blank in single mode')
    target_raw = os.environ[PROJECTION_TARGET_VERSION_ENV].strip()
    target = _require_projection_version(target_raw, PROJECTION_TARGET_VERSION_ENV) if target_raw else None
    if mode == 'single' and target is not None:
        raise MigrationCliError(f'{PROJECTION_TARGET_VERSION_ENV} must be blank in single mode')
    if mode == 'dual_write' and (target is None or target == active):
        raise MigrationCliError('dual_write requires a distinct target version before rollback')
    _positive_int_env(PROJECTION_SCHEMA_VERSION_ENV)
    retained = {
        _require_projection_version(value.strip(), PROJECTION_DELETE_VERSIONS_ENV)
        for value in _required_env(PROJECTION_DELETE_VERSIONS_ENV).split(',')
        if value.strip()
    }
    required_retained = {previous, active, *abandoned}
    if target is not None:
        required_retained.add(target)
    if not required_retained.issubset(retained | {previous, *abandoned}):
        raise MigrationCliError(f'{PROJECTION_DELETE_VERSIONS_ENV} does not retain current active/target versions')


def _validate_runtime_projection_contract(header: ReceiptHeader) -> None:
    _validate_migration_runtime(header.target_version)
    schema = _positive_int_env(PROJECTION_SCHEMA_VERSION_ENV)
    if schema != header.projection_schema_version:
        raise MigrationCliError('runtime projection schema does not match the backfill receipt')
    configured_provider = os.getenv('EMBEDDING_PROVIDER', '').strip().lower()
    configured_model = os.getenv('EMBEDDING_MODEL', '').strip()
    configured_dimension = _positive_int_env('EMBEDDING_DIMENSION')
    if configured_provider != header.embedding_provider:
        raise MigrationCliError('runtime embedding provider does not match the backfill receipt')
    if configured_model != header.embedding_model:
        raise MigrationCliError('runtime embedding model does not match the backfill receipt')
    if configured_dimension != header.embedding_dimension:
        raise MigrationCliError('runtime embedding dimension does not match the backfill receipt')


def _configured_store(identity: Any) -> VersionedVectorStoreAdapter:
    provider = os.getenv('VECTOR_STORE_PROVIDER', '').strip().lower()
    if provider == 'qdrant':
        raw_store = create_qdrant_vector_store_from_env()
    elif provider == 'pinecone':
        api_key = os.getenv('PINECONE_API_KEY', '').strip()
        index_name = os.getenv('PINECONE_INDEX_NAME', '').strip()
        if not api_key or not index_name:
            raise MigrationCliError('Pinecone migration requires PINECONE_API_KEY and PINECONE_INDEX_NAME')
        from pinecone import Pinecone  # pyright: ignore[reportMissingImports]

        raw_store = PineconeVectorStoreAdapter(Pinecone(api_key=api_key).Index(index_name))
    else:
        raise MigrationCliError('VECTOR_STORE_PROVIDER must explicitly select qdrant or pinecone')
    return VersionedVectorStoreAdapter(raw_store, identity)


def _configured_embeddings() -> EmbeddingRuntime:
    from utils.llm.clients import embeddings

    return embeddings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)

    validate = subparsers.add_parser('validate', help='validate an authoritative JSONL export without writing')
    validate.add_argument('--records', required=True, type=Path)
    validate.add_argument('--allow-empty', action='store_true')

    backfill = subparsers.add_parser('backfill', help='embed authoritative JSONL and write one target projection')
    backfill.add_argument('--records', required=True, type=Path)
    backfill.add_argument('--receipt', required=True, type=Path)
    backfill.add_argument('--namespace', required=True)
    backfill.add_argument('--target-version', required=True)
    backfill.add_argument('--batch-size', type=int, default=100)
    backfill.add_argument('--allow-empty', action='store_true')

    verify = subparsers.add_parser('verify', help='verify target vectors against the source-bound receipt')
    verify.add_argument('--records', required=True, type=Path)
    verify.add_argument('--receipt', required=True, type=Path)
    verify.add_argument('--batch-size', type=int, default=100)
    verify.add_argument('--report-output', type=Path)

    switch = subparsers.add_parser('switch', help='re-verify online, then write a switch env overlay')
    switch.add_argument('--plan', required=True, type=Path)
    switch.add_argument('--env-output', required=True, type=Path)
    switch.add_argument('--batch-size', type=int, default=100)

    rollback = subparsers.add_parser('rollback', help='write a rollback env overlay without mutating the store')
    rollback.add_argument('--previous-version', required=True)
    rollback.add_argument('--abandoned-version', action='append', default=[])
    rollback.add_argument('--env-output', required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == 'validate':
            records, source_sha256 = load_authority_records(args.records, allow_empty=args.allow_empty)
            result: dict[str, Any] = {
                'status': 'valid',
                'records': len(records),
                'source_sha256': source_sha256,
            }
        elif args.command == 'backfill':
            embeddings = _configured_embeddings()
            store = _configured_store(embeddings)
            result = backfill_from_authority(
                records_path=args.records,
                receipt_path=args.receipt,
                namespace=args.namespace,
                target_version=args.target_version,
                store=store,
                embeddings=embeddings,
                batch_size=args.batch_size,
                allow_empty=args.allow_empty,
            )
        elif args.command == 'verify':
            if args.report_output is not None:
                _require_new_output(args.report_output)
            report = verify_receipt(
                records_path=args.records,
                receipt_path=args.receipt,
                store_factory=_configured_store,
                batch_size=args.batch_size,
            )
            result = {'status': 'verified' if report.ready_to_switch else 'incomplete', **asdict(report)}
            if args.report_output is not None:
                _atomic_write(args.report_output, json.dumps(result, sort_keys=True, indent=2) + '\n')
            if not report.ready_to_switch:
                raise ProjectionUnavailableError('vector_switch', 'target verification is incomplete', retryable=False)
        elif args.command == 'switch':
            manifest = switch_from_plan(
                plan_path=args.plan,
                env_output=args.env_output,
                store_factory=_configured_store,
                batch_size=args.batch_size,
            )
            result = {'status': 'switch_manifest_ready', 'env_output': str(args.env_output), 'manifest': manifest}
        else:
            manifest = rollback_manifest(
                previous_version=args.previous_version,
                abandoned_versions=args.abandoned_version,
                env_output=args.env_output,
            )
            result = {'status': 'rollback_manifest_ready', 'env_output': str(args.env_output), 'manifest': manifest}
    except (MigrationCliError, ProjectionUnavailableError, OSError, ValueError) as error:
        print(
            json.dumps(
                {'status': 'error', 'error_type': type(error).__name__, 'error': str(error)},
                sort_keys=True,
                separators=(',', ':'),
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as error:
        print(
            json.dumps(
                {
                    'status': 'error',
                    'error_type': type(error).__name__,
                    'error': 'unexpected migration failure; no cutover manifest was written',
                },
                sort_keys=True,
                separators=(',', ':'),
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
