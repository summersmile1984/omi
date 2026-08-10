"""Unit tests for the desktop TTS endpoint's MiMo (self-hosted) branch.

Hermetic: never touches the real MiMo API. Covers the opt-in routing so the
desktop `/v1/tts/synthesize` endpoint uses MiMo-TTS when configured.
"""

from __future__ import annotations

import pytest

from routers.desktop_tts_updates import mimo_tts_enabled


def test_mimo_enabled_only_when_provider_and_key_set(monkeypatch):
    monkeypatch.delenv('TTS_PROVIDER', raising=False)
    monkeypatch.delenv('MIMO_API_KEY', raising=False)
    assert mimo_tts_enabled() is False

    monkeypatch.setenv('TTS_PROVIDER', 'mimo')
    monkeypatch.delenv('MIMO_API_KEY', raising=False)
    assert mimo_tts_enabled() is False  # no key

    monkeypatch.setenv('MIMO_API_KEY', 'key')
    assert mimo_tts_enabled() is True

    monkeypatch.setenv('TTS_PROVIDER', 'openai')
    assert mimo_tts_enabled() is False  # other provider wins


def test_mimo_synthesize_uses_client_and_returns_audio(monkeypatch):
    audio = b'RIFF\x24\x00\x00\x00WAVE'
    captured = {}

    class _FakeClient:
        def synthesize(self, text, **kwargs):
            captured['text'] = text
            return audio

    import utils.mimo_pipeline.tts as tts_mod
    import routers.desktop_tts_updates as mod

    monkeypatch.setattr(tts_mod, 'MimoTTSClient', _FakeClient)
    monkeypatch.setattr(
        mod,
        'run_blocking',
        lambda executor, fn, *args, **kwargs: asyncio_future(fn(*args, **kwargs)),
    )

    result = asyncio_run(mod._mimo_tts_synthesize('你好测试'))
    assert result == audio
    assert captured['text'] == '你好测试'


def test_mimo_synthesize_raises_on_client_error(monkeypatch):
    class _ErrClient:
        def synthesize(self, text, **kwargs):
            raise RuntimeError('boom')

    import utils.mimo_pipeline.tts as tts_mod
    import routers.desktop_tts_updates as mod

    monkeypatch.setattr(tts_mod, 'MimoTTSClient', _ErrClient)
    monkeypatch.setattr(tts_mod, 'MimoTTSAPIError', RuntimeError)
    monkeypatch.setattr(
        mod,
        'run_blocking',
        lambda executor, fn, *args, **kwargs: fn(*args, **kwargs),
    )

    with pytest.raises(RuntimeError, match='boom'):
        asyncio_run(mod._mimo_tts_synthesize('你好'))


def asyncio_run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def asyncio_future(value):
    import asyncio
    f = asyncio.get_event_loop().create_future()
    f.set_result(value)
    return f
