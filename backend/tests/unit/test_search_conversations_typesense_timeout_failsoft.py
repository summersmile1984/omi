import os

os.environ.setdefault('TYPESENSE_API_KEY', 'test-key')
os.environ.setdefault('TYPESENSE_HOST', 'localhost')
os.environ.setdefault('TYPESENSE_HOST_PORT', '8108')

from unittest.mock import patch

import pytest

from utils.conversations.search import ConversationSearchUnavailableError, search_conversations


def test_search_conversations_typesense_timeout_failsoft():
    with patch('utils.conversations.search.client') as mock_client:
        mock_client.collections['conversations'].documents.search.side_effect = TimeoutError(
            'HTTPSConnectionPool(host="typesense"): Read timed out (read timeout=2)'
        )
        with pytest.raises(ConversationSearchUnavailableError):
            search_conversations(uid='uid-1', query='meeting notes')


def test_search_conversations_typesense_service_unavailable_failsoft():
    class ServiceUnavailable(Exception):
        pass

    ServiceUnavailable.__module__ = 'typesense.exceptions'
    with patch('utils.conversations.search.client') as mock_client:
        mock_client.collections['conversations'].documents.search.side_effect = ServiceUnavailable(
            '{"message":"not ready"}'
        )
        with pytest.raises(ConversationSearchUnavailableError):
            search_conversations(uid='uid-1', query='meeting notes')


def test_search_conversations_non_transient_error_is_typed_unavailable():
    with patch('utils.conversations.search.client') as mock_client:
        mock_client.collections['conversations'].documents.search.side_effect = ValueError('bad query shape')
        with pytest.raises(ConversationSearchUnavailableError) as error:
            search_conversations(uid='uid-1', query='meeting notes')
    assert error.value.retryable is False


def test_selected_but_unconfigured_provider_is_typed_and_never_constructs_client(monkeypatch):
    monkeypatch.setenv('CONVERSATION_KEYWORD_INDEX_PROVIDER', 'typesense')
    monkeypatch.delenv('TYPESENSE_HOST', raising=False)
    monkeypatch.delenv('TYPESENSE_API_KEY', raising=False)
    with patch('utils.conversations.search._get_typesense_client') as get_client:
        with pytest.raises(ConversationSearchUnavailableError) as error:
            search_conversations(uid='uid-1', query='meeting notes')
    assert error.value.retryable is False
    assert error.value.provider == 'typesense'
    get_client.assert_not_called()


def test_neutral_omission_is_typed_disabled_before_ambient_typesense(monkeypatch):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('CONVERSATION_KEYWORD_INDEX_PROVIDER', raising=False)
    monkeypatch.setenv('TYPESENSE_HOST', 'ambient-typesense')
    monkeypatch.setenv('TYPESENSE_API_KEY', 'ambient-key')
    with patch('utils.conversations.search._get_typesense_client') as get_client:
        with pytest.raises(ConversationSearchUnavailableError) as error:
            search_conversations(uid='uid-1', query='meeting notes')
    assert error.value.retryable is False
    assert error.value.provider == 'disabled'
    get_client.assert_not_called()


def test_invalid_provider_value_is_typed_unavailable(monkeypatch):
    monkeypatch.setenv('CONVERSATION_KEYWORD_INDEX_PROVIDER', 'unknown-provider')

    with pytest.raises(ConversationSearchUnavailableError) as error:
        search_conversations(uid='uid-1', query='meeting notes')

    assert error.value.retryable is False
    assert error.value.provider == 'unknown-provider'
