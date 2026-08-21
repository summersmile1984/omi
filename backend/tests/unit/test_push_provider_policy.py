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
    with pytest.raises(ValueError, match="unsupported PUSH_PROVIDER='webhook'"):
        validate_push_provider({'PUSH_PROVIDER': 'webhook'})
