"""Self-hosted Typesense projection for conversation keyword search.

Firestore remains authoritative.  This module owns the independently named
Typesense collection used by self-host deployments; it does not rely on the
Firebase Typesense extension or its schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, cast

CONVERSATION_KEYWORD_PROVIDER_ENV = 'CONVERSATION_KEYWORD_INDEX_PROVIDER'
CONVERSATION_COLLECTION_ENV = 'CONVERSATION_TYPESENSE_COLLECTION'
DEFAULT_CONVERSATION_INDEX_NAME = 'omi_conversations'
SCHEMA_VERSION = 1
NEUTRAL_DEPLOYMENT_PROFILES = frozenset({'neutral', 'self_hosted', 'self-hosted'})


class ConversationIndexUnavailableError(RuntimeError):
    """The selected conversation keyword projection cannot be updated."""


class ConversationIndexSchemaError(ConversationIndexUnavailableError):
    """The existing Typesense collection is incompatible with this backend."""


@dataclass(frozen=True)
class ConversationIndexReconciliation:
    expected_count: int
    actual_count: int
    expected_hash: str
    actual_hash: str
    missing_ids: tuple[str, ...]
    unexpected_ids: tuple[str, ...]
    mismatched_ids: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not (self.missing_ids or self.unexpected_ids or self.mismatched_ids)

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), 'matches': self.matches}


def conversation_keyword_index_provider(environ: Mapping[str, str] | None = None) -> str:
    """Return the explicit projection owner for this deployment.

    Managed Omi keeps the historical Firebase extension by default.  The
    self-host compose file pins this to ``typesense`` and therefore exercises
    only the implementation in this module. Neutral profiles disable the
    projection when the operator omitted the provider, so an ambient
    Typesense credential cannot create an unexpected index or network path.
    """
    values = os.environ if environ is None else environ
    configured = (values.get(CONVERSATION_KEYWORD_PROVIDER_ENV) or '').strip().lower()
    profile = (values.get('OMI_DEPLOYMENT_PROFILE') or '').strip().lower()
    provider = configured or ('disabled' if profile in NEUTRAL_DEPLOYMENT_PROFILES else 'firebase_extension')
    if provider not in {'firebase_extension', 'typesense', 'disabled'}:
        raise ConversationIndexUnavailableError(f'unsupported conversation keyword index provider {provider!r}')
    return provider


def conversations_collection_name() -> str:
    return (
        os.getenv(CONVERSATION_COLLECTION_ENV, DEFAULT_CONVERSATION_INDEX_NAME).strip()
        or DEFAULT_CONVERSATION_INDEX_NAME
    )


def typesense_configured() -> bool:
    return bool(os.getenv('TYPESENSE_HOST', '').strip() and os.getenv('TYPESENSE_API_KEY', '').strip())


def require_typesense_projection() -> None:
    provider = conversation_keyword_index_provider()
    if provider != 'typesense':
        raise ConversationIndexUnavailableError(f'conversation keyword index provider is {provider!r}, not typesense')
    if not typesense_configured():
        raise ConversationIndexUnavailableError(
            'conversation Typesense projection selected but TYPESENSE_HOST/TYPESENSE_API_KEY are missing'
        )


def _typesense_client() -> Any:
    require_typesense_projection()
    from utils.conversations.search import client

    return client


def _is_object_not_found(exc: BaseException) -> bool:
    return type(exc).__name__ == 'ObjectNotFound' or getattr(exc, 'status_code', None) == 404


def _schema(collection_name: str | None = None) -> Dict[str, Any]:
    return {
        'name': collection_name or conversations_collection_name(),
        'fields': [
            {'name': 'conversation_id', 'type': 'string'},
            {'name': 'userId', 'type': 'string', 'facet': True},
            {'name': 'title', 'type': 'string'},
            {'name': 'overview', 'type': 'string'},
            {'name': 'created_at', 'type': 'int64'},
            {'name': 'started_at', 'type': 'int64'},
            {'name': 'finished_at', 'type': 'int64'},
            {'name': 'discarded', 'type': 'bool', 'facet': True},
            {'name': 'is_locked', 'type': 'bool', 'facet': True},
            {'name': 'schema_version', 'type': 'int32', 'facet': True},
            {'name': 'content_hash', 'type': 'string'},
        ],
        'default_sorting_field': 'created_at',
    }


def _normalized_field(field: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: field[key]
        for key in ('name', 'type', 'facet', 'optional')
        if key in field and not (key in {'facet', 'optional'} and field[key] is False)
    }


def ensure_conversations_collection() -> Dict[str, Any]:
    """Create the self-host collection or validate its exact search contract."""
    require_typesense_projection()
    expected = _schema()
    collection_name = conversations_collection_name()
    alias_target = _conversation_alias_target(collection_name)
    inspected_name = alias_target or collection_name
    try:
        actual = cast(Dict[str, Any], _typesense_client().collections[inspected_name].retrieve())
    except Exception as exc:
        if not _is_object_not_found(exc):
            raise ConversationIndexUnavailableError('unable to inspect conversation Typesense schema') from exc
        try:
            created = _typesense_client().collections.create(expected)
        except Exception as create_exc:
            raise ConversationIndexUnavailableError('unable to create conversation Typesense schema') from create_exc
        return cast(Dict[str, Any], created) if isinstance(created, dict) else expected

    expected_fields = [_normalized_field(field) for field in expected['fields']]
    actual_fields = [_normalized_field(cast(Dict[str, Any], field)) for field in actual.get('fields', [])]
    if actual_fields != expected_fields or actual.get('default_sorting_field') != expected['default_sorting_field']:
        raise ConversationIndexSchemaError(
            f'Typesense collection {collection_name!r} does not match conversation schema v{SCHEMA_VERSION}'
        )
    return actual


def _conversation_alias_target(alias_name: str) -> str | None:
    """Resolve an active Typesense alias without making aliases mandatory at bootstrap."""
    client = _typesense_client()
    aliases = getattr(client, 'aliases', None)
    if aliases is None:
        return None
    try:
        mapping = aliases[alias_name].retrieve()
    except Exception as exc:
        if _is_object_not_found(exc):
            return None
        raise ConversationIndexUnavailableError('unable to inspect conversation Typesense alias') from exc
    target = mapping.get('collection_name') if isinstance(mapping, dict) else None
    if not isinstance(target, str) or not target.strip():
        raise ConversationIndexSchemaError(f'Typesense alias {alias_name!r} has no collection target')
    return target


def _epoch(value: Any, fallback: int = 0) -> int:
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(aware.timestamp())
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            return fallback
    timestamp = getattr(value, 'timestamp', None)
    if callable(timestamp):
        try:
            timestamp_value: Any = timestamp()
            return int(timestamp_value)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return fallback


def _document_id(uid: str, conversation_id: str) -> str:
    return hashlib.sha256(f'{uid}\0{conversation_id}'.encode('utf-8')).hexdigest()


def _content_hash(document: Dict[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key not in {'id', 'content_hash'}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def build_conversation_document(uid: str, conversation: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build the complete searchable projection, excluding E2EE content."""
    conversation_id = str(conversation.get('id') or '').strip()
    if not uid.strip() or not conversation_id or conversation.get('data_protection_level') == 'e2ee':
        return None
    raw_structured = conversation.get('structured')
    structured: Dict[str, Any] = cast(Dict[str, Any], raw_structured) if isinstance(raw_structured, dict) else {}
    title = conversation.get('user_title')
    if not isinstance(title, str):
        title = structured.get('title') if isinstance(structured.get('title'), str) else ''
    overview = structured.get('overview') if isinstance(structured.get('overview'), str) else ''
    created_at = _epoch(conversation.get('created_at'))
    document: Dict[str, Any] = {
        'id': _document_id(uid, conversation_id),
        'conversation_id': conversation_id,
        'userId': uid,
        'title': title,
        'overview': overview,
        'created_at': created_at,
        'started_at': _epoch(conversation.get('started_at'), created_at),
        'finished_at': _epoch(conversation.get('finished_at'), created_at),
        'discarded': bool(conversation.get('discarded', False)),
        'is_locked': bool(conversation.get('is_locked', False)),
        'schema_version': SCHEMA_VERSION,
    }
    document['content_hash'] = _content_hash(document)
    return document


def _read_authoritative_conversation(
    uid: str, conversation_id: str, firestore_client: Any = None
) -> Optional[Dict[str, Any]]:
    if firestore_client is None:
        from database._client import db as firestore_client
    snapshot = (
        firestore_client.collection('users').document(uid).collection('conversations').document(conversation_id).get()
    )
    if not getattr(snapshot, 'exists', False):
        return None
    raw_data: Any = snapshot.to_dict()
    data: Dict[str, Any] = cast(Dict[str, Any], raw_data) if isinstance(raw_data, dict) else {}
    data['id'] = conversation_id
    return data


def delete_conversation_document(uid: str, conversation_id: str) -> None:
    """Delete one exact user-scoped projection; missing is idempotent."""
    if conversation_keyword_index_provider() != 'typesense':
        return
    require_typesense_projection()
    ensure_conversations_collection()
    try:
        _typesense_client().collections[conversations_collection_name()].documents[
            _document_id(uid, conversation_id)
        ].delete()
    except Exception as exc:
        if _is_object_not_found(exc):
            return
        raise ConversationIndexUnavailableError('unable to delete conversation search projection') from exc


def purge_user_conversation_index(uid: str) -> int:
    """Delete every conversation projection for an account, failing closed on provider errors."""
    if conversation_keyword_index_provider() != 'typesense':
        return 0
    require_typesense_projection()
    ensure_conversations_collection()
    escaped_uid = uid.replace('\\', '\\\\').replace('`', '\\`')
    try:
        result = (
            _typesense_client()
            .collections[conversations_collection_name()]
            .documents.delete({'filter_by': f'userId:=`{escaped_uid}`'})
        )
    except Exception as exc:
        if _is_object_not_found(exc):
            return 0
        raise ConversationIndexUnavailableError('unable to purge account conversation search projection') from exc
    payload = cast(Dict[str, Any], result) if isinstance(result, dict) else {}
    return int(payload.get('num_deleted') or 0)


def sync_conversation_document(
    uid: str,
    conversation_id: str,
    *,
    conversation: Optional[Dict[str, Any]] = None,
    firestore_client: Any = None,
) -> None:
    """Replace one projection from authoritative state, or remove it."""
    if conversation_keyword_index_provider() != 'typesense':
        return
    authoritative = conversation
    if authoritative is None:
        authoritative = _read_authoritative_conversation(uid, conversation_id, firestore_client)
    document = build_conversation_document(uid, authoritative) if authoritative is not None else None
    if document is None:
        delete_conversation_document(uid, conversation_id)
        return
    ensure_conversations_collection()
    try:
        _typesense_client().collections[conversations_collection_name()].documents.upsert(document)
    except Exception as exc:
        raise ConversationIndexUnavailableError('unable to update conversation search projection') from exc


def _iter_source_documents(firestore_client: Any = None) -> Iterator[Dict[str, Any]]:
    if firestore_client is None:
        from database._client import db as firestore_client
    for snapshot in firestore_client.collection_group('conversations').stream():
        reference = getattr(snapshot, 'reference', None)
        path = str(getattr(reference, 'path', '') or '')
        parts = [part for part in path.split('/') if part]
        # Both the Google SDK and firestore_pg expose the canonical reference
        # path. firestore_pg's CollectionReference intentionally has no
        # ``parent`` property, so deriving the owner through parent.parent
        # would silently skip every self-host row.
        if len(parts) != 4 or parts[0] != 'users' or parts[2] != 'conversations':
            continue
        uid, conversation_id = parts[1], parts[3]
        raw_data: Any = snapshot.to_dict()
        data: Dict[str, Any] = cast(Dict[str, Any], raw_data) if isinstance(raw_data, dict) else {}
        data['id'] = conversation_id
        document = build_conversation_document(uid, data)
        if document is not None:
            yield document


def _import_results(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, str):
        for line in value.splitlines():
            if line.strip():
                yield cast(Dict[str, Any], json.loads(line))
    elif isinstance(value, list):
        for item in cast(list[Any], value):
            if isinstance(item, dict):
                yield cast(Dict[str, Any], item)


def rebuild_conversation_index(*, firestore_client: Any = None, batch_size: int = 200) -> int:
    """Build, reconcile, and atomically publish a shadow Typesense collection."""
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    require_typesense_projection()
    collection_name = conversations_collection_name()
    shadow_name = f'{collection_name}__shadow_{uuid.uuid4().hex[:12]}'
    client = _typesense_client()
    try:
        client.collections.create(_schema(shadow_name))
    except Exception as exc:
        raise ConversationIndexUnavailableError('unable to create conversation Typesense shadow collection') from exc
    try:
        documents_api = client.collections[shadow_name].documents
        total = 0
        batch: list[Dict[str, Any]] = []
        for document in _iter_source_documents(firestore_client):
            batch.append(document)
            if len(batch) >= batch_size:
                total += _import_batch(documents_api, batch)
                batch = []
        if batch:
            total += _import_batch(documents_api, batch)
        report = _reconcile_conversation_collection(shadow_name, firestore_client=firestore_client)
        if not report.matches:
            raise ConversationIndexUnavailableError('conversation Typesense shadow reconciliation failed')
        aliases = getattr(client, 'aliases', None)
        if aliases is None:
            raise ConversationIndexUnavailableError('Typesense aliases API is required for atomic conversation rebuild')
        try:
            aliases.upsert(collection_name, {'collection_name': shadow_name})
        except Exception as exc:
            raise ConversationIndexUnavailableError(
                'unable to atomically publish conversation Typesense alias'
            ) from exc
        return total
    except Exception:
        try:
            client.collections[shadow_name].delete()
        except Exception:
            pass
        raise


def _import_batch(documents_api: Any, batch: list[Dict[str, Any]]) -> int:
    try:
        result = documents_api.import_(batch, {'action': 'upsert'})
        rows = list(_import_results(result))
    except Exception as exc:
        raise ConversationIndexUnavailableError('conversation Typesense bulk import failed') from exc
    if len(rows) != len(batch) or any(row.get('success') is not True for row in rows):
        raise ConversationIndexUnavailableError('conversation Typesense bulk import returned partial failure')
    return len(batch)


def _digest(items: Dict[str, str]) -> str:
    canonical = '\n'.join(f'{item_id}:{items[item_id]}' for item_id in sorted(items))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def reconcile_conversation_index(*, firestore_client: Any = None) -> ConversationIndexReconciliation:
    """Compare authoritative Firestore count/hash with the Typesense export."""
    ensure_conversations_collection()
    return _reconcile_conversation_collection(conversations_collection_name(), firestore_client=firestore_client)


def _reconcile_conversation_collection(
    collection_name: str,
    *,
    firestore_client: Any = None,
) -> ConversationIndexReconciliation:
    """Compare authority against one physical collection or active alias."""
    expected = {str(doc['id']): str(doc['content_hash']) for doc in _iter_source_documents(firestore_client)}
    try:
        exported = _typesense_client().collections[collection_name].documents.export()
    except Exception as exc:
        raise ConversationIndexUnavailableError('unable to export conversation Typesense projection') from exc
    actual: Dict[str, str] = {}
    for row in _import_results(exported):
        item_id = str(row.get('id') or '')
        if item_id:
            actual[item_id] = str(row.get('content_hash') or '')
    expected_ids = set(expected)
    actual_ids = set(actual)
    return ConversationIndexReconciliation(
        expected_count=len(expected),
        actual_count=len(actual),
        expected_hash=_digest(expected),
        actual_hash=_digest(actual),
        missing_ids=tuple(sorted(expected_ids - actual_ids)),
        unexpected_ids=tuple(sorted(actual_ids - expected_ids)),
        mismatched_ids=tuple(
            sorted(item_id for item_id in expected_ids & actual_ids if expected[item_id] != actual[item_id])
        ),
    )
