"""Identity deletion and final completion use the actual internal HTTP contract."""

from contextlib import nullcontext
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
