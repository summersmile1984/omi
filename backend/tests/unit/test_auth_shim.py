import json

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


def test_verify_id_token_accepts_es256_and_preserves_uid(monkeypatch):
    token, public_jwk = _es256_credential({"uid": "user-123", "sub": "subject-123"})
    monkeypatch.setattr(auth_shim, "_fetch_jwks", lambda: {"keys": [public_jwk]})

    claims = auth_shim.verify_id_token(token)

    assert claims["uid"] == "user-123"
    assert claims["sub"] == "subject-123"


def test_verify_id_token_rejects_algorithm_key_mismatch(monkeypatch):
    token, public_jwk = _es256_credential({"uid": "user-123"})
    public_jwk["alg"] = "RS256"
    monkeypatch.setattr(auth_shim, "_fetch_jwks", lambda: {"keys": [public_jwk]})

    with pytest.raises(auth_shim.InvalidIdTokenError, match="does not match JWK alg"):
        auth_shim.verify_id_token(token)


def test_verify_id_token_rejects_missing_identity(monkeypatch):
    token, public_jwk = _es256_credential({"scope": "read"})
    monkeypatch.setattr(auth_shim, "_fetch_jwks", lambda: {"keys": [public_jwk]})

    with pytest.raises(auth_shim.InvalidIdTokenError, match="missing uid/sub"):
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
