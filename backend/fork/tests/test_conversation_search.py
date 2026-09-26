"""The upstream Typesense conversation projection honors privacy deletions.

The fork's former `conversation-search.resolve-client` patch became redundant
during the 2026-09-26 sync: upstream's `_resolve_firestore_client` now calls
the memoized `database._client.get_firestore_client` factory itself instead of
returning the factory, which is exactly what the patch used to do (and, once
upstream absorbed that, what made the patch's `original()()` double-call drop
every projection as unindexable). The real projection owner keeps the account
policy, field allowlist, privacy deletion and provider-error behavior this
test drives with the selected client.
"""

from database import _client
from tests.unit.test_conversation_typesense_index import (
    _conversation_data,
    _fake_typesense,
    _firestore_with_doc,
    _policy_db,
)
from utils.conversations import typesense_index


def test_projection_resolves_selected_client_and_keeps_privacy_deletes(monkeypatch):
    monkeypatch.setenv('TYPESENSE_HOST', 'localhost')
    monkeypatch.setenv('TYPESENSE_API_KEY', 'synthetic')
    monkeypatch.delenv(typesense_index.CONVERSATION_INDEX_WRITES_ENV, raising=False)
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
