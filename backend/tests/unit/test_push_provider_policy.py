from __future__ import annotations

import pytest

from config.push_provider import (
    PushProviderConfigurationError,
    push_webhook_config,
    selected_push_provider,
    validate_push_provider,
)


def test_neutral_profile_omitted_push_provider_fails_closed() -> None:
    assert selected_push_provider({'OMI_DEPLOYMENT_PROFILE': 'neutral'}) == 'disabled'
    assert selected_push_provider({'OMI_DEPLOYMENT_PROFILE': 'self-hosted'}) == 'disabled'


def test_managed_profile_preserves_firebase_default() -> None:
    assert selected_push_provider({'OMI_DEPLOYMENT_PROFILE': 'managed'}) == 'firebase'
    assert selected_push_provider({}) == 'firebase'


def test_explicit_provider_wins_profile_default() -> None:
    assert selected_push_provider({'OMI_DEPLOYMENT_PROFILE': 'neutral', 'PUSH_PROVIDER': 'firebase'}) == 'firebase'
    assert selected_push_provider({'PUSH_PROVIDER': 'disabled'}) == 'disabled'


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(PushProviderConfigurationError, match="unsupported PUSH_PROVIDER='unknown'"):
        validate_push_provider({'PUSH_PROVIDER': 'unknown'})


def test_operator_webhook_requires_explicit_public_https_contract() -> None:
    env = {
        'OMI_DEPLOYMENT_PROFILE': 'self_hosted',
        'PUSH_PROVIDER': 'webhook',
        'PUSH_WEBHOOK_URL': 'https://notify.example.test/omi',
        'PUSH_WEBHOOK_SECRET': 'operator-secret-1234',
    }
    assert validate_push_provider(env) == 'webhook'
    config = push_webhook_config(env)
    assert config.url == env['PUSH_WEBHOOK_URL']
    assert config.timeout_seconds == 5


@pytest.mark.parametrize(
    'url',
    [
        'http://notify.example.test/omi',
        'https://user:password@notify.example.test/omi',
        'https://notify.example.test/omi?secret=leak',
    ],
)
def test_operator_webhook_rejects_unsafe_url_shapes(url: str) -> None:
    with pytest.raises(PushProviderConfigurationError, match='PUSH_WEBHOOK_URL'):
        validate_push_provider(
            {
                'PUSH_PROVIDER': 'webhook',
                'PUSH_WEBHOOK_URL': url,
                'PUSH_WEBHOOK_SECRET': 'operator-secret-1234',
            }
        )
