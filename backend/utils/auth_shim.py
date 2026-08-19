"""Better Auth JWT verification shim — drop-in for firebase_admin.auth.verify_id_token.

Validates JWTs issued by a Better Auth service (email+password + jwt plugin,
ES256 JWKS at ``AUTH_JWKS_URL``). Returns a claims dict shaped like
Firebase's ``verify_id_token`` (``{'uid': ..., 'sub': ...}``) so the single
core auth entry point (utils/other/endpoints.py verify_token) works unchanged.

The shim is deliberately isolated here; the only switch is
``AUTH_PROVIDER=better_auth`` (see endpoints.verify_token). When unset, the
original firebase_admin path is untouched.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import httpx
import jwt as pyjwt
from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm

logger = logging.getLogger(__name__)

_DEFAULT_JWKS_URL = "http://127.0.0.1:3000/api/auth/jwks"
_SUPPORTED_ALGORITHMS_BY_KEY_TYPE = {
    "EC": {"ES256"},
    "RSA": {"RS256"},
    "OKP": {"EdDSA"},
}


class InvalidIdTokenError(Exception):
    """Mirror of firebase_admin.auth.InvalidIdTokenError for the shim path."""


class CertificateFetchError(Exception):
    """Mirror of firebase_admin.auth.CertificateFetchError (JWKS fetch failed)."""


# ---------------------------------------------------------------------------
# JWKS cache (thread-safe, TTL-refreshed)
# ---------------------------------------------------------------------------

_jwks: Optional[Dict[str, Any]] = None
_jwks_fetched_at: float = 0.0
_jwks_source_url: Optional[str] = None
_jwks_lock = threading.Lock()


def _jwks_url() -> str:
    return os.getenv("AUTH_JWKS_URL", _DEFAULT_JWKS_URL).strip() or _DEFAULT_JWKS_URL


def _jwks_cache_ttl() -> int:
    return int(os.getenv("AUTH_JWKS_CACHE_TTL_SECONDS", "300"))


def _jwks_timeout() -> float:
    return float(os.getenv("AUTH_JWKS_TIMEOUT_SECONDS", "5"))


def _fetch_jwks() -> Dict[str, Any]:
    """Fetch and cache the Better Auth JWKS (public keys for JWT verification)."""
    global _jwks, _jwks_fetched_at, _jwks_source_url
    now = time.time()
    url = _jwks_url()
    with _jwks_lock:
        if _jwks is not None and _jwks_source_url == url and (now - _jwks_fetched_at) < _jwks_cache_ttl():
            return _jwks
        try:
            resp = httpx.get(url, timeout=_jwks_timeout())
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            # Stale cache is better than failing closed on a transient fetch error
            if _jwks is not None and _jwks_source_url == url:
                logger.warning("JWKS refresh failed, using stale cache: %s", exc)
                return _jwks
            raise CertificateFetchError(f"JWKS fetch failed: {exc}") from exc
        if "keys" not in data:
            raise CertificateFetchError(f"JWKS response missing keys: {data}")
        _jwks = data
        _jwks_fetched_at = now
        _jwks_source_url = url
        logger.info("auth_shim: JWKS cached (%d keys)", len(data["keys"]))
        return data


def verify_id_token(token: str, **_: Any) -> Dict[str, Any]:
    """Verify a Better Auth JWT; return claims shaped like Firebase.

    Returns ``{'uid': <sub-or-uid>, 'sub': ...}`` matching the shape
    ``verify_token`` consumes (``decoded_token['uid']``).
    """
    if not token:
        raise InvalidIdTokenError("empty token")
    try:
        header = pyjwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm not in {"ES256", "RS256", "EdDSA"}:
            raise InvalidIdTokenError(f"unsupported JWT algorithm={algorithm}")
        jwks = _fetch_jwks()
        kid = header.get("kid")
        key = next((k for k in jwks["keys"] if k.get("kid") == kid), None)
        if key is None:
            # kid not in cache — force refresh once (rotation)
            global _jwks, _jwks_fetched_at, _jwks_source_url
            with _jwks_lock:
                _jwks = None
                _jwks_fetched_at = 0.0
                _jwks_source_url = None
            jwks = _fetch_jwks()
            key = next((k for k in jwks["keys"] if k.get("kid") == kid), None)
        if key is None:
            raise InvalidIdTokenError(f"JWKS has no key for kid={kid}")
        key_type = key.get("kty")
        if algorithm not in _SUPPORTED_ALGORITHMS_BY_KEY_TYPE.get(key_type, set()):
            raise InvalidIdTokenError(f"JWT algorithm={algorithm} does not match JWK kty={key_type}")
        if key.get("alg") not in (None, algorithm):
            raise InvalidIdTokenError(f"JWT algorithm={algorithm} does not match JWK alg={key.get('alg')}")
        # JWK dict -> cryptography key (ES256/RS256/EdDSA all supported)
        verify_key = _jwk_to_key(key)
        claims = pyjwt.decode(
            token,
            verify_key,
            algorithms=[algorithm],
            options={"verify_aud": False},  # Better Auth JWT carries no aud
        )
    except pyjwt.PyJWTError as exc:
        raise InvalidIdTokenError(str(exc)) from exc
    except CertificateFetchError:
        raise
    uid = claims.get("uid") or claims.get("sub")
    if not uid:
        raise InvalidIdTokenError("Better Auth JWT missing uid/sub claim")
    return {**claims, "uid": str(uid), "sub": str(claims.get("sub") or uid)}


def _jwk_to_key(jwk: Dict[str, Any]) -> Any:
    """Convert a JWKS key dict into a pyjwt-verifiable key.

    Supports only asymmetric EC (ES256), RSA (RS256), and OKP (EdDSA) keys.
    """
    kty = jwk.get("kty")
    if kty == "EC":
        return ECAlgorithm.from_jwk(jwk)
    if kty == "RSA":
        return RSAAlgorithm.from_jwk(jwk)
    if kty == "OKP":  # Ed25519 / Ed448 (Better Auth default signing)
        return OKPAlgorithm.from_jwk(jwk)
    raise InvalidIdTokenError(f"unsupported JWK kty={kty}")


def verify_id_token_for_firebase_compat(token: str) -> Dict[str, Any]:
    """Alias matching firebase_admin's signature surface (check_revoked, clock_skew)."""
    return verify_id_token(token)
