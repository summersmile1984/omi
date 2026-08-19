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

logger = logging.getLogger(__name__)

JWKS_URL = os.getenv("AUTH_JWKS_URL", "http://127.0.0.1:3000/jwks")
JWKS_CACHE_TTL = int(os.getenv("AUTH_JWKS_CACHE_TTL_SECONDS", "300"))
JWKS_TIMEOUT = float(os.getenv("AUTH_JWKS_TIMEOUT_SECONDS", "5"))


class InvalidIdTokenError(Exception):
    """Mirror of firebase_admin.auth.InvalidIdTokenError for the shim path."""


class CertificateFetchError(Exception):
    """Mirror of firebase_admin.auth.CertificateFetchError (JWKS fetch failed)."""


# ---------------------------------------------------------------------------
# JWKS cache (thread-safe, TTL-refreshed)
# ---------------------------------------------------------------------------

_jwks: Optional[Dict[str, Any]] = None
_jwks_fetched_at: float = 0.0
_jwks_lock = threading.Lock()


def _fetch_jwks() -> Dict[str, Any]:
    """Fetch and cache the Better Auth JWKS (public keys for JWT verification)."""
    global _jwks, _jwks_fetched_at
    now = time.time()
    with _jwks_lock:
        if _jwks is not None and (now - _jwks_fetched_at) < JWKS_CACHE_TTL:
            return _jwks
        try:
            resp = httpx.get(JWKS_URL, timeout=JWKS_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            # Stale cache is better than failing closed on a transient fetch error
            if _jwks is not None:
                logger.warning("JWKS refresh failed, using stale cache: %s", exc)
                return _jwks
            raise CertificateFetchError(f"JWKS fetch failed: {exc}") from exc
        if "keys" not in data:
            raise CertificateFetchError(f"JWKS response missing keys: {data}")
        _jwks = data
        _jwks_fetched_at = now
        logger.info("auth_shim: JWKS cached (%d keys)", len(data["keys"]))
        return _jwks


def verify_id_token(token: str, **_: Any) -> Dict[str, Any]:
    """Verify a Better Auth JWT; return claims shaped like Firebase.

    Returns ``{'uid': <sub-or-uid>, 'sub': ...}`` matching the shape
    ``verify_token`` consumes (``decoded_token['uid']``).
    """
    if not token:
        raise InvalidIdTokenError("empty token")
    try:
        header = pyjwt.get_unverified_header(token)
        jwks = _fetch_jwks()
        kid = header.get("kid")
        key = next((k for k in jwks["keys"] if k.get("kid") == kid), None)
        if key is None:
            # kid not in cache — force refresh once (rotation)
            global _jwks, _jwks_fetched_at
            with _jwks_lock:
                _jwks = None
                _jwks_fetched_at = 0.0
            jwks = _fetch_jwks()
            key = next((k for k in jwks["keys"] if k.get("kid") == kid), None)
        if key is None:
            raise InvalidIdTokenError(f"JWKS has no key for kid={kid}")
        # JWK dict -> cryptography key (ES256/RS256/EdDSA all supported)
        verify_key = _jwk_to_key(key)
        claims = pyjwt.decode(
            token,
            verify_key,
            algorithms=[header.get("alg", "ES256")],
            options={"verify_aud": False},  # Better Auth JWT carries no aud
        )
    except pyjwt.PyJWTError as exc:
        raise InvalidIdTokenError(str(exc)) from exc
    except CertificateFetchError:
        raise
    uid = claims.get("uid") or claims.get("sub")
    if not uid:
        raise InvalidIdTokenError("Better Auth JWT missing uid/sub claim")
    return {"uid": str(uid), "sub": str(uid), **claims}


def _jwk_to_key(jwk: Dict[str, Any]) -> Any:
    """Convert a JWKS key dict into a pyjwt-verifiable key.

    Supports EC (ES256), RSA (RS256), and oct (HS256) JWK entries.
    """
    kty = jwk.get("kty")
    if kty == "oct":
        import base64

        return base64.urlsafe_b64decode(jwk["k"] + "==")
    if kty == "EC":
        from jwt.algorithms import ECAlgorithm

        return ECAlgorithm.from_jwk(jwk)
    if kty == "RSA":
        from jwt.algorithms import RSAAlgorithm

        return RSAAlgorithm.from_jwk(jwk)
    if kty == "OKP":  # Ed25519 / Ed448 (Better Auth default signing)
        from jwt.algorithms import OKPAlgorithm

        return OKPAlgorithm.from_jwk(jwk)
    raise InvalidIdTokenError(f"unsupported JWK kty={kty}")


def verify_id_token_for_firebase_compat(token: str) -> Dict[str, Any]:
    """Alias matching firebase_admin's signature surface (check_revoked, clock_skew)."""
    return verify_id_token(token)
