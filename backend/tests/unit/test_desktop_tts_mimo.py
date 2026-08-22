"""Unit tests for the desktop TTS endpoint's MiMo (self-hosted) branch.

Hermetic: never touches the real MiMo API. Covers the opt-in routing so the
desktop `/v1/tts/synthesize` endpoint uses MiMo-TTS when configured.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from routers.desktop_tts_updates import TtsSynthesizeRequest, mimo_tts_enabled


def test_mimo_enabled_only_when_provider_and_key_set(monkeypatch):
    monkeypatch.delenv('TTS_PROVIDER', raising=False)
    monkeypatch.delenv('MIMO_API_KEY', raising=False)
    assert mimo_tts_enabled() is False

    monkeypatch.setenv('TTS_PROVIDER', 'mimo')
    monkeypatch.delenv('MIMO_API_KEY', raising=False)
    assert mimo_tts_enabled() is False  # no key

    monkeypatch.setenv('MIMO_API_KEY', 'key')
    monkeypatch.setenv('MIMO_API_BASE', 'http://operator.example.test/mimo')
    assert mimo_tts_enabled() is True

    monkeypatch.setenv('TTS_PROVIDER', 'openai')
    assert mimo_tts_enabled() is False  # other provider wins


def test_explicit_mimo_selection_does_not_fall_through_to_openai(monkeypatch):
    import routers.desktop_tts_updates as mod
    from fastapi import HTTPException

    monkeypatch.setenv('TTS_PROVIDER', 'mimo')
    monkeypatch.setenv('MIMO_API_KEY', 'key')
    monkeypatch.delenv('MIMO_API_BASE', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-be-used')

    with pytest.raises(HTTPException) as exc_info:
        asyncio_run(mod.tts_synthesize(mod.TtsSynthesizeRequest(text='hello', voice_id='alloy'), uid='user'))
    assert exc_info.value.status_code == 503


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


@pytest.mark.asyncio
async def test_mimo_cannot_bypass_desktop_subscription_gate(monkeypatch):
    import routers.desktop_tts_updates as mod

    called = False

    async def fake_run_blocking(_executor, fn, *args, **kwargs):
        if fn is mod.is_desktop_trial_paywalled:
            return True
        return fn(*args, **kwargs)

    async def fake_synthesize(_text):
        nonlocal called
        called = True
        return b'audio'

    monkeypatch.setenv('TTS_PROVIDER', 'mimo')
    monkeypatch.setenv('MIMO_API_KEY', 'key')
    monkeypatch.setenv('MIMO_API_BASE', 'http://operator.example.test/mimo')
    monkeypatch.setattr(mod, 'run_blocking', fake_run_blocking)
    monkeypatch.setattr(mod, '_mimo_tts_synthesize', fake_synthesize)

    with pytest.raises(HTTPException) as exc_info:
        await mod.tts_synthesize(TtsSynthesizeRequest(text='hello', voice_id='alloy'), uid='user-1')

    assert exc_info.value.status_code == 403
    assert called is False


@pytest.mark.asyncio
async def test_mimo_cannot_bypass_desktop_tts_rate_limit(monkeypatch):
    import routers.desktop_tts_updates as mod

    called = False

    async def fake_run_blocking(_executor, fn, *args, **kwargs):
        if fn is mod.is_desktop_trial_paywalled:
            return False
        if fn is mod.redis_db.check_tts_rate_limit:
            return 1, 60
        return fn(*args, **kwargs)

    async def fake_synthesize(_text):
        nonlocal called
        called = True
        return b'audio'

    monkeypatch.setenv('TTS_PROVIDER', 'mimo')
    monkeypatch.setenv('MIMO_API_KEY', 'key')
    monkeypatch.setenv('MIMO_API_BASE', 'http://operator.example.test/mimo')
    monkeypatch.setattr(mod, 'run_blocking', fake_run_blocking)
    monkeypatch.setattr(mod, '_mimo_tts_synthesize', fake_synthesize)

    with pytest.raises(HTTPException) as exc_info:
        await mod.tts_synthesize(TtsSynthesizeRequest(text='hello', voice_id='alloy'), uid='user-1')

    assert exc_info.value.status_code == 429
    assert called is False


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def asyncio_future(value):
    import asyncio

    f = asyncio.get_running_loop().create_future()
    f.set_result(value)
    return f
