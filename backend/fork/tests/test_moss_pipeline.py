"""Contract tests for the optional, fork-owned MOSS transport adapter.

The admitted Server OS profile uses SenseVoice.  MOSS remains an explicit
operator adapter and must never register itself in upstream's STT selector.
"""

from __future__ import annotations

import pytest

from utils.moss_pipeline.config import MossConfigurationError, resolve_moss_config, validate_moss_audio_url


def test_moss_requires_an_explicit_operator_endpoint_before_credentials(monkeypatch):
    monkeypatch.delenv('MOSS_API_BASE', raising=False)
    monkeypatch.delenv('MOSS_API_KEY', raising=False)

    with pytest.raises(MossConfigurationError, match='MOSS_API_BASE'):
        resolve_moss_config()


def test_moss_rejects_managed_authorities(monkeypatch):
    monkeypatch.setenv('MOSS_API_BASE', 'https://api.mosi.cn')
    monkeypatch.setenv('MOSS_API_KEY', 'operator-key')

    with pytest.raises(MossConfigurationError, match='managed MOSS authority'):
        resolve_moss_config()


def test_private_moss_audio_requires_an_explicit_allowlist(monkeypatch):
    monkeypatch.delenv('MOSS_AUDIO_URL_ALLOWLIST', raising=False)
    with pytest.raises(MossConfigurationError, match='explicit allowlist'):
        validate_moss_audio_url('http://moss/recording.wav')

    monkeypatch.setenv('MOSS_AUDIO_URL_ALLOWLIST', 'moss')
    assert validate_moss_audio_url('http://moss/recording.wav') == 'http://moss/recording.wav'
