"""Real signature verification against the shared two-target claims fixtures."""

import json
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from utils import auth_shim

FIXTURE = json.loads((Path(__file__).resolve().parents[3] / "contracts/auth/claims.json").read_text())


@pytest.fixture
def credential(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    key = json.loads(ECAlgorithm.to_jwk(private.public_key()))
    key.update(kid="current-key", alg="ES256", use="sig")
    monkeypatch.setattr(auth_shim, "_jwks", None)
    monkeypatch.setattr(auth_shim, "_jwks_fetched_at", 0)
    monkeypatch.setattr(
        auth_shim.httpx,
        "get",
        lambda url, **kw: httpx.Response(200, json={"keys": [key]}, request=httpx.Request("GET", url)),
    )
    monkeypatch.setenv("AUTH_JWT_ISSUER", FIXTURE["issuer"])
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", FIXTURE["audience"])
    monkeypatch.setenv("AUTH_SERVER_INTERNAL_URL", "https://auth.internal.invalid")
    monkeypatch.setenv("AUTH_INTERNAL_ADMIN_SECRET", "synthetic-internal-secret")
    monkeypatch.setattr(
        auth_shim.httpx,
        "post",
        lambda url, **kw: httpx.Response(
            200,
            json={"uid": "existing-user", "sessionGeneration": "current-session"},
            request=httpx.Request("POST", url),
        ),
    )
    now = int(time.time())
    claims = dict(
        sub="existing-user",
        uid="existing-user",
        sid="current-session",
        iss=FIXTURE["issuer"],
        aud=FIXTURE["audience"],
        iat=now - 10,
        exp=now + 3590,
    )

    def issue(changes=None, drop=None, kid="current-key"):
        payload = {**claims, **(changes or {})}
        if drop:
            payload.pop(drop, None)
        return jwt.encode(payload, private, algorithm="ES256", headers={"kid": kid})

    return issue, now


@pytest.mark.parametrize("example", FIXTURE["cases"], ids=lambda row: row["name"])
def test_shared_claims(credential, example):
    issue, now = credential
    changes = dict(example.get("set", {}))
    for claim in ("iat", "exp"):
        if f"{claim}Offset" in example:
            changes[claim] = now + example[f"{claim}Offset"]
    token = issue(changes, example.get("drop"))
    if example.get("valid"):
        assert auth_shim.verify_id_token(token)["uid"] == "existing-user"
    else:
        with pytest.raises(auth_shim.InvalidIdTokenError):
            auth_shim.verify_id_token(token)


def test_unknown_key_refuses_and_refreshes_once(credential, monkeypatch):
    issue, _ = credential
    with pytest.raises(auth_shim.InvalidIdTokenError):
        auth_shim.verify_id_token(issue(kid="unpublished-key"))


def test_revoked_session_and_deleted_account_are_rejected(credential, monkeypatch):
    issue, _ = credential
    monkeypatch.setattr(auth_shim.httpx, "post", lambda url, **kw: httpx.Response(401))
    with pytest.raises(auth_shim.InvalidIdTokenError, match="no longer active"):
        auth_shim.verify_id_token(issue())


def test_unavailable_authority_is_not_a_cached_success(credential, monkeypatch):
    issue, _ = credential
    token = issue()
    assert auth_shim.verify_id_token(token)["uid"] == "existing-user"

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("synthetic failure")

    monkeypatch.setattr(auth_shim.httpx, "post", unavailable)
    with pytest.raises(auth_shim.CertificateFetchError):
        auth_shim.verify_id_token(token)
    monkeypatch.setattr(auth_shim, "_jwks_fetched_at", 0)
    monkeypatch.setattr(auth_shim.httpx, "get", unavailable)
    with pytest.raises(auth_shim.CertificateFetchError):
        auth_shim.verify_id_token(token)


def test_missing_issuer_configuration_fails_closed(credential, monkeypatch):
    issue, _ = credential
    monkeypatch.delenv("AUTH_JWT_ISSUER")
    with pytest.raises(auth_shim.CertificateFetchError):
        auth_shim.verify_id_token(issue())
