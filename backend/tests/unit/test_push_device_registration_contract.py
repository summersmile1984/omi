from __future__ import annotations

import pytest

from config.push_provider import (
    PUSH_DEVICE_TOKEN_SCHEMA,
    PUSH_DEVICE_TOKEN_TYPE,
    normalize_device_registration,
)
from models.other import SaveFcmTokenRequest


def test_registration_is_provider_neutral_and_preserves_legacy_headers() -> None:
    request = SaveFcmTokenRequest(fcm_token='opaque-apns-or-fcm-token', time_zone='Asia/Shanghai')
    registration = normalize_device_registration(
        token=request.fcm_token,
        platform='iOS',
        device_id_hash='A' * 64,
    )

    assert registration == {
        'schema': PUSH_DEVICE_TOKEN_SCHEMA,
        'token_type': PUSH_DEVICE_TOKEN_TYPE,
        'platform': 'ios',
        'device_id_hash': 'a' * 64,
        'token': 'opaque-apns-or-fcm-token',
    }
    assert request.token_type == PUSH_DEVICE_TOKEN_TYPE


def test_registration_defaults_unknown_legacy_headers() -> None:
    assert normalize_device_registration(token='opaque-token') == {
        'schema': PUSH_DEVICE_TOKEN_SCHEMA,
        'token_type': PUSH_DEVICE_TOKEN_TYPE,
        'platform': 'unknown',
        'device_id_hash': 'default',
        'token': 'opaque-token',
    }


@pytest.mark.parametrize(
    ('kwargs', 'message'),
    [
        ({'token': '   '}, 'non-empty'),
        ({'token': 'token', 'platform': 'APNS/FCM'}, 'lowercase identifier'),
        ({'token': 'token', 'device_id_hash': 'not-a-hash'}, 'SHA-256'),
    ],
)
def test_registration_rejects_ambiguous_identity_headers(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_device_registration(**kwargs)


def test_request_rejects_empty_token_and_unknown_token_type() -> None:
    with pytest.raises(ValueError):
        SaveFcmTokenRequest(fcm_token='', time_zone='UTC')
    with pytest.raises(ValueError):
        SaveFcmTokenRequest(fcm_token='token', time_zone='UTC', token_type='firebase')
