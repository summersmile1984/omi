"""Preserve authoritative Better Auth failures through the upstream consumers."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from ..registry import Patch


def _self_hosted(row: dict) -> bool:
    return row.get('target') == 'self_hosted'


def _verify_token(original: Callable) -> Callable:
    @wraps(original)
    def verify(token: str) -> str:
        from firebase_admin.auth import CertificateFetchError, ExpiredIdTokenError, InvalidIdTokenError
        from jwt import ExpiredSignatureError
        from utils import auth_shim

        try:
            return auth_shim.verify_id_token(token)['uid']
        except auth_shim.CertificateFetchError as error:
            raise CertificateFetchError('Authentication authority unavailable', error) from error
        except auth_shim.InvalidIdTokenError as error:
            if isinstance(error.__cause__, ExpiredSignatureError):
                raise ExpiredIdTokenError('Token expired', error) from error
            raise InvalidIdTokenError('Invalid authorization token') from error

    return verify


def _http_consumer(original: Callable) -> Callable:
    @wraps(original)
    def get_uid(*args: Any, **kwargs: Any) -> str:
        from fastapi import HTTPException
        from firebase_admin.auth import CertificateFetchError

        try:
            return original(*args, **kwargs)
        except CertificateFetchError as error:
            raise HTTPException(
                status_code=503, detail={'code': 'auth_service_unavailable', 'retryable': True}
            ) from error

    return get_uid


def _ws_failure(original: Callable) -> Callable:
    @wraps(original)
    def close(error: Exception) -> tuple[int, str]:
        from firebase_admin.auth import CertificateFetchError

        if isinstance(error, CertificateFetchError):
            return 1013, 'Authentication authority unavailable; retry later'
        return original(error)

    return close


def _first_message_consumer(original: Callable) -> Callable:
    @wraps(original)
    async def get_uid(*args: Any, **kwargs: Any) -> str:
        from fastapi import WebSocketException
        from firebase_admin.auth import CertificateFetchError, ExpiredIdTokenError

        try:
            return await original(*args, **kwargs)
        except CertificateFetchError as error:
            raise WebSocketException(code=1013, reason='Authentication authority unavailable; retry later') from error
        except ExpiredIdTokenError as error:
            raise WebSocketException(code=4001, reason='Token refresh required') from error

    return get_uid


def patches() -> list[Patch]:
    return [
        Patch(
            name=f'auth.{attribute}',
            module='utils.other.endpoints',
            attribute=attribute,
            build=build,
            applies_to=_self_hosted,
            reason='auth authority outages must remain retryable through HTTP and WebSocket consumers',
        )
        for attribute, build in (
            ('verify_token', _verify_token),
            ('get_current_user_uid', _http_consumer),
            ('get_current_user_uid_no_byok_validation', _http_consumer),
            ('_get_ws_auth_close', _ws_failure),
            ('get_current_user_uid_from_ws_message', _first_message_consumer),
        )
    ]
