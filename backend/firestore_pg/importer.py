"""Resumable Firestore-to-PostgreSQL document import and reconciliation.

The source is first captured into a mode-0600 JSONL manifest.  That immutable
manifest is the resume authority; after writes complete the live source is
scanned again and must still have the same count/content hash.  PostgreSQL is
then independently enumerated from the explicit collection registry.  Any
count or hash mismatch raises and leaves cutover unauthorized.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, cast
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .codec import encode_document
from .engine import get_engine
from .migrations import COLLECTION_TABLE, check_schema, provision_collections
from .sql import upsert_sql

IMPORT_SCHEMA_VERSION = 3


class ImportReconciliationError(RuntimeError):
    """Source/target state is not safe to cut over."""


@dataclass(frozen=True)
class Inventory:
    count: int
    content_hash: str
    collections: tuple[str, ...]


@dataclass(frozen=True)
class SourceAuthority:
    project: str
    database: str
    resolved_endpoint: str
    emulator_authority: str


def _canonical_endpoint(value: Any, *, label: str, required: bool) -> str:
    raw = str(value or '').strip()
    if not raw:
        if required:
            raise ImportReconciliationError(f'Firestore source client must expose explicit {label}')
        return ''
    parsed = urlsplit(raw if '://' in raw else f'//{raw}')
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ImportReconciliationError(f'Firestore source {label} is not a host authority')
    host = (parsed.hostname or '').lower().rstrip('.')
    if not host:
        raise ImportReconciliationError(f'Firestore source {label} is not a host authority')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ImportReconciliationError(f'Firestore source {label} has an invalid port') from exc
    rendered_host = f'[{host}]' if ':' in host else host
    return f'{rendered_host}:{port}' if port is not None else rendered_host


def _source_authority(client: Any) -> SourceAuthority:
    project = str(getattr(client, 'project', '') or '').strip()
    database = str(getattr(client, '_database', '') or '').strip()
    if not project or not database:
        raise ImportReconciliationError('Firestore source client must expose explicit project and database authority')
    endpoint = _canonical_endpoint(getattr(client, '_target', ''), label='resolved endpoint', required=True)
    emulator = _canonical_endpoint(
        getattr(client, '_emulator_host', ''),
        label='emulator authority',
        required=False,
    )
    if emulator and endpoint != emulator:
        raise ImportReconciliationError('Firestore source resolved endpoint does not match its emulator authority')
    return SourceAuthority(project, database, endpoint, emulator)


def _normalize(value: Any) -> Any:
    # Source snapshots are already-authoritative stored values. Preserve their
    # calendar time instead of reapplying the SDK's negative-epoch
    # DatetimeWithNanoseconds write conversion during a backend migration.
    return encode_document(value, preserve_timestamp_calendar=True)


def _record(path: str, data: Mapping[str, Any]) -> dict[str, Any]:
    parts = [part for part in path.split('/') if part]
    if len(parts) < 2 or len(parts) % 2:
        raise ValueError(f'invalid Firestore document path: {path!r}')
    return {'path': '/'.join(parts), 'data': _normalize(data), 'collection_id': parts[-2]}


def _stored_record(path: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """Build an inventory record from already-encoded JSONB without re-encoding."""
    parts = [part for part in path.split('/') if part]
    if len(parts) < 2 or len(parts) % 2:
        raise ValueError(f'invalid Firestore document path: {path!r}')
    return {'path': '/'.join(parts), 'data': dict(data), 'collection_id': parts[-2]}


def _encoded_record(record: Mapping[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _inventory(records: Iterable[Mapping[str, Any]]) -> Inventory:
    record_digests: list[bytes] = []
    count = 0
    collections: set[str] = set()
    for record in records:
        encoded = _encoded_record(record)
        record_digests.append(_record_digest(encoded))
        count += 1
        collections.add(str(record['collection_id']))
    return Inventory(count, _content_hash(record_digests), tuple(sorted(collections)))


def _record_digest(encoded: bytes) -> bytes:
    digest = hashlib.sha256()
    digest.update(len(encoded).to_bytes(8, 'big'))
    digest.update(encoded)
    return digest.digest()


def _content_hash(record_digests: Iterable[bytes]) -> str:
    """Return a deterministic multiset hash independent of traversal order."""
    digest = hashlib.sha256()
    for record_digest in sorted(record_digests):
        digest.update(record_digest)
    return digest.hexdigest()


def walk_source(client: Any) -> Iterator[dict[str, Any]]:
    """Recursively enumerate real and missing-parent Firestore document paths."""

    def list_documents_including_missing(collection: Any) -> Iterable[Any]:
        # The Firestore ListDocuments RPC supports show_missing, but the pinned
        # Python SDK's public CollectionReference.list_documents wrapper does
        # not expose that request field. Use its request-preparation seam and
        # rebuild references from the returned resource names. Fakes/newer
        # wrappers use the public keyword path below.
        prepare = getattr(collection, '_prep_list_documents', None)
        firestore_api = getattr(getattr(collection, '_client', None), '_firestore_api', None)
        if callable(prepare) and firestore_api is not None:
            request, kwargs = cast(tuple[Any, dict[str, Any]], prepare(None, None, None))
            if isinstance(request, dict):
                request['show_missing'] = True
            else:
                request.show_missing = True
            documents = firestore_api.list_documents(
                request=request,
                metadata=collection._client._rpc_metadata,
                **kwargs,
            )
            prefix = f'{collection._client._database_string}/documents/'
            return (collection._client.document(document.name.removeprefix(prefix)) for document in documents)
        return collection.list_documents(show_missing=True)

    def walk_collection(collection: Any) -> Iterator[dict[str, Any]]:
        # list_documents(show_missing=True in the Google SDK) is essential: a
        # missing parent document may still own live subcollections.
        references = sorted(list_documents_including_missing(collection), key=lambda ref: ref.path)
        for reference in references:
            snapshot = reference.get()
            if snapshot.exists:
                data = snapshot.to_dict()
                if not isinstance(data, Mapping):
                    raise TypeError(f'Firestore document {reference.path!r} did not return a mapping')
                yield _record(reference.path, data)
            for child in sorted(reference.collections(), key=lambda item: item.id):
                yield from walk_collection(child)

    for collection in sorted(client.collections(), key=lambda item: item.id):
        yield from walk_collection(collection)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp-{os.getpid()}')
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _require_private_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ImportReconciliationError(f'{label} is missing or is not a regular file: {path}')
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ImportReconciliationError(f'{label} must be mode 0600 or stricter: {path}')


def _read_checkpoint(path: Path) -> dict[str, Any]:
    _require_private_file(path, 'import checkpoint')
    raw = json.loads(path.read_text(encoding='utf-8'))
    if raw.get('schema_version') != IMPORT_SCHEMA_VERSION:
        raise ImportReconciliationError(f'unsupported import checkpoint schema at {path}')
    return raw


def _manifest_records(path: Path) -> Iterator[dict[str, Any]]:
    with path.open('r', encoding='utf-8') as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or set(record) != {'collection_id', 'data', 'path'}:
                raise ImportReconciliationError(f'invalid import manifest record at line {line_number}')
            yield record


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def capture_source(client: Any, checkpoint_path: Path) -> dict[str, Any]:
    """Capture one immutable source snapshot and create its resume checkpoint."""
    if checkpoint_path.exists():
        raise FileExistsError(f'import checkpoint already exists: {checkpoint_path}')
    source = _source_authority(client)
    manifest_path = checkpoint_path.with_suffix(checkpoint_path.suffix + '.documents.jsonl')
    if manifest_path.exists():
        raise FileExistsError(f'import manifest already exists: {manifest_path}')
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(f'.{manifest_path.name}.tmp-{os.getpid()}')
    record_digests: list[bytes] = []
    count = 0
    collections: set[str] = set()
    with temporary.open('x', encoding='utf-8') as handle:
        os.chmod(temporary, 0o600)
        for record in walk_source(client):
            encoded = _encoded_record(record)
            handle.write(encoded.decode('utf-8') + '\n')
            record_digests.append(_record_digest(encoded))
            count += 1
            collections.add(str(record['collection_id']))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(manifest_path)
    checkpoint = {
        'schema_version': IMPORT_SCHEMA_VERSION,
        'status': 'captured',
        'manifest': str(manifest_path.resolve()),
        'manifest_sha256': _file_hash(manifest_path),
        'source_count': count,
        'source_content_hash': _content_hash(record_digests),
        'collections': sorted(collections),
        'source_project': source.project,
        'source_database': source.database,
        'source_resolved_endpoint': source.resolved_endpoint,
        'source_emulator_authority': source.emulator_authority,
        'next_index': 0,
    }
    _atomic_json(checkpoint_path, checkpoint)
    return checkpoint


def target_inventory(engine: Optional[Engine] = None) -> Inventory:
    """Enumerate every registered PG document with its real Firestore path."""
    engine = engine or get_engine()
    check_schema(engine)
    records: list[dict[str, Any]] = []
    with engine.connect() as conn:
        registry = conn.execute(
            text(f'SELECT collection_id, table_name FROM {COLLECTION_TABLE} ORDER BY collection_id')
        ).fetchall()
        for collection_id, table in registry:
            rows = conn.execute(text(f'SELECT uid, doc_id, data FROM {table} ORDER BY uid, doc_id')).fetchall()
            for namespace, doc_id, data in rows:
                path = f'{namespace}/{collection_id}/{doc_id}' if namespace else f'{collection_id}/{doc_id}'
                records.append(_stored_record(path, data))
    records.sort(key=lambda item: str(item['path']))
    return _inventory(records)


def _source_inventory(client: Any) -> Inventory:
    records = list(walk_source(client))
    records.sort(key=lambda item: str(item['path']))
    return _inventory(records)


def _collection_tables(engine: Engine) -> dict[str, str]:
    with engine.connect() as conn:
        return {
            str(row[0]): str(row[1])
            for row in conn.execute(text(f'SELECT collection_id, table_name FROM {COLLECTION_TABLE}')).fetchall()
        }


def _write_manifest_record(engine: Engine, collection_tables: Mapping[str, str], record: Mapping[str, Any]) -> None:
    parts = str(record['path']).split('/')
    collection_id = str(record['collection_id'])
    table = collection_tables.get(collection_id)
    if table is None:
        raise ImportReconciliationError(f'collection {collection_id!r} was not provisioned')
    namespace = '/'.join(parts[:-2])
    with engine.begin() as conn:
        conn.execute(
            text(upsert_sql(table)),
            {
                'uid': namespace,
                'doc_id': parts[-1],
                # Manifest data is already the canonical tagged codec; do not
                # encode it a second time.
                'data': json.dumps(record['data'], separators=(',', ':'), ensure_ascii=False),
            },
        )


def run_import(
    source_client: Any,
    checkpoint_path: Path,
    *,
    engine: Optional[Engine] = None,
    checkpoint_interval: int = 100,
    freeze_guard: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Import or resume, then prove live-source/manifest/target reconciliation."""
    if checkpoint_interval < 1:
        raise ValueError('checkpoint_interval must be positive')
    engine = engine or get_engine()
    check_schema(engine)
    if checkpoint_path.exists():
        checkpoint = _read_checkpoint(checkpoint_path)
    else:
        target_before = target_inventory(engine)
        if target_before.count:
            raise ImportReconciliationError(
                'target contains documents but no matching checkpoint; use a fresh database or restore the checkpoint'
            )
        checkpoint = capture_source(source_client, checkpoint_path)

    source = _source_authority(source_client)
    expected_authority = (
        checkpoint.get('source_project'),
        checkpoint.get('source_database'),
        checkpoint.get('source_resolved_endpoint'),
        checkpoint.get('source_emulator_authority'),
    )
    actual_authority = (
        source.project,
        source.database,
        source.resolved_endpoint,
        source.emulator_authority,
    )
    if expected_authority != actual_authority:
        raise ImportReconciliationError(
            'checkpoint Firestore source project/database/endpoint/emulator authority does not match the resume client'
        )

    manifest_path = Path(str(checkpoint['manifest']))
    _require_private_file(manifest_path, 'import manifest')
    if _file_hash(manifest_path) != checkpoint.get('manifest_sha256'):
        raise ImportReconciliationError('import manifest is missing or changed; refusing resume')
    provision_collections((str(item) for item in checkpoint['collections']), engine)
    collection_tables = _collection_tables(engine)
    next_index = int(checkpoint.get('next_index', 0))
    try:
        for index, record in enumerate(_manifest_records(manifest_path)):
            if index < next_index:
                continue
            _write_manifest_record(engine, collection_tables, record)
            next_index = index + 1
            if next_index % checkpoint_interval == 0:
                checkpoint.update(status='importing', next_index=next_index)
                _atomic_json(checkpoint_path, checkpoint)

        checkpoint.update(status='reconciling', next_index=next_index)
        _atomic_json(checkpoint_path, checkpoint)
        manifest_inventory = _inventory(_manifest_records(manifest_path))
        if freeze_guard is not None:
            freeze_guard()
        live_source = _source_inventory(source_client)
        target_state = target_inventory(engine)
        expected = (int(checkpoint['source_count']), str(checkpoint['source_content_hash']))
        observed = {
            'manifest': (manifest_inventory.count, manifest_inventory.content_hash),
            'live_source': (live_source.count, live_source.content_hash),
            'target': (target_state.count, target_state.content_hash),
        }
        mismatches = [name for name, value in observed.items() if value != expected]
        if mismatches:
            raise ImportReconciliationError('count/content reconciliation failed for: ' + ', '.join(mismatches))
        checkpoint.update(
            status='passed',
            completed_at=datetime.now(timezone.utc).isoformat(),
            source_live_count=live_source.count,
            source_live_content_hash=live_source.content_hash,
            target_count=target_state.count,
            target_content_hash=target_state.content_hash,
        )
        _atomic_json(checkpoint_path, checkpoint)
        return checkpoint
    except Exception as exc:
        checkpoint.update(status='failed', next_index=next_index, error_class=type(exc).__name__)
        _atomic_json(checkpoint_path, checkpoint)
        raise
