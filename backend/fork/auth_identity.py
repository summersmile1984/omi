"""Read profiles, delete and prove absence through the selected identity authority.

The existing deletion worker retains its lifecycle. This adapter replaces only
its Firebase identity call, and completion rechecks all authoritative residuals.
An unavailable service or an unknown deletion outcome remains retryable.
"""

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import httpx

from firestore_pg.erasure import validate_uid
from utils import auth_shim


class IdentityAuthorityUnavailable(RuntimeError):
    """The identity authority could not prove the required result."""


@dataclass(frozen=True)
class IdentityUser:
    """Validated fields consumed by the existing database.auth profile owner."""

    uid: str
    email: str
    email_verified: bool
    phone_number: str | None
    display_name: str
    photo_url: str | None
    disabled: bool
    created_at_ms: int | None = None


def _created_at_ms(value):
    # Imported users without creation metadata retain their profile, but cannot
    # be admitted as newly registered accounts by entitlement consumers.
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError
        created = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if created.tzinfo is None:
            raise ValueError
        return int(created.timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        raise IdentityAuthorityUnavailable('Identity creation time could not be verified') from None


def get_user(uid):
    """Resolve the optional profile without invoking another identity provider.

    The upstream consumer retains its profile/default-name fallback. Failures
    reaching that path are observable and contain no authority response or PII.
    """
    reason = 'authorization_unavailable'
    try:
        status, data = _request('GET', uid, _authority())
        if status == 404 and data == {'error': 'user_not_found'}:
            return None
        if status != 200:
            raise IdentityAuthorityUnavailable('Identity profile authority is unavailable')
        reason = 'malformed_doc'
        user = data.get('user') if isinstance(data, dict) else None
        if (
            not isinstance(user, dict)
            or user.get('id') != uid
            or not isinstance(user.get('email'), str)
            or not isinstance(user.get('name'), str)
            or type(user.get('emailVerified')) is not bool
            or type(user.get('banned', False)) is not bool
            or any(
                user.get(field) is not None and not isinstance(user[field], str) for field in ('image', 'phoneNumber')
            )
        ):
            raise IdentityAuthorityUnavailable('Identity profile result could not be verified')
        return IdentityUser(
            uid=uid,
            email=user['email'],
            email_verified=user['emailVerified'],
            phone_number=user.get('phoneNumber'),
            display_name=user['name'],
            photo_url=user.get('image'),
            disabled=user.get('banned', False),
            created_at_ms=_created_at_ms(user.get('createdAt')),
        )
    except IdentityAuthorityUnavailable:
        from utils.observability.fallback import record_fallback

        record_fallback(
            component='other',
            from_mode='better_auth_profile',
            to_mode='optional_profile_unavailable',
            reason=reason,
            outcome='degraded',
        )
        raise


def _authority():
    try:
        return auth_shim.internal_authority()
    except auth_shim.CertificateFetchError:
        raise IdentityAuthorityUnavailable('Identity authority configuration is unavailable') from None


def _request(method, uid, authority, suffix=''):
    base_url, secret = authority
    try:
        segment = quote(validate_uid(uid), safe='')
        # quote() always preserves dots; HTTPX otherwise resolves these valid
        # imported identity values as parent/current path components.
        if segment in {'.', '..'}:
            segment = segment.replace('.', '%2E')
        response = httpx.request(
            method,
            f'{base_url}/internal/users/{segment}{suffix}',
            headers={'authorization': f'Bearer {secret}'},
            timeout=5,
            follow_redirects=False,
        )
        data = response.json()
    except (httpx.HTTPError, ValueError):
        raise IdentityAuthorityUnavailable('Identity authority request could not be verified') from None
    return response.status_code, data


def assert_erased(uid, *, authority=None):
    status, residuals = _request('GET', uid, authority or _authority(), '/residuals')
    if (
        status != 200
        or not isinstance(residuals, dict)
        or set(residuals) != {'users', 'sessions', 'accounts'}
        or any(type(value) is not int or value != 0 for value in residuals.values())
    ):
        raise IdentityAuthorityUnavailable('Identity deletion is incomplete or could not be verified')


def delete_account(uid):
    authority = _authority()
    status, result = _request('DELETE', uid, authority)
    if not (
        (status == 200 and isinstance(result, dict) and set(result) == {'success'} and result['success'] is True)
        or (status == 404 and result == {'error': 'user_not_found'})
    ):
        raise IdentityAuthorityUnavailable('Identity deletion did not return an authoritative result')
    # Absence of a user alone does not prove linked accounts/sessions are gone.
    # Reusing the same origin/credential also keeps one invocation on one owner.
    assert_erased(uid, authority=authority)
    return {'message': 'User deleted'}
