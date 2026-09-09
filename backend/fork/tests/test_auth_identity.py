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


@pytest.fixture
def referral_app(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fork import referral_transport
    from routers import referrals as router
    from utils.referrals import referral_claim_patch

    monkeypatch.setenv('ENCRYPTION_SECRET', 'synthetic-referral-contract-at-least-32')
    monkeypatch.setenv('REFERRAL_PUBLIC_BASE_URL', 'https://wrong.invalid')
    row = {
        'target': 'self_hosted',
        'api_base_url': 'https://api.eddy.invalid',
        'web_base_url': 'https://web.eddy.invalid',
    }
    monkeypatch.setattr(referral_transport, 'current', lambda: row)
    store = {}

    def claim(uid, referrer_uid, *, is_new_user):
        # Controlled persistence seam; execute the real eligibility policy.
        # The shared HTTP lane separately exercises actual PG/D1 transactions.
        patch, reason = referral_claim_patch(
            referred_uid=uid,
            referrer_uid=referrer_uid,
            is_new_user=is_new_user,
            user_data=store.get(uid),
        )
        if patch is not None:
            store.setdefault(uid, {}).update(patch)
        return patch is not None, reason

    monkeypatch.setattr(router, 'claim_referral_trial', claim)
    events = mock.Mock()
    monkeypatch.setattr(router, 'emit_posthog_event', events)
    sdk = mock.Mock(side_effect=AssertionError('Firebase referral lookup was called'))
    monkeypatch.setattr(router.firebase_admin.auth, 'get_user', sdk)
    app = FastAPI()
    app.include_router(router.router)
    referral_transport.install(app)
    app.dependency_overrides[router.auth.get_current_user_uid] = lambda: 'existing-user'
    with TestClient(app) as client:
        yield client, store, row, app, router, sdk, events


def test_referral_uses_admitted_origins_and_authoritative_age_once(authority, referral_app):
    from datetime import datetime, timezone
    from utils.referrals import create_referral_code

    calls, replies = authority
    client, store, _, _, _, sdk, _ = referral_app
    issued = client.get('/v1/users/me/referral')
    assert issued.status_code == 200 and issued.headers['cache-control'] == 'no-store'
    assert issued.json()['referral_url'] == 'https://api.eddy.invalid/r/' + create_referral_code('existing-user')
    code = create_referral_code('inviter')
    captured = client.get('/r/' + code, follow_redirects=False)
    assert captured.status_code == 302
    assert captured.headers['location'] == f'https://web.eddy.invalid/login?referral={code}&environment=prod'
    assert captured.headers['cache-control'] == 'no-store'
    assert captured.headers['referrer-policy'] == 'no-referrer'
    assert all(value in captured.headers['set-cookie'] for value in ('HttpOnly', 'Secure', 'SameSite=lax'))
    replies.extend([(200, {'user': profile(createdAt=datetime.now(timezone.utc).isoformat())})] * 2)
    assert client.post('/v1/users/me/referral/claim', json={'code': code}).json() == {'claimed': True, 'trial_days': 30}
    assert client.post('/v1/users/me/referral/claim', json={'code': code}).json() == {
        'claimed': False,
        'trial_days': 30,
    }
    user = store['existing-user']
    assert user['subscription']['plan'] == 'operator'
    assert user['subscription']['current_period_end'] - user['subscription']['current_period_start'] == 2592000
    assert user['referral']['referrer_uid'] == 'inviter'
    assert len(calls) == 2
    sdk.assert_not_called()


@pytest.mark.parametrize('created_at', [None, '2020-01-01T00:00:00Z', '2099-01-01T00:00:00Z'])
def test_referral_legacy_or_outside_window_never_mints_entitlement(authority, referral_app, created_at):
    from utils.referrals import create_referral_code

    _, replies = authority
    client, store, *_ = referral_app
    replies.append((200, {'user': profile(createdAt=created_at)}))
    response = client.post('/v1/users/me/referral/claim', json={'code': create_referral_code('inviter')})
    assert response.status_code == 200 and response.json() == {'claimed': False, 'trial_days': 30}
    assert not store


@pytest.mark.parametrize(
    'reply,status',
    [
        ((404, {'error': 'user_not_found'}), 401),
        ((200, {'user': profile(banned=True)}), 401),
        ((200, {'user': profile(createdAt='private-invalid-date')}), 503),
        ((200, {'user': profile(createdAt='2026-09-06T00:00:00')}), 503),
        ((503, {'error': 'private diagnostic'}), 503),
        (httpx.ReadTimeout('private timeout'), 503),
    ],
)
def test_referral_unknown_authority_cannot_grant_or_hide_retry(authority, referral_app, reply, status):
    from utils.referrals import create_referral_code

    _, replies = authority
    client, store, _, _, _, sdk, events = referral_app
    replies.append(reply)
    response = client.post('/v1/users/me/referral/claim', json={'code': create_referral_code('inviter')})
    assert response.status_code == status and 'private' not in response.text
    if status == 503:
        assert response.json()['detail'] == {'code': 'auth_service_unavailable', 'retryable': True}
    assert not store
    sdk.assert_not_called()
    events.assert_not_called()


def test_referral_keeps_auth_decoding_and_upstream_route_owner(authority, referral_app, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fork import referral_transport
    from utils.referrals import create_referral_code

    calls, _ = authority
    client, store, row, app, router, _, _ = referral_app
    assert client.post('/v1/users/me/referral/claim', json={}).status_code == 422
    assert client.post('/v1/users/me/referral/claim', json={'code': 'invalid'}).status_code == 404

    def denied():
        raise HTTPException(401, 'unauthorized')

    app.dependency_overrides[router.auth.get_current_user_uid] = denied
    assert client.get('/v1/users/me/referral').status_code == 401
    assert client.post('/v1/users/me/referral/claim', json={'code': create_referral_code('inviter')}).status_code == 401
    assert not store and not calls
    row['target'] = 'omi_cloud'
    upstream = FastAPI()
    upstream.include_router(router.router)
    referral_transport.install(upstream)
    assert all(route.dependant.call is route.endpoint for route in upstream.routes if hasattr(route, 'dependant'))
