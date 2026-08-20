"""Provider-neutral identity verification and account lifecycle boundary.

Business routers must not call Firebase or Better Auth directly.  They use this
module so changing ``AUTH_PROVIDER`` changes every authentication entry point,
not only the common HTTP dependency.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, cast
from urllib.parse import quote, urlsplit

import httpx


class IdentityError(Exception):
    """Base class for errors crossing the identity provider boundary."""


class IdentityInvalidToken(IdentityError):
    """The presented credential is invalid, expired, or revoked."""


class IdentityProviderUnavailable(IdentityError):
    """The selected identity provider could not authoritatively answer."""


class IdentityUserNotFound(IdentityError):
    """The requested identity does not exist."""


@dataclass(frozen=True)
class IdentityUser:
    """Provider-neutral subset used by the backend's profile surfaces."""

    uid: str
    email: str | None = None
    email_verified: bool = False
    phone_number: str | None = None
    display_name: str | None = None
    photo_url: str | None = None
    disabled: bool = False
    provider_data: tuple[Any, ...] = ()


_PRIVATE_SERVICE_HOST = re.compile(r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$')
_PRIVATE_SERVICE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', 'fc00::/7', '::1/128')
)


def _is_explicit_private_http_origin(value: str) -> bool:
    """Return whether a URL is an unambiguous container/private HTTP origin."""

    parsed = urlsplit(value)
    if parsed.scheme != 'http' or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        return False
    host = (parsed.hostname or '').rstrip('.').lower()
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if '.' not in host:
            return _PRIVATE_SERVICE_HOST.fullmatch(host) is not None
        return host.endswith(('.internal', '.svc', '.svc.cluster.local'))
    return any(address in network for network in _PRIVATE_SERVICE_NETWORKS if address.version == network.version)


def identity_provider() -> str:
    provider = os.getenv('AUTH_PROVIDER', 'firebase').strip().lower().replace('-', '_')
    if provider not in {'firebase', 'better_auth'}:
        raise IdentityProviderUnavailable(f'unsupported AUTH_PROVIDER={provider!r}')
    return provider


def validate_identity_configuration() -> None:
    """Fail startup before serving traffic with an unsafe production profile."""

    if identity_provider() != 'better_auth':
        return
    stage = (os.getenv('OMI_ENV_STAGE') or os.getenv('ENVIRONMENT') or os.getenv('APP_ENV') or '').strip().lower()
    if stage not in {'prod', 'production'}:
        return
    required = (
        'AUTH_JWKS_URL',
        'AUTH_JWT_ISSUER',
        'AUTH_JWT_AUDIENCE',
        'AUTH_SERVER_INTERNAL_URL',
        'AUTH_INTERNAL_ADMIN_SECRET',
    )
    missing = [name for name in required if not os.getenv(name, '').strip()]
    if missing:
        raise RuntimeError(f'production Better Auth configuration missing: {", ".join(missing)}')
    for name in ('AUTH_JWT_ISSUER', 'AUTH_JWT_AUDIENCE'):
        parsed = urlsplit(os.environ[name])
        if parsed.scheme != 'https' or not parsed.netloc:
            raise RuntimeError(f'{name} must use https in production')
    allow_private_http = os.getenv('AUTH_INTERNAL_ALLOW_HTTP', '').strip().lower() == 'true'
    for name in ('AUTH_JWKS_URL', 'AUTH_SERVER_INTERNAL_URL'):
        value = os.environ[name]
        parsed = urlsplit(value)
        if parsed.scheme == 'https' and parsed.netloc:
            continue
        if allow_private_http and _is_explicit_private_http_origin(value):
            continue
        raise RuntimeError(
            f'{name} must use https in production or explicit private HTTP with AUTH_INTERNAL_ALLOW_HTTP=true'
        )


def _exception_types(module: Any, *names: str) -> tuple[type[BaseException], ...]:
    return tuple(
        error_type
        for name in names
        if isinstance((error_type := getattr(module, name, None)), type) and issubclass(error_type, BaseException)
    )


def _firebase_auth_module() -> Any:
    # Call-time import keeps provider selection mutable and lets deployments
    # that select Better Auth avoid initializing the Firebase Auth client.
    from firebase_admin import auth as firebase_auth

    return firebase_auth


def verify_id_token(token: str, *, check_revoked: bool = False) -> dict[str, Any]:
    """Verify an ID token and return claims containing a normalized ``uid``."""

    if not token:
        raise IdentityInvalidToken('empty token')

    if identity_provider() == 'better_auth':
        from utils import auth_shim

        try:
            claims = auth_shim.verify_id_token(token)
        except auth_shim.InvalidIdTokenError as exc:
            raise IdentityInvalidToken(str(exc)) from exc
        except auth_shim.CertificateFetchError as exc:
            raise IdentityProviderUnavailable(str(exc)) from exc
        except Exception as exc:
            raise IdentityProviderUnavailable(f'Better Auth verification failed: {type(exc).__name__}') from exc
    else:
        firebase_auth = _firebase_auth_module()
        try:
            raw_claims = firebase_auth.verify_id_token(token, check_revoked=check_revoked)  # type: ignore[reportUnknownMemberType]
            claims = cast(dict[str, Any], raw_claims)
        except Exception as exc:
            invalid_types = _exception_types(
                firebase_auth,
                'InvalidIdTokenError',
                'ExpiredIdTokenError',
                'RevokedIdTokenError',
            )
            unavailable_types = _exception_types(firebase_auth, 'CertificateFetchError')
            if invalid_types and isinstance(exc, invalid_types):
                raise IdentityInvalidToken(str(exc)) from exc
            if unavailable_types and isinstance(exc, unavailable_types):
                raise IdentityProviderUnavailable(str(exc)) from exc
            raise IdentityProviderUnavailable(f'Firebase verification failed: {type(exc).__name__}') from exc

    uid = claims.get('uid') or claims.get('sub')
    if not isinstance(uid, str) or not uid:
        raise IdentityInvalidToken('identity token missing uid/sub claim')
    return {**claims, 'uid': uid, 'sub': str(claims.get('sub') or uid)}


def _auth_server_origin() -> str:
    explicit = os.getenv('AUTH_SERVER_INTERNAL_URL', '').strip()
    if explicit:
        return explicit.rstrip('/')
    jwks_url = os.getenv('AUTH_JWKS_URL', '').strip()
    parsed = urlsplit(jwks_url)
    if parsed.scheme in {'http', 'https'} and parsed.netloc:
        return f'{parsed.scheme}://{parsed.netloc}'
    raise IdentityProviderUnavailable(
        'AUTH_SERVER_INTERNAL_URL or AUTH_JWKS_URL is required for Better Auth lifecycle calls'
    )


def _internal_headers() -> dict[str, str]:
    secret = os.getenv('AUTH_INTERNAL_ADMIN_SECRET', '').strip()
    if not secret:
        raise IdentityProviderUnavailable('AUTH_INTERNAL_ADMIN_SECRET is required for Better Auth lifecycle calls')
    return {'Authorization': f'Bearer {secret}'}


def _internal_timeout() -> float:
    return float(os.getenv('AUTH_INTERNAL_TIMEOUT_SECONDS', '5'))


def authenticate_email_to_jwt(email: str, password: str) -> str:
    """Exchange Better Auth email credentials for a one-use browser JWT.

    OAuth consent pages call this through the same-origin backend so they do
    not need Firebase SDKs, cross-origin cookies, or access to the signed bearer
    session token.  The temporary Better Auth session is revoked immediately
    after the JWT is minted.
    """

    if identity_provider() != 'better_auth':
        raise IdentityProviderUnavailable('email identity exchange is only available with Better Auth')
    origin = _auth_server_origin()
    timeout = _internal_timeout()
    try:
        sign_in = httpx.request(
            'POST',
            f'{origin}/api/auth/sign-in/email',
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            json={'email': email.strip(), 'password': password, 'rememberMe': False},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise IdentityProviderUnavailable(f'Better Auth sign-in failed: {type(exc).__name__}') from exc
    if sign_in.status_code in {400, 401, 403, 422}:
        raise IdentityInvalidToken('invalid email or password')
    if sign_in.status_code >= 400:
        raise IdentityProviderUnavailable(f'Better Auth sign-in returned HTTP {sign_in.status_code}')
    session_token = sign_in.headers.get('set-auth-token', '').strip()
    if not session_token:
        raise IdentityProviderUnavailable('Better Auth sign-in did not return a signed bearer session')

    authorization = {'Authorization': f'Bearer {session_token}', 'Accept': 'application/json'}
    try:
        token_response = httpx.request(
            'GET',
            f'{origin}/api/auth/token',
            headers=authorization,
            timeout=timeout,
        )
        if token_response.status_code in {401, 403}:
            raise IdentityInvalidToken('Better Auth session expired before JWT exchange')
        if token_response.status_code >= 400:
            raise IdentityProviderUnavailable(f'Better Auth token exchange returned HTTP {token_response.status_code}')
        payload = token_response.json()
        token = payload.get('token') if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise IdentityProviderUnavailable('Better Auth token exchange response missing token')
        return token
    except httpx.HTTPError as exc:
        raise IdentityProviderUnavailable(f'Better Auth token exchange failed: {type(exc).__name__}') from exc
    finally:
        try:
            httpx.request(
                'POST',
                f'{origin}/api/auth/sign-out',
                headers=authorization,
                timeout=timeout,
            )
        except httpx.HTTPError:
            pass


def _internal_request(method: str, path: str) -> Mapping[str, Any] | None:
    try:
        response = httpx.request(
            method,
            f'{_auth_server_origin()}{path}',
            headers=_internal_headers(),
            timeout=_internal_timeout(),
        )
    except httpx.HTTPError as exc:
        raise IdentityProviderUnavailable(f'Better Auth lifecycle request failed: {type(exc).__name__}') from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise IdentityProviderUnavailable(f'Better Auth lifecycle request returned HTTP {response.status_code}')
    if not response.content:
        return {}
    payload = response.json()
    if not isinstance(payload, dict):
        raise IdentityProviderUnavailable('Better Auth lifecycle response was not an object')
    return cast(Mapping[str, Any], payload)


def get_user(uid: str) -> Any:
    """Return a provider user record with the legacy attribute surface."""

    if identity_provider() == 'firebase':
        firebase_auth = _firebase_auth_module()
        return firebase_auth.get_user(uid)  # type: ignore[reportUnknownMemberType]
    payload = _internal_request('GET', f'/internal/users/{quote(uid, safe="")}')
    if payload is None:
        raise IdentityUserNotFound(uid)
    user = payload.get('user')
    if not isinstance(user, dict):
        raise IdentityProviderUnavailable('Better Auth user response missing user object')
    return IdentityUser(
        uid=str(user.get('id') or uid),
        email=cast(str | None, user.get('email')),
        email_verified=bool(user.get('emailVerified', False)),
        display_name=cast(str | None, user.get('name')),
        photo_url=cast(str | None, user.get('image')),
        disabled=bool(user.get('banned', False)),
    )


def delete_user(uid: str) -> None:
    """Delete the identity and all provider-owned sessions/accounts."""

    if identity_provider() == 'firebase':
        firebase_auth = _firebase_auth_module()
        firebase_auth.delete_user(uid)  # type: ignore[reportUnknownMemberType]
        return
    # Deletion is intentionally idempotent for crash-retried account wipes.
    _internal_request('DELETE', f'/internal/users/{quote(uid, safe="")}')


def account_residual_counts(uid: str) -> dict[str, int]:
    """Return provider-owned identity rows remaining for an account."""

    if identity_provider() == 'firebase':
        firebase_auth = _firebase_auth_module()
        try:
            firebase_auth.get_user(uid)  # type: ignore[reportUnknownMemberType]
        except Exception as exc:
            not_found = _exception_types(firebase_auth, 'UserNotFoundError')
            if not_found and isinstance(exc, not_found):
                return {'users': 0, 'sessions': 0, 'accounts': 0}
            raise IdentityProviderUnavailable(f'Firebase deletion reconciliation failed: {type(exc).__name__}') from exc
        return {'users': 1, 'sessions': 0, 'accounts': 0}

    payload = _internal_request('GET', f'/internal/users/{quote(uid, safe="")}/residuals')
    if payload is None:
        raise IdentityProviderUnavailable('Better Auth residual-count endpoint returned not found')
    counts: dict[str, int] = {}
    for name in ('users', 'sessions', 'accounts'):
        value = payload.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise IdentityProviderUnavailable(f'Better Auth residual-count response has invalid {name}')
        counts[name] = value
    return counts
