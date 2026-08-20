from __future__ import annotations

import pytest
from fastapi import HTTPException

from models.tts import TtsSynthesizeRequest


@pytest.mark.asyncio
async def test_mimo_obeys_mobile_tts_rate_limit_before_synthesis(monkeypatch):
    import routers.tts as mod
    import utils.mimo_pipeline.tts as mimo_mod

    called = False

    class _Client:
        def synthesize(self, text, **kwargs):
            nonlocal called
            called = True
            return b'audio'

    async def fake_run_blocking(_executor, fn, *args, **kwargs):
        if fn is mod.redis_db.check_tts_rate_limit:
            return 1, 60
        return fn(*args, **kwargs)

    monkeypatch.setenv('TTS_PROVIDER', 'mimo')
    monkeypatch.setenv('MIMO_API_KEY', 'key')
    monkeypatch.setattr(mimo_mod, 'MimoTTSClient', _Client)
    monkeypatch.setattr(mod, 'run_blocking', fake_run_blocking)

    with pytest.raises(HTTPException) as exc_info:
        await mod.tts_synthesize(TtsSynthesizeRequest(text='hello'), uid='user-1')

    assert exc_info.value.status_code == 429
    assert called is False


@pytest.mark.asyncio
async def test_mimo_mobile_success_uses_same_rate_limit_boundary(monkeypatch):
    import routers.tts as mod
    import utils.mimo_pipeline.tts as mimo_mod

    calls = []

    class _Client:
        def synthesize(self, text, **kwargs):
            calls.append(('synthesize', text))
            return b'RIFFaudio'

    async def fake_run_blocking(_executor, fn, *args, **kwargs):
        if fn is mod.redis_db.check_tts_rate_limit:
            calls.append(('rate_limit', kwargs['char_count']))
            return 0, None
        return fn(*args, **kwargs)

    monkeypatch.setenv('TTS_PROVIDER', 'mimo')
    monkeypatch.setenv('MIMO_API_KEY', 'key')
    monkeypatch.setattr(mimo_mod, 'MimoTTSClient', _Client)
    monkeypatch.setattr(mod, 'run_blocking', fake_run_blocking)

    response = await mod.tts_synthesize(TtsSynthesizeRequest(text='hello'), uid='user-1')
    body = b''.join([chunk async for chunk in response.body_iterator])

    assert response.media_type == 'audio/wav'
    assert body == b'RIFFaudio'
    assert calls == [('rate_limit', 5), ('synthesize', 'hello')]
