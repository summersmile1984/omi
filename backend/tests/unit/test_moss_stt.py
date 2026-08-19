from __future__ import annotations

import pytest

from config.prerecorded_stt import PrerecordedSTTConfigurationError, PrerecordedSTTService
from utils.stt import pre_recorded


def test_moss_literal_selects_the_policy_admitted_prerecorded_provider(monkeypatch) -> None:
    monkeypatch.setenv('STT_PRERECORDED_MODEL', 'moss')

    assert pre_recorded.get_prerecorded_service('zh-CN') == (
        PrerecordedSTTService.MOSS,
        'zh',
        'moss-transcribe-diarize',
    )


def test_moss_provider_fails_closed_without_its_api_key(monkeypatch) -> None:
    monkeypatch.setenv('STT_PRERECORDED_MODEL', 'moss')
    monkeypatch.delenv('MOSS_API_KEY', raising=False)

    with pytest.raises(PrerecordedSTTConfigurationError) as exc_info:
        pre_recorded.get_prerecorded_provider('zh')

    assert exc_info.value.provider == PrerecordedSTTService.MOSS
    assert exc_info.value.missing_env == 'MOSS_API_KEY'


def test_moss_provider_constructs_through_the_production_selector(monkeypatch) -> None:
    from utils.moss_pipeline import prerecorded_provider

    class FakeMossProvider:
        pass

    monkeypatch.setenv('STT_PRERECORDED_MODEL', 'moss')
    monkeypatch.setenv('MOSS_API_KEY', 'test-key')
    monkeypatch.setattr(prerecorded_provider, 'MossPrerecordedProvider', FakeMossProvider)

    assert isinstance(pre_recorded.get_prerecorded_provider('zh'), FakeMossProvider)
