#!/usr/bin/env python3
# LIFECYCLE: permanent
"""Fail-closed, resumable GCS/Firebase Storage to MinIO migration.

The immutable mode-0600 inventory is the copy authority.  Every source object
is read at an exact GCS generation and hashed before copy.  Resume is bound to
the plan, source/target authorities, manifest bytes, and existing-object
policy.  A successful result requires a fresh source scan plus an independent
target enumeration and byte re-hash to match the original count/content hash.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping, Protocol, Sequence, cast
from urllib.parse import urlsplit

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


PLAN_SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 1
MANIFEST_FORMAT = 'omi-gcs-minio-inventory-v1'
CHECKPOINT_FORMAT = 'omi-gcs-minio-checkpoint-v1'
POLICIES = frozenset({'create-only', 'same-hash'})
_BUCKET = re.compile(r'[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?')
_SCOPE_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
_METADATA_KEY = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')
_SHA256 = re.compile(r'[0-9a-f]{64}')
_SINGLE_PUT_MAX_BYTES = 64 * 1024 * 1024
_MIN_MULTIPART_PART_BYTES = 8 * 1024 * 1024
_MAX_MULTIPART_PARTS = 10_000
_MAX_MULTIPART_PART_BYTES = 5 * 1024 * 1024 * 1024
_MAX_OBJECT_BYTES = _MAX_MULTIPART_PARTS * _MAX_MULTIPART_PART_BYTES
_RESERVED_METADATA_PREFIX = 'omi-migration-'
_RESERVED_METADATA = {
    'omi-migration-format': MANIFEST_FORMAT,
    'omi-migration-plan-sha256': '',
    'omi-migration-scope': '',
    'omi-migration-source-generation': '',
    'omi-migration-source-sha256': '',
    'omi-migration-source-metadata-sha256': '',
}
_MANIFEST_HEADER_KEYS = frozenset(
    {
        'type',
        'format',
        'plan_sha256',
        'source_authority',
        'record_count',
        'content_hash',
        'created_at',
    }
)
_RECORD_KEYS = frozenset(
    {
        'type',
        'scope_id',
        'source_bucket',
        'source_name',
        'target_bucket',
        'target_name',
        'generation',
        'size',
        'sha256',
        'metadata',
        'content_type',
    }
)


class StorageMigrationError(RuntimeError):
    """Safe operator-facing migration error."""


class StorageReconciliationError(StorageMigrationError):
    """Source/target state cannot authorize cutover."""


class TargetConflictError(StorageMigrationError):
    """A target object violated the selected existing-object policy."""


@dataclass(frozen=True)
class SourceAuthority:
    project: str
    endpoint: str


@dataclass(frozen=True)
class Scope:
    id: str
    source_bucket: str
    source_prefix: str
    target_bucket: str
    target_prefix: str


@dataclass(frozen=True)
class MigrationPlan:
    scopes: tuple[Scope, ...]
    sha256: str


@dataclass(frozen=True)
class SourceDescriptor:
    bucket: str
    name: str
    generation: str
    size: int
    metadata: dict[str, str]
    content_type: str | None


@dataclass(frozen=True)
class TargetDescriptor:
    bucket: str
    name: str
    size: int
    metadata: dict[str, str]
    content_type: str | None


@dataclass(frozen=True)
class ObjectRecord:
    scope_id: str
    source_bucket: str
    source_name: str
    target_bucket: str
    target_name: str
    generation: str
    size: int
    sha256: str
    metadata: dict[str, str]
    content_type: str | None


@dataclass(frozen=True)
class Inventory:
    count: int
    content_hash: str


@dataclass(frozen=True)
class Manifest:
    path: Path
    sha256: str
    source_authority: SourceAuthority
    plan_sha256: str
    inventory: Inventory
    records: tuple[ObjectRecord, ...]


class SourceStore(Protocol):
    @property
    def authority(self) -> SourceAuthority: ...

    def list_objects(self, scope: Scope) -> Iterable[SourceDescriptor]: ...

    def open_object(self, record: ObjectRecord) -> BinaryIO: ...


class TargetStore(Protocol):
    @property
    def authority(self) -> str: ...

    def ensure_bucket(self, bucket: str) -> None: ...

    def list_objects(self, scope: Scope) -> Iterable[TargetDescriptor]: ...

    def head_object(self, bucket: str, name: str) -> TargetDescriptor | None: ...

    def open_object(self, bucket: str, name: str) -> BinaryIO: ...

    def put_object_create_only(
        self,
        record: ObjectRecord,
        stream: BinaryIO,
        *,
        plan_sha256: str,
    ) -> None: ...


class _HashingReader(io.RawIOBase):
    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self._digest = hashlib.sha256()
        self._count = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self._raw.read(size)
        if chunk:
            self._digest.update(chunk)
            self._count += len(chunk)
        return chunk

    @property
    def count(self) -> int:
        return self._count

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _strict_json_loads(value: str | bytes) -> Any:
    return json.loads(value, parse_constant=lambda constant: _reject_json_constant(constant))


def _reject_json_constant(constant: str) -> Any:
    raise json.JSONDecodeError(f'non-standard numeric constant {constant}', constant, 0)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _encoded_record(record: ObjectRecord) -> bytes:
    return json.dumps(
        {'type': 'record', **asdict(record)},
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        allow_nan=False,
    ).encode('utf-8')


def _record_digest(encoded: bytes) -> bytes:
    digest = hashlib.sha256()
    digest.update(len(encoded).to_bytes(8, 'big'))
    digest.update(encoded)
    return digest.digest()


def _inventory(records: Iterable[ObjectRecord]) -> Inventory:
    digests: list[bytes] = []
    count = 0
    for record in records:
        digests.append(_record_digest(_encoded_record(record)))
        count += 1
    digest = hashlib.sha256()
    for item in sorted(digests):
        digest.update(item)
    return Inventory(count=count, content_hash=digest.hexdigest())


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent, text=True)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _require_new_private_file(path: Path, content_writer: Any) -> None:
    if path.exists():
        raise StorageMigrationError('refusing to overwrite an existing migration artifact')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            content_writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise StorageMigrationError('refusing to overwrite an existing migration artifact') from error
        temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _validate_bucket(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _BUCKET.fullmatch(value) or '..' in value:
        raise StorageMigrationError(f'{label} is not a safe bucket name')
    return value


def _validate_prefix(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise StorageMigrationError(f'{label} must be a string')
    if not value:
        return ''
    if not value.endswith('/'):
        raise StorageMigrationError(f'{label} must be empty or end with /')
    _validate_object_name(value[:-1], label)
    return value


def _validate_object_name(value: Any, label: str = 'object name') -> str:
    if not isinstance(value, str) or not value or value.startswith('/') or value.endswith('/'):
        raise StorageMigrationError(f'{label} is unsafe')
    if '\\' in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise StorageMigrationError(f'{label} is unsafe')
    parts = value.split('/')
    if any(part in {'', '.', '..'} for part in parts):
        raise StorageMigrationError(f'{label} is unsafe')
    return value


def _validate_metadata(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise StorageMigrationError('source object metadata is not a string map')
    result: dict[str, str] = {}
    for raw_key, raw_item in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_item, str):
            raise StorageMigrationError('source object metadata is not a string map')
        key = raw_key.strip()
        if key != raw_key or not _METADATA_KEY.fullmatch(key) or key.startswith(_RESERVED_METADATA_PREFIX):
            raise StorageMigrationError('source object metadata contains an unsupported key')
        if any(ord(character) < 32 or ord(character) > 126 for character in raw_item):
            raise StorageMigrationError('source object metadata contains an unsupported value')
        result[key] = raw_item
    if len(json.dumps(result, sort_keys=True, separators=(',', ':')).encode('utf-8')) > 1024:
        raise StorageMigrationError('source object metadata exceeds the safe MinIO migration limit')
    return result


def _validate_content_type(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 255:
        raise StorageMigrationError('source object content type is unsupported')
    if any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise StorageMigrationError('source object content type is unsupported')
    return value


def _prefixes_overlap(first: str, second: str) -> bool:
    return first.startswith(second) or second.startswith(first)


def load_plan(path: Path) -> MigrationPlan:
    raw_bytes = path.read_bytes()
    try:
        value = _strict_json_loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StorageMigrationError('migration plan is not valid UTF-8 JSON') from error
    if not isinstance(value, dict) or set(value) != {'schema_version', 'scopes'}:
        raise StorageMigrationError('migration plan must contain only schema_version and scopes')
    if value.get('schema_version') != PLAN_SCHEMA_VERSION:
        raise StorageMigrationError('unsupported migration plan schema')
    raw_scopes = value.get('scopes')
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise StorageMigrationError('migration plan scopes must be a non-empty list')
    scopes: list[Scope] = []
    ids: set[str] = set()
    required = {'id', 'source_bucket', 'source_prefix', 'target_bucket', 'target_prefix'}
    for raw_scope in raw_scopes:
        if not isinstance(raw_scope, dict) or set(raw_scope) != required:
            raise StorageMigrationError('each migration scope must have the exact documented fields')
        scope_id = raw_scope.get('id')
        if not isinstance(scope_id, str) or not _SCOPE_ID.fullmatch(scope_id) or scope_id in ids:
            raise StorageMigrationError('migration scope ids must be unique safe identifiers')
        ids.add(scope_id)
        scopes.append(
            Scope(
                id=scope_id,
                source_bucket=_validate_bucket(raw_scope.get('source_bucket'), 'source bucket'),
                source_prefix=_validate_prefix(raw_scope.get('source_prefix'), 'source prefix'),
                target_bucket=_validate_bucket(raw_scope.get('target_bucket'), 'target bucket'),
                target_prefix=_validate_prefix(raw_scope.get('target_prefix'), 'target prefix'),
            )
        )
    for index, first in enumerate(scopes):
        for second in scopes[index + 1 :]:
            if first.source_bucket == second.source_bucket and _prefixes_overlap(
                first.source_prefix, second.source_prefix
            ):
                raise StorageMigrationError('migration source scopes overlap')
            if first.target_bucket == second.target_bucket and _prefixes_overlap(
                first.target_prefix, second.target_prefix
            ):
                raise StorageMigrationError('migration target scopes overlap')
    scopes.sort(key=lambda scope: scope.id)
    return MigrationPlan(scopes=tuple(scopes), sha256=hashlib.sha256(raw_bytes).hexdigest())


def _scope_for_id(plan: MigrationPlan, scope_id: str) -> Scope:
    scope = next((item for item in plan.scopes if item.id == scope_id), None)
    if scope is None:
        raise StorageMigrationError('inventory refers to an unknown migration scope')
    return scope


def _record_from_descriptor(scope: Scope, descriptor: SourceDescriptor, sha256: str) -> ObjectRecord:
    if descriptor.bucket != scope.source_bucket or not descriptor.name.startswith(scope.source_prefix):
        raise StorageMigrationError('source provider returned an object outside its migration scope')
    source_name = _validate_object_name(descriptor.name)
    relative_name = source_name[len(scope.source_prefix) :]
    if not relative_name:
        raise StorageMigrationError('source provider returned a prefix placeholder instead of an object')
    target_name = _validate_object_name(f'{scope.target_prefix}{relative_name}')
    generation = str(descriptor.generation or '')
    if not generation.isdigit() or int(generation) < 1:
        raise StorageMigrationError('source object generation is invalid')
    if (
        not isinstance(descriptor.size, int)
        or isinstance(descriptor.size, bool)
        or not 0 <= descriptor.size <= _MAX_OBJECT_BYTES
    ):
        raise StorageMigrationError('source object size is invalid')
    if not _SHA256.fullmatch(sha256):
        raise StorageMigrationError('source object hash is invalid')
    return ObjectRecord(
        scope_id=scope.id,
        source_bucket=scope.source_bucket,
        source_name=source_name,
        target_bucket=scope.target_bucket,
        target_name=target_name,
        generation=generation,
        size=descriptor.size,
        sha256=sha256,
        metadata=_validate_metadata(descriptor.metadata),
        content_type=_validate_content_type(descriptor.content_type),
    )


def _sha256_stream(stream: BinaryIO, *, expected_size: int) -> str:
    digest = hashlib.sha256()
    count = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise StorageMigrationError('object stream returned non-byte content')
        digest.update(chunk)
        count += len(chunk)
    if count != expected_size:
        raise StorageReconciliationError('source or target object size changed while hashing')
    return digest.hexdigest()


def _read_stream_part(stream: BinaryIO, maximum: int) -> bytes:
    chunks: list[bytes] = []
    count = 0
    while count < maximum:
        chunk = stream.read(maximum - count)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise StorageMigrationError('object stream returned non-byte content')
        chunks.append(chunk)
        count += len(chunk)
    return b''.join(chunks)


def inventory_source(source: SourceStore, plan: MigrationPlan) -> tuple[tuple[ObjectRecord, ...], Inventory]:
    records: list[ObjectRecord] = []
    target_keys: set[tuple[str, str]] = set()
    for scope in plan.scopes:
        descriptors = sorted(source.list_objects(scope), key=lambda item: (item.name, str(item.generation)))
        for descriptor in descriptors:
            provisional = _record_from_descriptor(scope, descriptor, '0' * 64)
            with source.open_object(provisional) as stream:
                sha256 = _sha256_stream(stream, expected_size=provisional.size)
            record = _record_from_descriptor(scope, descriptor, sha256)
            target_key = (record.target_bucket, record.target_name)
            if target_key in target_keys:
                raise StorageMigrationError('migration plan maps multiple source objects to one target object')
            target_keys.add(target_key)
            records.append(record)
    records.sort(key=lambda item: (item.target_bucket, item.target_name))
    return tuple(records), _inventory(records)


def capture_inventory(source: SourceStore, plan: MigrationPlan, manifest_path: Path) -> Manifest:
    records, inventory = inventory_source(source, plan)
    header = {
        'type': 'header',
        'format': MANIFEST_FORMAT,
        'plan_sha256': plan.sha256,
        'source_authority': asdict(source.authority),
        'record_count': inventory.count,
        'content_hash': inventory.content_hash,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }

    def write_manifest(handle: BinaryIO) -> None:
        handle.write(json.dumps(header, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n')
        for record in records:
            handle.write(_encoded_record(record) + b'\n')

    _require_new_private_file(manifest_path, write_manifest)
    return Manifest(
        path=manifest_path.resolve(),
        sha256=_file_sha256(manifest_path),
        source_authority=source.authority,
        plan_sha256=plan.sha256,
        inventory=inventory,
        records=records,
    )


def _record_from_json(value: Any, plan: MigrationPlan) -> ObjectRecord:
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS or value.get('type') != 'record':
        raise StorageMigrationError('inventory contains an invalid object record')
    scope_id = value.get('scope_id')
    if not isinstance(scope_id, str):
        raise StorageMigrationError('inventory contains an invalid scope id')
    scope = _scope_for_id(plan, scope_id)
    source_bucket = value.get('source_bucket')
    source_name = value.get('source_name')
    size = value.get('size')
    if not isinstance(source_bucket, str) or not isinstance(source_name, str):
        raise StorageMigrationError('inventory contains an invalid source object identity')
    if not isinstance(size, int) or isinstance(size, bool):
        raise StorageMigrationError('inventory contains an invalid source object size')
    descriptor = SourceDescriptor(
        bucket=source_bucket,
        name=source_name,
        generation=str(value.get('generation') or ''),
        size=size,
        metadata=_validate_metadata(value.get('metadata')),
        content_type=_validate_content_type(value.get('content_type')),
    )
    sha256 = value.get('sha256')
    if not isinstance(sha256, str):
        raise StorageMigrationError('inventory contains an invalid object hash')
    record = _record_from_descriptor(scope, descriptor, sha256)
    if record.target_bucket != value.get('target_bucket') or record.target_name != value.get('target_name'):
        raise StorageMigrationError('inventory target mapping does not match the migration plan')
    return record


def load_manifest(path: Path, plan: MigrationPlan) -> Manifest:
    lines = path.read_bytes().splitlines()
    if not lines:
        raise StorageMigrationError('inventory manifest is empty')
    try:
        header = _strict_json_loads(lines[0])
        raw_records = [_strict_json_loads(line) for line in lines[1:] if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StorageMigrationError('inventory manifest is not valid UTF-8 JSONL') from error
    if not isinstance(header, dict) or set(header) != _MANIFEST_HEADER_KEYS or header.get('type') != 'header':
        raise StorageMigrationError('inventory manifest has an invalid header')
    if header.get('format') != MANIFEST_FORMAT or header.get('plan_sha256') != plan.sha256:
        raise StorageMigrationError('inventory manifest does not match the migration plan')
    authority = header.get('source_authority')
    if not isinstance(authority, dict) or set(authority) != {'project', 'endpoint'}:
        raise StorageMigrationError('inventory manifest has an invalid source authority')
    source_authority = SourceAuthority(
        project=str(authority.get('project') or ''),
        endpoint=_canonical_endpoint(str(authority.get('endpoint') or '')),
    )
    if not source_authority.project:
        raise StorageMigrationError('inventory manifest has an invalid source project')
    records = tuple(_record_from_json(value, plan) for value in raw_records)
    target_keys = {(record.target_bucket, record.target_name) for record in records}
    if len(target_keys) != len(records):
        raise StorageMigrationError('inventory contains colliding target object names')
    inventory = _inventory(records)
    if header.get('record_count') != inventory.count or header.get('content_hash') != inventory.content_hash:
        raise StorageMigrationError('inventory manifest count/content hash is invalid')
    return Manifest(
        path=path.resolve(),
        sha256=_file_sha256(path),
        source_authority=source_authority,
        plan_sha256=plan.sha256,
        inventory=inventory,
        records=records,
    )


def _metadata_digest(metadata: Mapping[str, str]) -> str:
    encoded = json.dumps(dict(metadata), sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def _expected_target_metadata(record: ObjectRecord, plan_sha256: str) -> dict[str, str]:
    return {
        **record.metadata,
        **{
            **_RESERVED_METADATA,
            'omi-migration-plan-sha256': plan_sha256,
            'omi-migration-scope': record.scope_id,
            'omi-migration-source-generation': record.generation,
            'omi-migration-source-sha256': record.sha256,
            'omi-migration-source-metadata-sha256': _metadata_digest(record.metadata),
        },
    }


def _content_type_matches(expected: str | None, actual: str | None) -> bool:
    if expected is None:
        return actual in {None, '', 'application/octet-stream', 'binary/octet-stream'}
    return actual == expected


def _verify_target_object(
    target: TargetStore,
    descriptor: TargetDescriptor,
    expected: ObjectRecord,
    *,
    plan_sha256: str,
) -> None:
    if descriptor.bucket != expected.target_bucket or descriptor.name != expected.target_name:
        raise StorageReconciliationError('target provider returned an object outside its migration scope')
    if descriptor.size != expected.size:
        raise StorageReconciliationError('target object size does not match inventory')
    if descriptor.metadata != _expected_target_metadata(expected, plan_sha256):
        raise StorageReconciliationError('target object metadata does not match inventory')
    if not _content_type_matches(expected.content_type, descriptor.content_type):
        raise StorageReconciliationError('target object content type does not match inventory')
    with target.open_object(descriptor.bucket, descriptor.name) as stream:
        actual_sha256 = _sha256_stream(stream, expected_size=expected.size)
    if actual_sha256 != expected.sha256:
        raise StorageReconciliationError('target object bytes do not match inventory')


def target_inventory(target: TargetStore, plan: MigrationPlan, manifest: Manifest) -> Inventory:
    expected = {(record.target_bucket, record.target_name): record for record in manifest.records}
    observed: dict[tuple[str, str], TargetDescriptor] = {}
    for scope in plan.scopes:
        for descriptor in target.list_objects(scope):
            if descriptor.bucket != scope.target_bucket or not descriptor.name.startswith(scope.target_prefix):
                raise StorageReconciliationError('target provider returned an object outside its migration scope')
            _validate_object_name(descriptor.name)
            key = (descriptor.bucket, descriptor.name)
            if key in observed:
                raise StorageReconciliationError('target provider returned a duplicate object')
            observed[key] = descriptor
    if set(observed) != set(expected):
        raise StorageReconciliationError('target object count/key reconciliation failed')
    verified: list[ObjectRecord] = []
    for key in sorted(expected):
        _verify_target_object(target, observed[key], expected[key], plan_sha256=plan.sha256)
        verified.append(expected[key])
    return _inventory(verified)


def _checkpoint_payload(
    *,
    plan: MigrationPlan,
    manifest: Manifest,
    target: TargetStore,
    existing_policy: str,
) -> dict[str, Any]:
    return {
        'schema_version': CHECKPOINT_SCHEMA_VERSION,
        'format': CHECKPOINT_FORMAT,
        'status': 'captured',
        'plan_sha256': plan.sha256,
        'manifest': str(manifest.path),
        'manifest_sha256': manifest.sha256,
        'source_authority': asdict(manifest.source_authority),
        'target_authority': target.authority,
        'existing_policy': existing_policy,
        'source_count': manifest.inventory.count,
        'source_content_hash': manifest.inventory.content_hash,
        'next_index': 0,
    }


def _read_checkpoint(
    path: Path,
    *,
    plan: MigrationPlan,
    manifest: Manifest,
    source: SourceStore,
    target: TargetStore,
    existing_policy: str,
) -> dict[str, Any]:
    try:
        checkpoint = _strict_json_loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StorageMigrationError('checkpoint is not valid UTF-8 JSON') from error
    if not isinstance(checkpoint, dict):
        raise StorageMigrationError('checkpoint must be a JSON object')
    expected = {
        'schema_version': CHECKPOINT_SCHEMA_VERSION,
        'format': CHECKPOINT_FORMAT,
        'plan_sha256': plan.sha256,
        'manifest': str(manifest.path),
        'manifest_sha256': manifest.sha256,
        'source_authority': asdict(source.authority),
        'target_authority': target.authority,
        'existing_policy': existing_policy,
        'source_count': manifest.inventory.count,
        'source_content_hash': manifest.inventory.content_hash,
    }
    mismatched = [key for key, value in expected.items() if checkpoint.get(key) != value]
    if mismatched:
        raise StorageMigrationError('checkpoint authority, plan, manifest, or policy does not match this resume')
    next_index = checkpoint.get('next_index')
    if not isinstance(next_index, int) or isinstance(next_index, bool) or not 0 <= next_index <= len(manifest.records):
        raise StorageMigrationError('checkpoint next_index is invalid')
    return checkpoint


def _copy_one(
    source: SourceStore,
    target: TargetStore,
    record: ObjectRecord,
    *,
    plan_sha256: str,
) -> None:
    with source.open_object(record) as raw:
        hashing = _HashingReader(raw)
        target.put_object_create_only(record, cast(BinaryIO, hashing), plan_sha256=plan_sha256)
        if hashing.count != record.size or hashing.hexdigest != record.sha256:
            raise StorageReconciliationError('source generation bytes changed during copy')
    descriptor = target.head_object(record.target_bucket, record.target_name)
    if descriptor is None:
        raise StorageReconciliationError('target did not retain a copied object')
    _verify_target_object(target, descriptor, record, plan_sha256=plan_sha256)


def run_apply(
    source: SourceStore,
    target: TargetStore,
    plan: MigrationPlan,
    manifest: Manifest,
    checkpoint_path: Path,
    *,
    existing_policy: str,
) -> dict[str, Any]:
    if existing_policy not in POLICIES:
        raise ValueError('existing_policy must be create-only or same-hash')
    if source.authority != manifest.source_authority:
        raise StorageMigrationError('live source authority does not match the inventory manifest')
    for scope in plan.scopes:
        target.ensure_bucket(scope.target_bucket)
    is_resume = checkpoint_path.exists()
    if is_resume:
        checkpoint = _read_checkpoint(
            checkpoint_path,
            plan=plan,
            manifest=manifest,
            source=source,
            target=target,
            existing_policy=existing_policy,
        )
    else:
        if existing_policy == 'create-only':
            target_has_objects = any(next(iter(target.list_objects(scope)), None) is not None for scope in plan.scopes)
            if target_has_objects:
                raise TargetConflictError(
                    'create-only migration requires empty target scopes when no checkpoint exists'
                )
        checkpoint = _checkpoint_payload(
            plan=plan,
            manifest=manifest,
            target=target,
            existing_policy=existing_policy,
        )
        _atomic_json(checkpoint_path, checkpoint)

    next_index = int(checkpoint['next_index'])
    try:
        for index, record in enumerate(manifest.records):
            if index < next_index:
                continue
            existing = target.head_object(record.target_bucket, record.target_name)
            if existing is not None:
                if existing_policy != 'same-hash' and not is_resume:
                    raise TargetConflictError('create-only migration encountered an existing target object')
                _verify_target_object(target, existing, record, plan_sha256=plan.sha256)
            else:
                _copy_one(source, target, record, plan_sha256=plan.sha256)
            next_index = index + 1
            checkpoint.update(status='importing', next_index=next_index)
            _atomic_json(checkpoint_path, checkpoint)

        checkpoint.update(
            status='applied',
            applied_at=datetime.now(timezone.utc).isoformat(),
            next_index=next_index,
        )
        _atomic_json(checkpoint_path, checkpoint)
        return checkpoint
    except Exception as error:
        checkpoint.update(status='failed', next_index=next_index, error_class=type(error).__name__)
        _atomic_json(checkpoint_path, checkpoint)
        raise


def run_verify(
    source: SourceStore,
    target: TargetStore,
    plan: MigrationPlan,
    manifest: Manifest,
    checkpoint_path: Path,
    *,
    existing_policy: str,
) -> dict[str, Any]:
    if existing_policy not in POLICIES:
        raise ValueError('existing_policy must be create-only or same-hash')
    if source.authority != manifest.source_authority:
        raise StorageMigrationError('live source authority does not match the inventory manifest')
    if not checkpoint_path.exists():
        raise StorageMigrationError('verification requires the apply checkpoint')
    checkpoint = _read_checkpoint(
        checkpoint_path,
        plan=plan,
        manifest=manifest,
        source=source,
        target=target,
        existing_policy=existing_policy,
    )
    next_index = int(checkpoint['next_index'])
    if next_index != len(manifest.records):
        raise StorageMigrationError('verification requires every inventoried object to be applied')
    try:
        checkpoint.update(status='verifying')
        _atomic_json(checkpoint_path, checkpoint)
        live_records, live_source = inventory_source(source, plan)
        target_state = target_inventory(target, plan, manifest)
        manifest_state = _inventory(manifest.records)
        expected = (manifest.inventory.count, manifest.inventory.content_hash)
        observed = {
            'manifest': (manifest_state.count, manifest_state.content_hash),
            'live_source': (live_source.count, live_source.content_hash),
            'target': (target_state.count, target_state.content_hash),
        }
        if any(value != expected for value in observed.values()) or _inventory(live_records) != live_source:
            raise StorageReconciliationError('source/manifest/target count or content reconciliation failed')
        checkpoint.update(
            status='passed',
            verified_at=datetime.now(timezone.utc).isoformat(),
            source_live_count=live_source.count,
            source_live_content_hash=live_source.content_hash,
            target_count=target_state.count,
            target_content_hash=target_state.content_hash,
        )
        _atomic_json(checkpoint_path, checkpoint)
        return checkpoint
    except Exception as error:
        checkpoint.update(status='failed', error_class=type(error).__name__)
        _atomic_json(checkpoint_path, checkpoint)
        raise


def _canonical_endpoint(value: str) -> str:
    raw = value.strip().rstrip('/')
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise StorageMigrationError('storage endpoint must be an explicit credential-free HTTP(S) origin')
    if parsed.path not in {'', '/'}:
        raise StorageMigrationError('storage endpoint must not contain a path')
    return f'{parsed.scheme}://{parsed.netloc.lower()}'


class GcsSource:
    def __init__(self, *, project: str, credentials_path: Path, endpoint: str) -> None:
        if not project.strip():
            raise StorageMigrationError('source project must be explicit')
        if not credentials_path.is_file():
            raise StorageMigrationError('source credentials file is missing')
        from google.cloud import storage  # pyright: ignore[reportAttributeAccessIssue]
        from google.oauth2 import service_account  # pyright: ignore[reportMissingImports]

        credentials = service_account.Credentials.from_service_account_file(str(credentials_path))
        options = {'api_endpoint': endpoint} if endpoint != 'https://storage.googleapis.com' else None
        self._client = storage.Client(project=project, credentials=credentials, client_options=options)
        self._authority = SourceAuthority(project=project, endpoint=_canonical_endpoint(endpoint))

    @property
    def authority(self) -> SourceAuthority:
        return self._authority

    def list_objects(self, scope: Scope) -> Iterable[SourceDescriptor]:
        for blob in self._client.list_blobs(scope.source_bucket, prefix=scope.source_prefix):
            yield SourceDescriptor(
                bucket=scope.source_bucket,
                name=str(blob.name),
                generation=str(blob.generation or ''),
                size=int(blob.size or 0),
                metadata=dict(blob.metadata or {}),
                content_type=blob.content_type,
            )

    def open_object(self, record: ObjectRecord) -> BinaryIO:
        blob = self._client.bucket(record.source_bucket).blob(
            record.source_name,
            generation=int(record.generation),
        )
        return blob.open('rb', if_generation_match=int(record.generation), chunk_size=1024 * 1024)


class MinioTarget:
    def __init__(self, *, endpoint: str, access_key: str, secret_key: str, region: str) -> None:
        if not access_key or not secret_key:
            raise StorageMigrationError('MinIO access key and secret key are required')
        import boto3  # pyright: ignore[reportMissingImports]
        from botocore.config import Config  # pyright: ignore[reportMissingImports]

        self._authority = _canonical_endpoint(endpoint)
        self._client = boto3.client(
            's3',
            endpoint_url=self._authority,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version='s3v4',
                s3={'addressing_style': 'path', 'payload_signing_enabled': False},
                # Botocore's optional default CRC32 header pass seeks back to
                # the beginning, which would buffer or break a true source
                # stream. The importer performs its own SHA-256 while copying
                # and independently re-reads MinIO before acknowledging it.
                request_checksum_calculation='when_required',
            ),
        )

    @property
    def authority(self) -> str:
        return self._authority

    @staticmethod
    def _error_code(error: Exception) -> str:
        response = getattr(error, 'response', {})
        detail = response.get('Error', {}) if isinstance(response, dict) else {}
        return str(detail.get('Code') or '') if isinstance(detail, dict) else ''

    def ensure_bucket(self, bucket: str) -> None:
        try:
            self._client.head_bucket(Bucket=bucket)
            return
        except Exception as error:
            if self._error_code(error) not in {'404', 'NoSuchBucket', 'NotFound'}:
                raise
        try:
            self._client.create_bucket(Bucket=bucket)
        except Exception as error:
            if self._error_code(error) not in {'BucketAlreadyExists', 'BucketAlreadyOwnedByYou'}:
                raise

    def list_objects(self, scope: Scope) -> Iterable[TargetDescriptor]:
        paginator = self._client.get_paginator('list_objects_v2')
        parameters: dict[str, Any] = {'Bucket': scope.target_bucket}
        if scope.target_prefix:
            parameters['Prefix'] = scope.target_prefix
        for page in paginator.paginate(**parameters):
            for item in page.get('Contents', []):
                name = str(item.get('Key') or '')
                descriptor = self.head_object(scope.target_bucket, name)
                if descriptor is None:
                    raise StorageReconciliationError('target object disappeared during enumeration')
                yield descriptor

    def head_object(self, bucket: str, name: str) -> TargetDescriptor | None:
        try:
            value = self._client.head_object(Bucket=bucket, Key=name)
        except Exception as error:
            if self._error_code(error) in {'404', 'NoSuchKey', 'NotFound'}:
                return None
            raise
        return TargetDescriptor(
            bucket=bucket,
            name=name,
            size=int(value.get('ContentLength') or 0),
            metadata={str(key): str(item) for key, item in (value.get('Metadata') or {}).items()},
            content_type=value.get('ContentType'),
        )

    def open_object(self, bucket: str, name: str) -> BinaryIO:
        return self._client.get_object(Bucket=bucket, Key=name)['Body']

    def put_object_create_only(
        self,
        record: ObjectRecord,
        stream: BinaryIO,
        *,
        plan_sha256: str,
    ) -> None:
        if record.size > _SINGLE_PUT_MAX_BYTES:
            self._put_multipart_create_only(record, stream, plan_sha256=plan_sha256)
            return
        parameters: dict[str, Any] = {
            'Bucket': record.target_bucket,
            'Key': record.target_name,
            'Body': stream,
            'ContentLength': record.size,
            'Metadata': _expected_target_metadata(record, plan_sha256),
            'IfNoneMatch': '*',
        }
        if record.content_type is not None:
            parameters['ContentType'] = record.content_type
        try:
            self._client.put_object(**parameters)
        except Exception as error:
            if self._error_code(error) in {'412', 'PreconditionFailed', 'ConditionalRequestConflict'}:
                raise TargetConflictError('target object was created concurrently') from error
            raise

    def _put_multipart_create_only(
        self,
        record: ObjectRecord,
        stream: BinaryIO,
        *,
        plan_sha256: str,
    ) -> None:
        part_size = max(
            _MIN_MULTIPART_PART_BYTES,
            (record.size + _MAX_MULTIPART_PARTS - 1) // _MAX_MULTIPART_PARTS,
        )
        if part_size > _MAX_MULTIPART_PART_BYTES:
            raise StorageMigrationError('source object exceeds the supported MinIO multipart size')
        create_parameters: dict[str, Any] = {
            'Bucket': record.target_bucket,
            'Key': record.target_name,
            'Metadata': _expected_target_metadata(record, plan_sha256),
        }
        if record.content_type is not None:
            create_parameters['ContentType'] = record.content_type
        created = self._client.create_multipart_upload(**create_parameters)
        upload_id = str(created.get('UploadId') or '')
        if not upload_id:
            raise StorageMigrationError('MinIO did not return a multipart upload id')
        completed = False
        try:
            parts: list[dict[str, Any]] = []
            remaining = record.size
            part_number = 1
            while remaining:
                chunk = _read_stream_part(stream, min(part_size, remaining))
                if not chunk:
                    raise StorageReconciliationError('source stream ended before its inventoried size')
                uploaded = self._client.upload_part(
                    Bucket=record.target_bucket,
                    Key=record.target_name,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                    ContentLength=len(chunk),
                )
                etag = str(uploaded.get('ETag') or '')
                if not etag:
                    raise StorageMigrationError('MinIO multipart response omitted an ETag')
                parts.append({'ETag': etag, 'PartNumber': part_number})
                remaining -= len(chunk)
                part_number += 1
            if stream.read(1):
                raise StorageReconciliationError('source stream exceeded its inventoried size')
            self._client.complete_multipart_upload(
                Bucket=record.target_bucket,
                Key=record.target_name,
                UploadId=upload_id,
                MultipartUpload={'Parts': parts},
                IfNoneMatch='*',
            )
            completed = True
        except Exception as error:
            if self._error_code(error) in {'412', 'PreconditionFailed', 'ConditionalRequestConflict'}:
                raise TargetConflictError('target object was created concurrently') from error
            raise
        finally:
            if not completed:
                try:
                    self._client.abort_multipart_upload(
                        Bucket=record.target_bucket,
                        Key=record.target_name,
                        UploadId=upload_id,
                    )
                except Exception:
                    pass


def _configured_source(arguments: Any) -> GcsSource:
    return GcsSource(
        project=arguments.source_project,
        credentials_path=arguments.source_credentials,
        endpoint=_canonical_endpoint(arguments.source_endpoint),
    )


def _configured_target(arguments: Any) -> MinioTarget:
    return MinioTarget(
        endpoint=arguments.target_endpoint,
        access_key=os.getenv('MINIO_ACCESS_KEY', ''),
        secret_key=os.getenv('MINIO_SECRET_KEY', ''),
        region=os.getenv('MINIO_REGION', 'us-east-1'),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for command in ('dry-run', 'apply', 'verify'):
        child = subparsers.add_parser(command)
        child.add_argument('--plan', required=True, type=Path)
        child.add_argument('--manifest', required=True, type=Path)
        child.add_argument('--source-project', required=True)
        child.add_argument('--source-credentials', required=True, type=Path)
        child.add_argument('--source-endpoint', default='https://storage.googleapis.com')
        if command in {'apply', 'verify'}:
            child.add_argument('--checkpoint', required=True, type=Path)
            child.add_argument('--target-endpoint', default=os.getenv('MINIO_ENDPOINT', ''))
            child.add_argument('--existing-policy', required=True, choices=sorted(POLICIES))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        plan = load_plan(arguments.plan)
        source = _configured_source(arguments)
        if arguments.command == 'dry-run':
            manifest = capture_inventory(source, plan, arguments.manifest)
            result = {
                'status': 'dry-run-passed',
                'records': manifest.inventory.count,
                'content_hash': manifest.inventory.content_hash,
                'manifest_sha256': manifest.sha256,
            }
        else:
            manifest = load_manifest(arguments.manifest, plan)
            target = _configured_target(arguments)
            if arguments.command == 'apply':
                applied = run_apply(
                    source,
                    target,
                    plan,
                    manifest,
                    arguments.checkpoint,
                    existing_policy=arguments.existing_policy,
                )
                result = {
                    'status': applied['status'],
                    'source_count': applied['source_count'],
                    'source_content_hash': applied['source_content_hash'],
                    'applied_count': applied['next_index'],
                }
            else:
                verified = run_verify(
                    source,
                    target,
                    plan,
                    manifest,
                    arguments.checkpoint,
                    existing_policy=arguments.existing_policy,
                )
                result = {
                    'status': verified['status'],
                    'source_count': verified['source_live_count'],
                    'source_content_hash': verified['source_live_content_hash'],
                    'target_count': verified['target_count'],
                    'target_content_hash': verified['target_content_hash'],
                }
    except (StorageMigrationError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    except Exception as error:
        print(f'ERROR: unexpected storage migration failure ({type(error).__name__})', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
