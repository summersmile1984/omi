from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from database import conversations as conversations_db
from utils.conversations import typesense_index as index


class ObjectNotFound(Exception):
    pass


class _Documents:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def upsert(self, document):
        self.rows[document['id']] = dict(document)

    def __getitem__(self, document_id):
        rows = self.rows

        class _Document:
            def delete(self):
                if document_id not in rows:
                    raise ObjectNotFound()
                del rows[document_id]

        return _Document()

    def delete(self, _parameters):
        count = len(self.rows)
        self.rows.clear()
        return {'num_deleted': count}

    def import_(self, documents, _parameters):
        for document in documents:
            self.upsert(document)
        return '\n'.join(json.dumps({'success': True}) for _ in documents)

    def export(self):
        return '\n'.join(json.dumps(row) for row in self.rows.values())


class _Collection:
    def __init__(self, schema):
        self.schema = schema
        self.documents = _Documents()

    def retrieve(self):
        return self.schema

    def delete(self):
        self.documents.rows.clear()


class _Collections:
    def __init__(self):
        self.values: dict[str, _Collection] = {}

    def __getitem__(self, name):
        if name not in self.values:
            raise ObjectNotFound()
        return self.values[name]

    def create(self, schema):
        self.values[schema['name']] = _Collection(schema)
        return schema


class _Aliases:
    def __init__(self, collections):
        self.collections = collections
        self.values: dict[str, dict[str, str]] = {}

    def __getitem__(self, name):
        aliases = self.values

        class _Alias:
            def retrieve(self):
                if name not in aliases:
                    raise ObjectNotFound()
                return dict(aliases[name])

        return _Alias()

    def upsert(self, name, mapping):
        self.values[name] = dict(mapping)
        self.collections.values[name] = self.collections.values[mapping['collection_name']]
        return dict(mapping)


class _Snapshot:
    def __init__(self, uid: str, conversation_id: str, data: dict):
        self.id = conversation_id
        self._data = data
        self.reference = SimpleNamespace(path=f'users/{uid}/conversations/{conversation_id}')

    def to_dict(self):
        return dict(self._data)


class _Firestore:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def collection_group(self, name):
        assert name == 'conversations'
        return SimpleNamespace(stream=lambda: iter(self.snapshots))


class _MutationDocument:
    def __init__(self, events):
        self.exists = True
        self.events = events
        self.payload = {'data_protection_level': 'standard'}

    def get(self):
        return self

    def to_dict(self):
        return dict(self.payload)

    def create(self, payload):
        self.payload = dict(payload)
        self.events.append('source_create')

    def update(self, payload):
        self.payload.update(payload)
        self.events.append('source_update')

    def collections(self):
        return []

    def delete(self):
        self.events.append('source_delete')


class _MutationCollection:
    def __init__(self, document):
        self._document = document

    def document(self, _document_id):
        return self._document


class _MutationUser:
    def __init__(self, document):
        self._document = document

    def collection(self, name):
        assert name == 'conversations'
        return _MutationCollection(self._document)


class _MutationDB:
    def __init__(self, document):
        self._document = document

    def collection(self, name):
        assert name == 'users'
        return SimpleNamespace(document=lambda _uid: _MutationUser(self._document))


@pytest.fixture
def projection(monkeypatch):
    monkeypatch.setenv(index.CONVERSATION_KEYWORD_PROVIDER_ENV, 'typesense')
    monkeypatch.setenv(index.CONVERSATION_COLLECTION_ENV, 'test_conversations')
    monkeypatch.setenv('TYPESENSE_HOST', 'typesense')
    monkeypatch.setenv('TYPESENSE_API_KEY', 'test-key')
    collections = _Collections()
    client = SimpleNamespace(collections=collections, aliases=_Aliases(collections))
    with patch.object(index, '_typesense_client', return_value=client):
        yield client


def _conversation(conversation_id='c-1', title='Planning', overview='Discuss launch'):
    created = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    return {
        'id': conversation_id,
        'structured': {'title': title, 'overview': overview},
        'created_at': created,
        'started_at': created,
        'finished_at': created,
        'discarded': False,
        'is_locked': False,
        'data_protection_level': 'standard',
    }


def test_schema_is_created_and_exact_mismatch_is_rejected(projection):
    created = index.ensure_conversations_collection()
    assert created['name'] == 'test_conversations'
    created['fields'][0]['type'] = 'int64'
    with pytest.raises(index.ConversationIndexSchemaError, match='does not match'):
        index.ensure_conversations_collection()


def test_document_has_user_scoped_identity_hash_and_e2ee_is_excluded():
    document = index.build_conversation_document('user-1', _conversation())
    assert document is not None
    assert document['conversation_id'] == 'c-1'
    assert document['id'] != 'c-1'
    assert len(document['content_hash']) == 64
    assert index.build_conversation_document('user-1', {**_conversation(), 'data_protection_level': 'e2ee'}) is None


def test_sync_upserts_update_and_delete_are_authoritative(projection):
    index.sync_conversation_document('user-1', 'c-1', conversation=_conversation())
    documents = projection.collections['test_conversations'].documents
    assert next(iter(documents.rows.values()))['title'] == 'Planning'

    index.sync_conversation_document('user-1', 'c-1', conversation=_conversation(title='Renamed'))
    assert next(iter(documents.rows.values()))['title'] == 'Renamed'

    index.delete_conversation_document('user-1', 'c-1')
    assert documents.rows == {}
    index.delete_conversation_document('user-1', 'c-1')


def test_account_purge_uses_user_scoped_typesense_filter(projection):
    index.sync_conversation_document('user-1', 'c-1', conversation=_conversation())
    documents = projection.collections['test_conversations'].documents
    documents.delete = MagicMock(return_value={'num_deleted': 1})

    assert index.purge_user_conversation_index('user-1') == 1
    documents.delete.assert_called_once_with({'filter_by': 'userId:=`user-1`'})


def test_database_create_and_update_paths_synchronously_call_projection(monkeypatch):
    events = []
    document = _MutationDocument(events)
    monkeypatch.setattr(conversations_db, 'db', _MutationDB(document))
    sync = MagicMock(side_effect=lambda *_args: events.append('projection_sync'))
    monkeypatch.setattr(conversations_db, '_sync_conversation_search_projection', sync)

    created = conversations_db.create_conversation_if_absent_with_lifecycle(
        'user-1', {'id': 'c-1', 'data_protection_level': 'standard'}
    )
    conversations_db.update_conversation_title('user-1', 'c-1', 'Renamed')

    assert created is True
    assert events == ['source_create', 'projection_sync', 'source_update', 'projection_sync']
    assert sync.call_args_list[0].args == ('user-1', 'c-1')


def test_database_delete_fails_closed_before_removing_authoritative_source(monkeypatch):
    events = []
    document = _MutationDocument(events)
    monkeypatch.setattr(conversations_db, 'db', _MutationDB(document))
    monkeypatch.setattr(
        conversations_db,
        '_delete_conversation_search_projection',
        MagicMock(side_effect=lambda *_args: events.append('projection_delete')),
    )

    conversations_db.delete_conversation('user-1', 'c-1')

    assert events == ['projection_delete', 'source_delete']


def test_selected_but_unconfigured_typesense_fails_before_client_construction(monkeypatch):
    monkeypatch.setenv(index.CONVERSATION_KEYWORD_PROVIDER_ENV, 'typesense')
    monkeypatch.delenv('TYPESENSE_HOST', raising=False)
    monkeypatch.delenv('TYPESENSE_API_KEY', raising=False)
    with patch.object(index, '_typesense_client') as client:
        with pytest.raises(index.ConversationIndexUnavailableError, match='missing'):
            index.sync_conversation_document('user-1', 'c-1', conversation=_conversation())
    client.assert_not_called()


def test_bulk_rebuild_and_count_hash_reconciliation_detect_drift(projection):
    firestore = _Firestore(
        [
            _Snapshot('user-1', 'c-1', _conversation()),
            _Snapshot('user-2', 'c-2', _conversation('c-2', title='Review')),
            _Snapshot(
                'user-3',
                'c-3',
                {**_conversation('c-3'), 'data_protection_level': 'e2ee'},
            ),
        ]
    )
    assert index.rebuild_conversation_index(firestore_client=firestore, batch_size=1) == 2
    report = index.reconcile_conversation_index(firestore_client=firestore)
    assert report.matches is True
    assert report.expected_count == report.actual_count == 2
    assert report.expected_hash == report.actual_hash
    first_shadow = projection.aliases.values['test_conversations']['collection_name']
    assert first_shadow.startswith('test_conversations__shadow_')
    assert first_shadow in projection.collections.values

    assert index.rebuild_conversation_index(firestore_client=firestore, batch_size=1) == 2
    second_shadow = projection.aliases.values['test_conversations']['collection_name']
    assert second_shadow != first_shadow
    assert projection.collections.values[first_shadow].documents.rows

    documents = projection.collections['test_conversations'].documents
    next(iter(documents.rows.values()))['content_hash'] = 'drift'
    drift = index.reconcile_conversation_index(firestore_client=firestore)
    assert drift.matches is False
    assert len(drift.mismatched_ids) == 1


def test_bulk_import_partial_failure_is_never_acknowledged(projection):
    index.ensure_conversations_collection()

    def recreate_with_failing_import(schema):
        collection = _Collection(schema)
        collection.documents.import_ = MagicMock(return_value=json.dumps({'success': False, 'error': 'bad row'}))
        projection.collections.values[schema['name']] = collection
        return schema

    projection.collections.create = MagicMock(side_effect=recreate_with_failing_import)
    firestore = _Firestore([_Snapshot('user-1', 'c-1', _conversation())])

    with pytest.raises(index.ConversationIndexUnavailableError, match='partial failure'):
        index.rebuild_conversation_index(firestore_client=firestore)
