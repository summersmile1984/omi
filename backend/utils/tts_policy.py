"""Deployment-selected TTS availability policy shared by mobile and desktop routes."""

from __future__ import annotations

import os

TTS_DISABLED_DETAIL = {
    'code': 'model_capability_unavailable',
    'capability': 'tts',
    'reason': 'disabled',
    'retryable': False,
}


def tts_provider_missing_in_neutral_deployment() -> bool:
    """Return whether a neutral deployment omitted its TTS boundary.

    Managed deployments historically infer their TTS provider from the
    presence of a managed credential.  That inference is unsafe for a
    self-hosted process: an ambient ``ELEVENLABS_API_KEY`` or
    ``OPENAI_API_KEY`` must never turn an omitted provider setting into a
    vendor request.  The Compose profile requires ``TTS_PROVIDER`` at
    assembly time; this request-boundary guard covers direct launches and
    stale containers as well.
    """

    profile = os.getenv('OMI_DEPLOYMENT_PROFILE', '').strip().lower()
    neutral = profile in {'neutral', 'self_hosted', 'self-hosted'}
    return neutral and not os.getenv('TTS_PROVIDER', '').strip()


def tts_official_provider_forbidden_in_neutral(provider: str) -> bool:
    """Return whether a fixed vendor TTS provider is forbidden in neutral mode.

    Compatible and local transports remain operator-selected.  ``elevenlabs``
    and ``openai`` are different: their routes are hard-coded to official
    vendor authorities, so an ambient credential or an explicit provider token
    must not turn a neutral process into a vendor proxy.
    """

    profile = os.getenv('OMI_DEPLOYMENT_PROFILE', '').strip().lower()
    neutral = profile in {'neutral', 'self_hosted', 'self-hosted'}
    return neutral and provider.strip().lower() in {'elevenlabs', 'openai'}


def tts_explicitly_disabled() -> bool:
    return os.getenv('TTS_PROVIDER', '').strip().lower() == 'disabled'
