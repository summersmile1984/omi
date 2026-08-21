from __future__ import annotations

import pytest

from config.push_provider import selected_push_provider, validate_push_provider


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
    with pytest.raises(ValueError, match="unsupported PUSH_PROVIDER='unknown'"):
        validate_push_provider({'PUSH_PROVIDER': 'unknown'})


def test_operator_webhook_requires_explicit_receiver_contract(tmp_path) -> None:
    secret = tmp_path / 'push.secret'
    secret.write_bytes(b'must-not-be-logged-' + b'x' * 32)
    secret.chmod(0o600)
    with pytest.raises(ValueError, match='HTTPS URL') as error:
        validate_push_provider(
            {
                'OMI_DEPLOYMENT_PROFILE': 'self_hosted',
                'PUSH_PROVIDER': 'webhook',
                'PUSH_WEBHOOK_URL': 'http://10.0.0.4/push',
                'PUSH_WEBHOOK_SECRET_FILE': str(secret),
            }
        )

    assert 'must-not-be-logged' not in str(error.value)


def test_operator_webhook_is_accepted_only_with_private_secret_file(tmp_path) -> None:
    secret = tmp_path / 'push.secret'
    secret.write_bytes(b'x' * 32)
    secret.chmod(0o600)
    assert (
        validate_push_provider(
            {
                'OMI_DEPLOYMENT_PROFILE': 'self_hosted',
                'PUSH_PROVIDER': 'webhook',
                'PUSH_WEBHOOK_URL': 'https://push.example.test/push',
                'PUSH_WEBHOOK_SECRET_FILE': str(secret),
            }
        )
        == 'webhook'
    )
