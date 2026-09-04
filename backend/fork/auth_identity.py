"""Delete and prove absence through the configured Better Auth authority.

The existing deletion worker retains its lifecycle. This adapter replaces only
its Firebase identity call, and completion rechecks all authoritative residuals.
An unavailable service or an unknown deletion outcome remains retryable.
"""

from urllib.parse import quote

import httpx

from firestore_pg.erasure import validate_uid
from utils import auth_shim


class IdentityAuthorityUnavailable(RuntimeError):
    """The identity authority could not prove the required deletion result."""


def _authority():
    try:
        return auth_shim.internal_authority()
    except auth_shim.CertificateFetchError:
        raise IdentityAuthorityUnavailable('Identity authority configuration is unavailable') from None


def _request(method, uid, authority, suffix=''):
    base_url, secret = authority
    try:
        response = httpx.request(
            method,
            f'{base_url}/internal/users/{quote(validate_uid(uid), safe="")}{suffix}',
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
