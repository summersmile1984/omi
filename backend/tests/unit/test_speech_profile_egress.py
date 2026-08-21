"""Neutral egress guards for speech-profile matching.

The synchronous helper uses ``httpx.post`` directly, while the async helper
uses the shared STT client. Both must reject an undeclared authority before
reading or uploading customer audio.
"""

import pytest

from utils.egress_policy import EgressPolicyUnavailable
from utils.stt import speech_profile


def test_sync_speech_profile_rejects_undeclared_authority_before_audio_read(monkeypatch, tmp_path):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)
    monkeypatch.setenv('HOSTED_SPEECH_PROFILE_API_URL', 'https://speech-profile.operator.example/match')

    with pytest.raises(EgressPolicyUnavailable, match='egress_allowlist_not_configured'):
        speech_profile.get_speech_profile_matching_predictions('uid-1', str(tmp_path / 'missing.wav'), [{'text': 'hi'}])


@pytest.mark.asyncio
async def test_async_speech_profile_rejects_undeclared_authority_before_audio_read(monkeypatch, tmp_path):
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)
    monkeypatch.setenv('HOSTED_SPEECH_PROFILE_API_URL', 'https://speech-profile.operator.example/match')

    with pytest.raises(EgressPolicyUnavailable, match='egress_allowlist_not_configured'):
        await speech_profile.async_get_speech_profile_matching_predictions(
            'uid-1', str(tmp_path / 'missing.wav'), [{'text': 'hi'}]
        )
