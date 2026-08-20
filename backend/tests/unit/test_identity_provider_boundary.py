from types import SimpleNamespace

import pytest

from utils import auth_shim, identity


def test_better_auth_invalid_token_is_not_reported_as_provider_outage(monkeypatch):
    monkeypatch.setenv('AUTH_PROVIDER', 'better_auth')
    monkeypatch.setattr(
        auth_shim,
        'verify_id_token',
        lambda _token: (_ for _ in ()).throw(auth_shim.InvalidIdTokenError('bad token')),
    )

    with pytest.raises(identity.IdentityInvalidToken, match='bad token'):
        identity.verify_id_token('token')


def test_better_auth_jwks_outage_is_retryable_provider_failure(monkeypatch):
    monkeypatch.setenv('AUTH_PROVIDER', 'better_auth')
    monkeypatch.setattr(
        auth_shim,
        'verify_id_token',
        lambda _token: (_ for _ in ()).throw(auth_shim.CertificateFetchError('offline')),
    )

    with pytest.raises(identity.IdentityProviderUnavailable, match='offline'):
        identity.verify_id_token('token')


def test_better_auth_lifecycle_uses_authenticated_internal_boundary(monkeypatch):
    monkeypatch.setenv('AUTH_PROVIDER', 'better_auth')
    monkeypatch.setenv('AUTH_SERVER_INTERNAL_URL', 'http://auth.internal:3000')
    monkeypatch.setenv('AUTH_INTERNAL_ADMIN_SECRET', 'internal-secret')
    calls = []

    def request(method, url, headers, timeout):
        calls.append((method, url, headers, timeout))
        return SimpleNamespace(status_code=200, content=b'{}', json=lambda: {})

    monkeypatch.setattr(identity.httpx, 'request', request)

    identity.delete_user('user/with?reserved')

    assert calls == [
        (
            'DELETE',
            'http://auth.internal:3000/internal/users/user%2Fwith%3Freserved',
            {'Authorization': 'Bearer internal-secret'},
            5.0,
        )
    ]


def test_better_auth_browser_exchange_revokes_temporary_session(monkeypatch):
    monkeypatch.setenv('AUTH_PROVIDER', 'better_auth')
    monkeypatch.setenv('AUTH_SERVER_INTERNAL_URL', 'http://auth.internal:3000')
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith('/sign-in/email'):
            return SimpleNamespace(status_code=200, headers={'set-auth-token': 'signed-session'})
        if url.endswith('/token'):
            return SimpleNamespace(status_code=200, json=lambda: {'token': 'identity-jwt'})
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(identity.httpx, 'request', request)

    assert identity.authenticate_email_to_jwt('person@example.com', 'password') == 'identity-jwt'
    assert [call[:2] for call in calls] == [
        ('POST', 'http://auth.internal:3000/api/auth/sign-in/email'),
        ('GET', 'http://auth.internal:3000/api/auth/token'),
        ('POST', 'http://auth.internal:3000/api/auth/sign-out'),
    ]
    assert calls[1][2]['headers']['Authorization'] == 'Bearer signed-session'


def test_better_auth_browser_exchange_preserves_invalid_credential_class(monkeypatch):
    monkeypatch.setenv('AUTH_PROVIDER', 'better_auth')
    monkeypatch.setenv('AUTH_SERVER_INTERNAL_URL', 'http://auth.internal:3000')
    monkeypatch.setattr(
        identity.httpx,
        'request',
        lambda *_args, **_kwargs: SimpleNamespace(status_code=401, headers={}),
    )

    with pytest.raises(identity.IdentityInvalidToken, match='invalid email or password'):
        identity.authenticate_email_to_jwt('person@example.com', 'wrong')


def test_production_better_auth_requires_explicit_secure_contract(monkeypatch):
    monkeypatch.setenv('AUTH_PROVIDER', 'better_auth')
    monkeypatch.setenv('ENVIRONMENT', 'production')
    for name in (
        'AUTH_JWKS_URL',
        'AUTH_JWT_ISSUER',
        'AUTH_JWT_AUDIENCE',
        'AUTH_SERVER_INTERNAL_URL',
        'AUTH_INTERNAL_ADMIN_SECRET',
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match='production Better Auth configuration missing'):
        identity.validate_identity_configuration()


def test_production_better_auth_rejects_insecure_identity_endpoints(monkeypatch):
    monkeypatch.setenv('AUTH_PROVIDER', 'better_auth')
    monkeypatch.setenv('ENVIRONMENT', 'production')
    monkeypatch.setenv('AUTH_JWKS_URL', 'http://auth.example/api/auth/jwks')
    monkeypatch.setenv('AUTH_JWT_ISSUER', 'https://auth.example')
    monkeypatch.setenv('AUTH_JWT_AUDIENCE', 'https://api.example')
    monkeypatch.setenv('AUTH_SERVER_INTERNAL_URL', 'https://auth.internal')
    monkeypatch.setenv('AUTH_INTERNAL_ADMIN_SECRET', 'secret')

    with pytest.raises(RuntimeError, match='AUTH_JWKS_URL must use https'):
        identity.validate_identity_configuration()


@pytest.mark.parametrize(
    'internal_origin',
    [
        'http://auth-server:3000',
        'http://127.0.0.1:3000',
        'http://10.42.0.8:3000',
        'http://auth.private.internal:3000',
        'http://auth.default.svc:3000',
    ],
)
def test_production_better_auth_allows_explicit_private_http_control_plane(monkeypatch, internal_origin):
    monkeypatch.setenv('AUTH_PROVIDER', 'better_auth')
    monkeypatch.setenv('ENVIRONMENT', 'production')
    monkeypatch.setenv('AUTH_JWKS_URL', f'{internal_origin}/api/auth/jwks')
    monkeypatch.setenv('AUTH_JWT_ISSUER', 'https://auth.operator.example')
    monkeypatch.setenv('AUTH_JWT_AUDIENCE', 'https://auth.operator.example')
    monkeypatch.setenv('AUTH_SERVER_INTERNAL_URL', internal_origin)
    monkeypatch.setenv('AUTH_INTERNAL_ADMIN_SECRET', 'secret')
    monkeypatch.setenv('AUTH_INTERNAL_ALLOW_HTTP', 'true')

    identity.validate_identity_configuration()


def test_production_better_auth_legacy_config_rejects_private_http_without_opt_in(monkeypatch):
    monkeypatch.setenv('AUTH_PROVIDER', 'better_auth')
    monkeypatch.setenv('ENVIRONMENT', 'production')
    monkeypatch.setenv('AUTH_JWKS_URL', 'http://auth-server:3000/api/auth/jwks')
    monkeypatch.setenv('AUTH_JWT_ISSUER', 'https://auth.operator.example')
    monkeypatch.setenv('AUTH_JWT_AUDIENCE', 'https://auth.operator.example')
    monkeypatch.setenv('AUTH_SERVER_INTERNAL_URL', 'http://auth-server:3000')
    monkeypatch.setenv('AUTH_INTERNAL_ADMIN_SECRET', 'secret')
    monkeypatch.delenv('AUTH_INTERNAL_ALLOW_HTTP', raising=False)

    with pytest.raises(RuntimeError, match='AUTH_JWKS_URL must use https'):
        identity.validate_identity_configuration()


@pytest.mark.parametrize(
    'public_origin',
    ['http://auth.example.com:3000', 'http://169.254.169.254', 'http://169.254.169.254.sslip.io'],
)
def test_production_better_auth_rejects_public_http_even_with_private_opt_in(monkeypatch, public_origin):
    monkeypatch.setenv('AUTH_PROVIDER', 'better_auth')
    monkeypatch.setenv('ENVIRONMENT', 'production')
    monkeypatch.setenv('AUTH_JWKS_URL', f'{public_origin}/api/auth/jwks')
    monkeypatch.setenv('AUTH_JWT_ISSUER', 'https://auth.operator.example')
    monkeypatch.setenv('AUTH_JWT_AUDIENCE', 'https://auth.operator.example')
    monkeypatch.setenv('AUTH_SERVER_INTERNAL_URL', 'http://auth-server:3000')
    monkeypatch.setenv('AUTH_INTERNAL_ADMIN_SECRET', 'secret')
    monkeypatch.setenv('AUTH_INTERNAL_ALLOW_HTTP', 'true')

    with pytest.raises(RuntimeError, match='AUTH_JWKS_URL must use https'):
        identity.validate_identity_configuration()
