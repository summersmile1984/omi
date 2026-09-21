"""Exercise the registered resolver through the real projection/privacy owner."""

from database import _client
from fork.patches import collect
from tests.unit.test_conversation_typesense_index import (
    _conversation_data,
    _fake_typesense,
    _firestore_with_doc,
    _policy_db,
)
from utils.conversations import typesense_index


def test_registered_projection_resolves_factory_and_keeps_privacy_deletes(monkeypatch):
    patch = next(item for item in collect() if item.name == 'conversation-search.resolve-client')
    module, original = patch.target()
    monkeypatch.setattr(module, patch.attribute, patch.build(original))
    monkeypatch.setenv('TYPESENSE_HOST', 'localhost')
    monkeypatch.setenv('TYPESENSE_API_KEY', 'synthetic')
    monkeypatch.delenv('TYPESENSE_CONVERSATION_INDEX_WRITES', raising=False)
    selected_client = _firestore_with_doc(_conversation_data())
    monkeypatch.setattr(_client, 'get_firestore_client', lambda: selected_client)
    index, rows = _fake_typesense()
    monkeypatch.setattr(typesense_index, '_typesense_client', lambda: index)

    assert typesense_index.sync_conversation_index_after_write('owner', 'conv-1', db_client=_policy_db())
    assert rows['conv-1']['userId'] == 'owner'
    assert rows['conv-1']['structured']['title'] == 'Standup'
    assert 'transcript_segments' not in rows['conv-1']
    assert typesense_index.sync_conversation_index_after_write('owner', 'conv-1', db_client=_policy_db('e2ee'))
    assert rows == {}
