"""Identity deletion and final completion use the actual internal HTTP contract."""

from contextlib import nullcontext
import importlib
import types
from unittest import mock

import httpx
import pytest

from fork import auth_identity, provider_guard
from fork.patches.auth import patches
from fork.registry import build_registry
from testing.import_isolation import stub_modules


@pytest.fixture
def authority(monkeypatch):
    monkeypatch.setenv('AUTH_SERVER_INTERNAL_URL', 'http://identity.invalid')
    monkeypatch.setenv('AUTH_INTERNAL_ADMIN_SECRET', 'synthetic-internal-key')
    monkeypatch.setenv('AUTH_INTERNAL_ALLOW_HTTP', 'true')
    calls = []
    replies = []

    def request(method, url, **kwargs):
        calls.append((method, url))
        assert kwargs['headers'] == {'authorization': 'Bearer synthetic-internal-key'}
        assert kwargs['follow_redirects'] is False
        value = replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return httpx.Response(value[0], json=value[1])

    monkeypatch.setattr(auth_identity.httpx, 'request', request)
    return calls, replies


@pytest.fixture
def profile_consumer():
    client = types.ModuleType('database._client')
    client.db = mock.MagicMock()
    client.db.collection.return_value.document.return_value.get.return_value.exists = False
    redis = types.ModuleType('database.redis_db')
    redis.cache_user_name = mock.Mock()
    with stub_modules({'database._client': client, 'database.redis_db': redis, 'database.auth': None}):
        consumer = importlib.import_module('database.auth')
        # Capture the same public functions imported by conversation/model code
        # before applying the production registry at its SDK leaf.
        lookup, name = consumer.get_user_from_uid, consumer.get_user_name
        with mock.patch.object(
            consumer.auth, 'get_user', side_effect=AssertionError('Firebase profile was called')
        ) as sdk:
            registry = build_registry(patch for patch in patches() if patch.module == 'database.auth')
            yield registry, lookup, name, sdk, redis.cache_user_name


def profile(**updates):
    return {
        'id': 'existing-user',
        'email': 'synthetic@example.invalid',
        'emailVerified': True,
        'name': 'Synthetic Person',
        'image': None,
        **updates,
    }


def test_captured_profile_consumers_use_selected_identity_before_model_work(authority, profile_consumer):
    calls, replies = authority
    registry, lookup, name, sdk, cache = profile_consumer
    registry.apply({'target': 'self_hosted'})
    replies.extend([(200, {'user': profile()}), (200, {'user': profile()})])
    assert lookup('existing-user') == {
        'uid': 'existing-user',
        'email': 'synthetic@example.invalid',
        'email_verified': True,
        'phone_number': None,
        'display_name': 'Synthetic Person',
        'photo_url': None,
        'disabled': False,
    }
    assert name('existing-user') == 'Synthetic'
    cache.assert_called_once_with('existing-user', 'Synthetic', ttl=3600)
    sdk.assert_not_called()
    assert calls == [('GET', 'http://identity.invalid/internal/users/existing-user')] * 2


def test_absent_optional_profile_keeps_default_without_switching_authority(authority, profile_consumer):
    _, replies = authority
    registry, lookup, name, sdk, cache = profile_consumer
    registry.apply({'target': 'self_hosted'})
    replies.extend([(404, {'error': 'user_not_found'})] * 3)
    assert lookup('existing-user') is None
    assert name('existing-user') == 'The User'
    assert name('existing-user', use_default=False) is None
    sdk.assert_not_called()
    cache.assert_not_called()


@pytest.mark.parametrize(
    'reply',
    [
        (200, {'user': profile(id='another-user')}),
        (200, {'user': profile(emailVerified=1)}),
        (200, {'user': profile(name=['private response'])}),
        (200, {'user': profile(banned='private response')}),
        (200, {'user': profile(phoneNumber=123)}),
        (404, {'error': 'private wrong-route response'}),
        (401, {'error': 'private response'}),
        (503, {'error': 'private response'}),
        httpx.ReadTimeout('private transport diagnostic'),
    ],
)
def test_profile_failure_is_sanitized_and_observable_without_firebase(authority, profile_consumer, caplog, reply):
    _, replies = authority
    registry, lookup, _, sdk, _ = profile_consumer
    registry.apply({'target': 'self_hosted'})
    replies.append(reply)
    assert lookup('existing-user') is None
    sdk.assert_not_called()
    assert 'omi_fallback_event' in caplog.text
    assert 'private' not in caplog.text


def test_upstream_profile_keeps_original_sdk_owner(authority, profile_consumer):
    calls, _ = authority
    registry, lookup, _, sdk, _ = profile_consumer
    sdk.side_effect = None
    sdk.return_value = types.SimpleNamespace(
        uid='legacy',
        email=None,
        email_verified=False,
        phone_number=None,
        display_name=None,
        photo_url=None,
        disabled=False,
    )
    registry.apply({'target': 'omi_cloud'})
    assert lookup('legacy')['uid'] == 'legacy'
    sdk.assert_called_once_with('legacy')
    assert calls == []


@pytest.mark.parametrize('status,body', [(200, {'success': True}), (404, {'error': 'user_not_found'})])
def test_existing_and_already_absent_identity_use_the_selected_authority(authority, status, body):
    calls, replies = authority
    replies.extend([(status, body), (200, {'users': 0, 'sessions': 0, 'accounts': 0})])
    # Exercise the same verified symbol replacement used by the worker's
    # `from utils.other import endpoints as auth` module reference.
    endpoint = types.ModuleType('utils.other.endpoints')
    endpoint.delete_account = mock.Mock(side_effect=AssertionError('Firebase delete was called'))
    deletion_patch = next(patch for patch in patches() if patch.attribute == 'delete_account')
    with stub_modules({'utils.other.endpoints': endpoint}):
        registry = build_registry([deletion_patch])
        registry.apply({'target': 'omi_cloud'})
        assert not registry.applied
        registry.apply({'target': 'self_hosted'})
        assert endpoint.delete_account('existing-user') == {'message': 'User deleted'}
    assert calls == [
        ('DELETE', 'http://identity.invalid/internal/users/existing-user'),
        ('GET', 'http://identity.invalid/internal/users/existing-user/residuals'),
    ]


@pytest.mark.parametrize(
    'result',
    [
        (200, {'users': 0, 'sessions': 1, 'accounts': 0}),
        (200, {'users': False, 'sessions': 0, 'accounts': 0}),
        (200, {'users': 0}),
        (404, {'error': 'user_not_found'}),
        (503, {'error': 'synthetic diagnostic'}),
    ],
)
def test_residuals_or_unproved_absence_never_publish_completion(authority, monkeypatch, result):
    _, replies = authority
    replies.append(result)
    monkeypatch.setattr(provider_guard, 'account_lock', lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(provider_guard, 'assert_erased', mock.Mock())
    completion = mock.Mock()
    with pytest.raises(auth_identity.IdentityAuthorityUnavailable):
        provider_guard.complete(completion)('existing-user')
    completion.assert_not_called()


@pytest.mark.parametrize(
    'result',
    [
        (200, {'success': False}),
        (200, {'success': 1}),
        (401, {'error': 'unauthorized'}),
        (404, {'error': 'route_missing'}),
        (503, {'error': 'private server response'}),
        httpx.ReadTimeout('private transport diagnostic'),
    ],
)
def test_delete_failure_and_unknown_outcome_stay_retryable(authority, result):
    calls, replies = authority
    replies.append(result)
    with pytest.raises(auth_identity.IdentityAuthorityUnavailable) as error:
        auth_identity.delete_account('existing-user')
    assert 'private' not in str(error.value) and len(calls) == 1


def test_missing_authority_never_attempts_firebase_or_network(authority, monkeypatch):
    calls, _ = authority
    monkeypatch.delenv('AUTH_INTERNAL_ADMIN_SECRET')
    with pytest.raises(auth_identity.IdentityAuthorityUnavailable):
        auth_identity.delete_account('existing-user')
    assert calls == []


@pytest.mark.parametrize('uid,segment', [('.', '%2E'), ('..', '%2E%2E'), ('%2E%2E', '%252E%252E')])
def test_imported_dot_identity_survives_actual_http_request_normalization(authority, monkeypatch, uid, segment):
    requests = []

    def transport(request):
        requests.append(request)
        suffix = '/residuals' if request.method == 'GET' else ''
        if request.url.raw_path != f'/internal/users/{segment}{suffix}'.encode():
            return httpx.Response(404, json={'error': 'route_missing'})
        payload = {'users': 0, 'sessions': 0, 'accounts': 0} if suffix else {'success': True}
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(auth_identity.httpx, 'request', client.request)
        assert auth_identity.delete_account(uid) == {'message': 'User deleted'}
    assert [request.method for request in requests] == ['DELETE', 'GET']
