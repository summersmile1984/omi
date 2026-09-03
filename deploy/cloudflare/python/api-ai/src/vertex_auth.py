"""Google service-account OAuth 2.0 JWT bearer flow for Vertex AI.

The API-AI Worker cannot use a VM metadata server or ADC file.  This module
therefore accepts only an operator-provided service-account JSON secret and
signs a short-lived JWT with the Workers Web Crypto FFI.  Raw private keys and
access tokens never enter D1, logs, or an assertion header.
"""

from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from time import time
from typing import Any
from urllib.parse import urlencode

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
VERTEX_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
MAX_SERVICE_ACCOUNT_BYTES = 32_000
MAX_PRIVATE_KEY_BYTES = 8_192
MAX_TOKEN_RESPONSE_BYTES = 64_000
MAX_ACCESS_TOKEN_BYTES = 8_192
TOKEN_TTL_SECONDS = 3_600
TOKEN_CACHE_SAFETY_SECONDS = 60
TOKEN_EXCHANGE_TIMEOUT_SECONDS = 5
PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
SERVICE_ACCOUNT_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}@[A-Za-z0-9-]{1,63}\.iam\.gserviceaccount\.com$"
)
PRIVATE_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class VertexAuthError(Exception):
    """A deliberately non-sensitive Vertex credential/exchange failure."""

    def __init__(self, message: str, *, code: str = "vertex_auth_unavailable"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VertexServiceAccount:
    project_id: str
    client_email: str
    private_key: str
    private_key_id: str | None = None


@dataclass(frozen=True)
class _CachedAccessToken:
    value: str
    expires_at: int
    cache_key: str


_cached_access_token: _CachedAccessToken | None = None


def _utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _base64url(value: bytes | bytearray | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _private_key_bytes(value: str) -> bytes:
    normalized = value.replace("\\n", "\n")
    begin = "-----BEGIN PRIVATE KEY-----"
    end = "-----END PRIVATE KEY-----"
    if not normalized.startswith(begin) or not normalized.endswith(end):
        raise VertexAuthError("service account key is unavailable")
    encoded = normalized[len(begin) : -len(end)]
    encoded = "".join(encoded.split())
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise VertexAuthError("service account key is unavailable") from error
    if not 256 <= len(decoded) <= MAX_PRIVATE_KEY_BYTES:
        raise VertexAuthError("service account key is unavailable")
    return decoded


def parse_service_account(
    value: str | None,
    *,
    expected_project_id: str | None = None,
) -> VertexServiceAccount | None:
    """Parse and minimally validate an operator-provided service account.

    Returning ``None`` for every malformed shape keeps the route's error
    surface independent of the secret contents.  Cryptographic validity is
    checked by Web Crypto immediately before the token exchange.
    """

    if not isinstance(value, str) or not value or _utf8_bytes(value) > MAX_SERVICE_ACCOUNT_BYTES:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    project_id = parsed.get("project_id")
    client_email = parsed.get("client_email")
    private_key = parsed.get("private_key")
    private_key_id = parsed.get("private_key_id")
    if (
        not isinstance(project_id, str)
        or not PROJECT_ID_PATTERN.fullmatch(project_id)
        or not isinstance(client_email, str)
        or not SERVICE_ACCOUNT_EMAIL_PATTERN.fullmatch(client_email)
        or not isinstance(private_key, str)
        or _utf8_bytes(private_key) > MAX_PRIVATE_KEY_BYTES
        or (expected_project_id is not None and project_id != expected_project_id)
    ):
        return None
    if private_key_id is not None and (
        not isinstance(private_key_id, str) or not PRIVATE_KEY_ID_PATTERN.fullmatch(private_key_id)
    ):
        return None
    try:
        _private_key_bytes(private_key)
    except VertexAuthError:
        return None
    return VertexServiceAccount(
        project_id=project_id,
        client_email=client_email,
        private_key=private_key,
        private_key_id=private_key_id if isinstance(private_key_id, str) else None,
    )


def _buffer_source(value: bytes) -> object:
    """Convert bytes to a JS TypedArray when running inside Pyodide.

    CPython tests do not expose ``js``; retaining the bytes fallback keeps the
    signing function injectable without importing a Workers-only module.
    """

    try:
        from js import Uint8Array  # type: ignore[import-not-found]

        target = Uint8Array.new(len(value))
        target.set(list(value))
        return target
    except (ImportError, ModuleNotFoundError, AttributeError, TypeError):
        return value


def _to_bytes(value: object) -> bytes:
    converted = getattr(value, "to_py", None)
    if callable(converted):
        value = converted()
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    try:
        return bytes(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise VertexAuthError("service account signing is unavailable") from None


async def _sign_rs256(unsigned: bytes, private_key: bytes) -> bytes:
    """Sign with Workers' native Web Crypto RSASSA-PKCS1-v1_5 implementation."""

    try:
        from js import crypto  # type: ignore[import-not-found]
    except (ImportError, ModuleNotFoundError) as error:
        raise VertexAuthError("service account signing is unavailable") from error
    try:
        key = await crypto.subtle.importKey(
            "pkcs8",
            _buffer_source(private_key),
            {"name": "RSASSA-PKCS1-v1_5", "hash": "SHA-256"},
            False,
            ["sign"],
        )
        signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, _buffer_source(unsigned))
        return _to_bytes(signature)
    except Exception as error:
        raise VertexAuthError("service account signing is unavailable") from error


def _cache_key(account: VertexServiceAccount) -> str:
    # The digest allows key rotation under the same service-account email
    # without retaining the private key in process-global state.
    digest = hashlib.sha256(_private_key_bytes(account.private_key)).hexdigest()
    return f"{account.client_email}:{account.project_id}:{digest}"


async def _response_bytes(response: object) -> bytes:
    method = getattr(response, "arrayBuffer", None)
    if not callable(method):
        raise VertexAuthError("Vertex token exchange returned an invalid response")
    value = _to_bytes(await method())
    if len(value) > MAX_TOKEN_RESPONSE_BYTES:
        raise VertexAuthError("Vertex token exchange returned an invalid response")
    return value


def clear_access_token_cache() -> None:
    """Clear the isolate-local token cache (used by rotation/tests)."""

    global _cached_access_token
    _cached_access_token = None


async def access_token(
    service_account_json: str | None,
    worker_fetch: Any,
    *,
    expected_project_id: str | None = None,
    now: int | None = None,
) -> str:
    """Exchange a service-account JWT for a bounded, cached Vertex token."""

    account = parse_service_account(service_account_json, expected_project_id=expected_project_id)
    if account is None:
        raise VertexAuthError("Vertex service-account credentials are unavailable")
    current = int(time()) if now is None else now
    if current < 0:
        raise VertexAuthError("Vertex clock is unavailable")
    key = _cache_key(account)
    global _cached_access_token
    if (
        _cached_access_token is not None
        and _cached_access_token.cache_key == key
        and _cached_access_token.expires_at > current + TOKEN_CACHE_SAFETY_SECONDS
    ):
        return _cached_access_token.value
    if not callable(worker_fetch):
        raise VertexAuthError("Vertex token exchange is unavailable")
    header = {"alg": "RS256", "typ": "JWT"}
    if account.private_key_id:
        header["kid"] = account.private_key_id
    claims = {
        "iss": account.client_email,
        "scope": VERTEX_SCOPE,
        "aud": GOOGLE_TOKEN_URL,
        "iat": current,
        "exp": current + TOKEN_TTL_SECONDS,
    }
    encoded_header = _base64url(json.dumps(header, separators=(",", ":")))
    encoded_claims = _base64url(json.dumps(claims, separators=(",", ":")))
    unsigned = f"{encoded_header}.{encoded_claims}".encode()
    signature = await _sign_rs256(unsigned, _private_key_bytes(account.private_key))
    assertion = f"{unsigned.decode()}.{_base64url(signature)}"
    try:
        async with asyncio.timeout(TOKEN_EXCHANGE_TIMEOUT_SECONDS):
            response = await worker_fetch(
                GOOGLE_TOKEN_URL,
                method="POST",
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                },
                body=urlencode(
                    {
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    }
                ),
            )
        status = int(getattr(response, "status", 0))
        body = await _response_bytes(response)
    except TimeoutError as error:
        raise VertexAuthError("Vertex token exchange timed out", code="vertex_auth_timeout") from error
    except VertexAuthError:
        raise
    except Exception as error:
        raise VertexAuthError("Vertex token exchange is unavailable", code="vertex_auth_unavailable") from error
    if status < 200 or status >= 300:
        code = "vertex_auth_rate_limited" if status == 429 else "vertex_auth_rejected"
        raise VertexAuthError("Vertex token exchange was rejected", code=code)
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as error:
        raise VertexAuthError(
            "Vertex token exchange returned an invalid response",
            code="vertex_auth_invalid_response",
        ) from error
    if not isinstance(payload, dict):
        raise VertexAuthError(
            "Vertex token exchange returned an invalid response",
            code="vertex_auth_invalid_response",
        )
    token = payload.get("access_token")
    expires_in = payload.get("expires_in", TOKEN_TTL_SECONDS)
    if (
        not isinstance(token, str)
        or not token
        or _utf8_bytes(token) > MAX_ACCESS_TOKEN_BYTES
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or not 60 <= expires_in <= TOKEN_TTL_SECONDS
    ):
        raise VertexAuthError(
            "Vertex token exchange returned an invalid response",
            code="vertex_auth_invalid_response",
        )
    _cached_access_token = _CachedAccessToken(
        value=token,
        expires_at=current + expires_in,
        cache_key=key,
    )
    return token


__all__ = [
    "GOOGLE_TOKEN_URL",
    "VERTEX_SCOPE",
    "VertexAuthError",
    "VertexServiceAccount",
    "access_token",
    "clear_access_token_cache",
    "parse_service_account",
]
