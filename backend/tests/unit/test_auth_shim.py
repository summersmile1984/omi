import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from utils import auth_shim


def _es256_credential(claims):
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "test-key", "alg": "ES256", "use": "sig"})
    token = jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": "test-key"})
    return token, public_jwk


def _valid_claims(**overrides):
    now = int(time.time())
    return {
        "sub": "user-123",
        "iss": "http://127.0.0.1:3000",
        "aud": "http://127.0.0.1:3000",
        "iat": now,
        "exp": now + 900,
        **overrides,
    }


def test_verify_id_token_accepts_es256_and_preserves_uid(monkeypatch):
    token, public_jwk = _es256_credential(_valid_claims(uid="user-123", sub="subject-123"))
    monkeypatch.setattr(auth_shim, "_fetch_jwks", lambda: {"keys": [public_jwk]})

    claims = auth_shim.verify_id_token(token)

    assert claims["uid"] == "user-123"
    assert claims["sub"] == "subject-123"


def test_verify_id_token_rejects_algorithm_key_mismatch(monkeypatch):
    token, public_jwk = _es256_credential(_valid_claims(uid="user-123"))
    public_jwk["alg"] = "RS256"
    monkeypatch.setattr(auth_shim, "_fetch_jwks", lambda: {"keys": [public_jwk]})

    with pytest.raises(auth_shim.InvalidIdTokenError, match="does not match JWK alg"):
        auth_shim.verify_id_token(token)


def test_verify_id_token_rejects_missing_identity(monkeypatch):
    token, public_jwk = _es256_credential(
        {
            "scope": "read",
            "iss": "http://127.0.0.1:3000",
            "aud": "http://127.0.0.1:3000",
            "exp": int(time.time()) + 900,
        }
    )
    monkeypatch.setattr(auth_shim, "_fetch_jwks", lambda: {"keys": [public_jwk]})

    with pytest.raises(auth_shim.InvalidIdTokenError, match="missing uid/sub"):
        auth_shim.verify_id_token(token)


@pytest.mark.parametrize(
    ("claim", "value", "message"),
    [
        ("iss", "https://other-auth.example", "Invalid issuer"),
        ("aud", "https://other-api.example", "Audience doesn't match"),
    ],
)
def test_verify_id_token_rejects_wrong_token_boundary(monkeypatch, claim, value, message):
    token, public_jwk = _es256_credential(_valid_claims(**{claim: value}))
    monkeypatch.setattr(auth_shim, "_fetch_jwks", lambda: {"keys": [public_jwk]})

    with pytest.raises(auth_shim.InvalidIdTokenError, match=message):
        auth_shim.verify_id_token(token)


def test_fetch_jwks_reads_url_at_call_boundary(monkeypatch):
    requested_urls = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": []}

    def _get(url, timeout):
        requested_urls.append((url, timeout))
        return _Response()

    monkeypatch.setattr(auth_shim.httpx, "get", _get)
    monkeypatch.setattr(auth_shim, "_jwks", None)
    monkeypatch.setattr(auth_shim, "_jwks_fetched_at", 0.0)
    monkeypatch.setattr(auth_shim, "_jwks_source_url", None)
    monkeypatch.setenv("AUTH_JWKS_URL", "http://auth-one/api/auth/jwks")
    auth_shim._fetch_jwks()
    monkeypatch.setenv("AUTH_JWKS_URL", "http://auth-two/api/auth/jwks")
    auth_shim._fetch_jwks()

    assert [url for url, _ in requested_urls] == [
        "http://auth-one/api/auth/jwks",
        "http://auth-two/api/auth/jwks",
    ]


def test_fetch_jwks_rejects_non_object_payload_as_provider_error(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    monkeypatch.setattr(auth_shim.httpx, 'get', lambda *_args, **_kwargs: _Response())
    monkeypatch.setattr(auth_shim, '_jwks', None)
    monkeypatch.setattr(auth_shim, '_jwks_fetched_at', 0.0)
    monkeypatch.setattr(auth_shim, '_jwks_source_url', None)

    with pytest.raises(auth_shim.CertificateFetchError, match='missing keys'):
        auth_shim._fetch_jwks()


def test_fetch_jwks_uses_bounded_stale_cache_then_fails_closed(monkeypatch):
    url = 'https://auth.example/api/auth/jwks'
    monkeypatch.setenv('AUTH_JWKS_URL', url)
    monkeypatch.setenv('AUTH_JWKS_CACHE_TTL_SECONDS', '0')
    monkeypatch.setenv('AUTH_JWKS_MAX_STALE_SECONDS', '100')
    monkeypatch.setattr(auth_shim, '_jwks', {'keys': [{'kid': 'old'}]})
    monkeypatch.setattr(auth_shim, '_jwks_fetched_at', 950.0)
    monkeypatch.setattr(auth_shim, '_jwks_source_url', url)
    monkeypatch.setattr(auth_shim.httpx, 'get', lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('down')))
    monkeypatch.setattr(auth_shim.time, 'time', lambda: 1000.0)

    assert auth_shim._fetch_jwks() == {'keys': [{'kid': 'old'}]}

    monkeypatch.setattr(auth_shim.time, 'time', lambda: 1101.0)
    with pytest.raises(auth_shim.CertificateFetchError, match='JWKS fetch failed'):
        auth_shim._fetch_jwks()
