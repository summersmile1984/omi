from __future__ import annotations

import asyncio
import httpx
import numpy as np
from pathlib import Path
import pytest
import subprocess
import sys
from types import SimpleNamespace
from fastapi import HTTPException

from models.tts import TtsSynthesizeRequest as MobileTtsRequest
from utils.llm.capabilities import ModelCapabilityUnavailableError
from utils.tts_provider import TtsAudio


class _Semaphore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return None


class _Circuit:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.successes = 0
        self.failures = 0

    def allow_request(self) -> bool:
        return self.allowed

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1


def _compatible_env(monkeypatch) -> None:
    monkeypatch.setenv('TTS_PROVIDER', 'openai_compatible')
    monkeypatch.setenv('TTS_OPENAI_COMPATIBLE_BASE_URL', 'http://tts.internal/v1')
    monkeypatch.setenv('TTS_OPENAI_COMPATIBLE_API_KEY', 'operator-key')
    monkeypatch.setenv('TTS_OPENAI_COMPATIBLE_MODEL', 'local-voice-model')
    monkeypatch.setenv('TTS_OPENAI_COMPATIBLE_VOICE', 'local-voice')


def test_tts_provider_import_does_not_load_optional_sherpa_runtime():
    backend_dir = Path(__file__).resolve().parents[2]
    script = """
import builtins

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == 'sherpa_onnx':
        raise AssertionError('optional Sherpa runtime loaded during module import')
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import utils.tts_provider
"""

    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_compatible_tts_fails_before_client_construction_when_endpoint_is_missing(monkeypatch):
    import utils.tts_provider as mod

    monkeypatch.delenv('TTS_OPENAI_COMPATIBLE_BASE_URL', raising=False)
    monkeypatch.setenv('TTS_OPENAI_COMPATIBLE_API_KEY', 'operator-key')
    monkeypatch.setenv('TTS_OPENAI_COMPATIBLE_MODEL', 'local-model')
    monkeypatch.setenv('TTS_OPENAI_COMPATIBLE_VOICE', 'local-voice')
    monkeypatch.setattr(mod, 'get_tts_client', lambda: (_ for _ in ()).throw(AssertionError('must not construct')))

    with pytest.raises(ModelCapabilityUnavailableError) as exc_info:
        await mod.synthesize_openai_compatible_tts('hello')

    assert exc_info.value.as_dict() == {
        'code': 'model_capability_unavailable',
        'capability': 'tts',
        'reason': 'tts_openai_compatible_base_url_not_configured',
        'retryable': False,
    }


@pytest.mark.asyncio
async def test_compatible_tts_uses_only_the_explicit_endpoint_and_returns_audio(monkeypatch):
    import utils.tts_provider as mod

    _compatible_env(monkeypatch)
    captured = {}
    circuit = _Circuit()

    class _Client:
        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return httpx.Response(
                200,
                headers={'Content-Type': 'audio/wav; charset=binary'},
                content=b'RIFF-local-audio',
                request=httpx.Request('POST', url),
            )

    monkeypatch.setattr(mod, 'get_tts_client', lambda: _Client())
    monkeypatch.setattr(mod, 'get_tts_semaphore', lambda: _Semaphore())
    monkeypatch.setattr(mod, 'get_webhook_circuit_breaker', lambda _url: circuit)

    result = await mod.synthesize_openai_compatible_tts(
        'hello', voice='speaker-a', audio_format='wav', instructions='calm'
    )

    assert result == TtsAudio(content=b'RIFF-local-audio', media_type='audio/wav')
    assert captured['url'] == 'http://tts.internal/v1/audio/speech'
    assert captured['headers']['Authorization'] == 'Bearer operator-key'
    assert captured['json'] == {
        'model': 'local-voice-model',
        'input': 'hello',
        'voice': 'speaker-a',
        'response_format': 'wav',
        'instructions': 'calm',
    }
    assert circuit.successes == 1
    assert circuit.failures == 0


@pytest.mark.asyncio
async def test_compatible_tts_rejects_non_audio_provider_payload(monkeypatch):
    import utils.tts_provider as mod

    _compatible_env(monkeypatch)
    circuit = _Circuit()

    class _Client:
        async def post(self, url, **_kwargs):
            return httpx.Response(
                200,
                headers={'Content-Type': 'application/json'},
                content=b'{"error":"not audio"}',
                request=httpx.Request('POST', url),
            )

    monkeypatch.setattr(mod, 'get_tts_client', lambda: _Client())
    monkeypatch.setattr(mod, 'get_tts_semaphore', lambda: _Semaphore())
    monkeypatch.setattr(mod, 'get_webhook_circuit_breaker', lambda _url: circuit)

    with pytest.raises(ModelCapabilityUnavailableError) as exc_info:
        await mod.synthesize_openai_compatible_tts('hello')

    assert exc_info.value.as_dict()['reason'] == 'provider_invalid_audio'
    assert circuit.failures == 1


@pytest.mark.asyncio
async def test_mobile_and_desktop_routes_share_the_compatible_provider_boundary(monkeypatch):
    import routers.desktop_tts_updates as desktop
    import routers.tts as mobile

    _compatible_env(monkeypatch)
    calls = []

    async def fake_synthesize(text, **kwargs):
        calls.append((text, kwargs))
        return TtsAudio(content=b'audio', media_type='audio/wav')

    async def mobile_run_blocking(_executor, fn, *args, **kwargs):
        if fn is mobile.redis_db.check_tts_rate_limit:
            return 0, None
        return fn(*args, **kwargs)

    async def desktop_run_blocking(_executor, fn, *args, **kwargs):
        if fn is desktop.is_desktop_trial_paywalled:
            return False
        if fn is desktop.redis_db.check_tts_rate_limit:
            return 0, None
        return fn(*args, **kwargs)

    monkeypatch.setattr(mobile, 'synthesize_openai_compatible_tts', fake_synthesize)
    monkeypatch.setattr(desktop, 'synthesize_openai_compatible_tts', fake_synthesize)
    monkeypatch.setattr(mobile, 'run_blocking', mobile_run_blocking)
    monkeypatch.setattr(desktop, 'run_blocking', desktop_run_blocking)

    mobile_response = await mobile.tts_synthesize(
        MobileTtsRequest(text='mobile', voice_id='voice-a', output_format='wav'), uid='user-1'
    )
    assert b''.join([chunk async for chunk in mobile_response.body_iterator]) == b'audio'
    desktop_response = await desktop.tts_synthesize(
        desktop.TtsSynthesizeRequest(text='desktop', voice_id='voice-b', instructions='bright'), uid='user-1'
    )

    assert desktop_response.body == b'audio'
    assert calls == [
        ('mobile', {'voice': 'voice-a', 'audio_format': 'wav'}),
        ('desktop', {'voice': 'voice-b', 'audio_format': 'mp3', 'instructions': 'bright'}),
    ]


@pytest.mark.asyncio
async def test_explicit_mimo_selection_never_falls_through_to_residual_vendor_keys(monkeypatch):
    import routers.desktop_tts_updates as desktop
    import routers.tts as mobile

    monkeypatch.setenv('TTS_PROVIDER', 'mimo')
    monkeypatch.delenv('MIMO_API_KEY', raising=False)
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'must-not-be-used')
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-be-used')

    async def desktop_run_blocking(_executor, fn, *_args, **_kwargs):
        if fn is desktop.is_desktop_trial_paywalled:
            return False
        raise AssertionError('missing selected provider must fail before rate/provider work')

    monkeypatch.setattr(desktop, 'run_blocking', desktop_run_blocking)

    with pytest.raises(HTTPException) as mobile_error:
        await mobile.tts_synthesize(MobileTtsRequest(text='hello'), uid='user-1')
    with pytest.raises(HTTPException) as desktop_error:
        await desktop.tts_synthesize(desktop.TtsSynthesizeRequest(text='hello', voice_id='alloy'), uid='user-1')

    assert mobile_error.value.detail['reason'] == 'mimo_credential_not_configured'
    assert desktop_error.value.detail['reason'] == 'mimo_credential_not_configured'


@pytest.mark.asyncio
async def test_sherpa_tts_uses_only_mounted_files_and_returns_valid_wav(monkeypatch, tmp_path):
    import utils.tts_provider as mod

    model = tmp_path / 'model.onnx'
    tokens = tmp_path / 'tokens.txt'
    data_dir = tmp_path / 'espeak-ng-data'
    model.write_bytes(b'operator model')
    tokens.write_text('tokens', encoding='utf-8')
    data_dir.mkdir()
    monkeypatch.setenv('TTS_SHERPA_MODEL', str(model))
    monkeypatch.setenv('TTS_SHERPA_TOKENS', str(tokens))
    monkeypatch.setenv('TTS_SHERPA_DATA_DIR', str(data_dir))
    generated = SimpleNamespace(
        samples=np.sin(np.linspace(0, 20, 1600, dtype=np.float32)),
        sample_rate=16000,
    )
    engine = SimpleNamespace(generate=lambda text, **kwargs: generated)
    monkeypatch.setattr(mod, '_get_sherpa_engine', lambda _config: engine)
    monkeypatch.setattr(mod, 'get_tts_client', lambda: (_ for _ in ()).throw(AssertionError('no HTTP client')))

    result = await mod.synthesize_sherpa_tts('local speech', audio_format='wav')

    assert result.media_type == 'audio/wav'
    assert result.content[:4] == b'RIFF'
    assert len(result.content) > 3200


def test_sherpa_tts_serializes_inference_on_the_shared_engine(monkeypatch, tmp_path):
    import utils.tts_provider as mod

    model = tmp_path / 'model.onnx'
    tokens = tmp_path / 'tokens.txt'
    data_dir = tmp_path / 'espeak-ng-data'
    model.write_bytes(b'operator model')
    tokens.write_text('tokens', encoding='utf-8')
    data_dir.mkdir()
    monkeypatch.setenv('TTS_SHERPA_MODEL', str(model))
    monkeypatch.setenv('TTS_SHERPA_TOKENS', str(tokens))
    monkeypatch.setenv('TTS_SHERPA_DATA_DIR', str(data_dir))

    entered = []

    class _RecordingLock:
        def __enter__(self):
            entered.append('enter')

        def __exit__(self, _exc_type, _exc, _tb):
            entered.append('exit')

    generated = SimpleNamespace(samples=np.ones(1600, dtype=np.float32), sample_rate=16000)
    engine = SimpleNamespace(generate=lambda _text, **_kwargs: generated)
    monkeypatch.setattr(mod, '_sherpa_generation_lock', _RecordingLock())
    monkeypatch.setattr(mod, '_get_sherpa_engine', lambda _config: engine)

    result = mod._generate_sherpa_tts('serialized local speech', 'wav')

    assert result.content[:4] == b'RIFF'
    assert entered == ['enter', 'exit']


@pytest.mark.asyncio
async def test_sherpa_tts_waiters_do_not_occupy_llm_executor_workers(monkeypatch):
    import utils.tts_provider as mod

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    calls = []

    async def fake_run_blocking(_executor, _fn, text, audio_format):
        calls.append((text, audio_format))
        if text == 'first':
            first_entered.set()
            await release_first.wait()
        return TtsAudio(content=text.encode(), media_type='audio/wav')

    monkeypatch.setattr(mod, 'run_blocking', fake_run_blocking)

    first = asyncio.create_task(mod.synthesize_sherpa_tts('first', audio_format='wav'))
    await first_entered.wait()
    second = asyncio.create_task(mod.synthesize_sherpa_tts('second', audio_format='wav'))
    await asyncio.sleep(0)

    assert calls == [('first', 'wav')]

    release_first.set()
    assert await first == TtsAudio(content=b'first', media_type='audio/wav')
    assert await second == TtsAudio(content=b'second', media_type='audio/wav')
    assert calls == [('first', 'wav'), ('second', 'wav')]


@pytest.mark.asyncio
async def test_sherpa_tts_missing_model_fails_before_runtime_or_network(monkeypatch, tmp_path):
    import utils.tts_provider as mod

    tokens = tmp_path / 'tokens.txt'
    data_dir = tmp_path / 'espeak-ng-data'
    tokens.write_text('tokens', encoding='utf-8')
    data_dir.mkdir()
    monkeypatch.setenv('TTS_SHERPA_MODEL', str(tmp_path / 'missing.onnx'))
    monkeypatch.setenv('TTS_SHERPA_TOKENS', str(tokens))
    monkeypatch.setenv('TTS_SHERPA_DATA_DIR', str(data_dir))
    monkeypatch.setattr(mod, '_get_sherpa_engine', lambda _config: (_ for _ in ()).throw(AssertionError('no runtime')))
    monkeypatch.setattr(mod, 'get_tts_client', lambda: (_ for _ in ()).throw(AssertionError('no HTTP client')))

    with pytest.raises(ModelCapabilityUnavailableError) as exc_info:
        await mod.synthesize_sherpa_tts('local speech', audio_format='wav')

    assert exc_info.value.as_dict()['reason'] == 'tts_sherpa_model_not_readable'


@pytest.mark.asyncio
async def test_mobile_and_desktop_routes_share_local_sherpa_boundary(monkeypatch):
    import routers.desktop_tts_updates as desktop
    import routers.tts as mobile

    monkeypatch.setenv('TTS_PROVIDER', 'sherpa_onnx')
    calls = []

    async def fake_synthesize(text, **kwargs):
        calls.append((text, kwargs))
        return TtsAudio(content=b'local-audio', media_type='audio/wav')

    async def mobile_run_blocking(_executor, fn, *args, **kwargs):
        if fn is mobile.redis_db.check_tts_rate_limit:
            return 0, None
        return fn(*args, **kwargs)

    async def desktop_run_blocking(_executor, fn, *args, **kwargs):
        if fn is desktop.is_desktop_trial_paywalled:
            return False
        if fn is desktop.redis_db.check_tts_rate_limit:
            return 0, None
        return fn(*args, **kwargs)

    monkeypatch.setattr(mobile, 'synthesize_sherpa_tts', fake_synthesize)
    monkeypatch.setattr(desktop, 'synthesize_sherpa_tts', fake_synthesize)
    monkeypatch.setattr(mobile, 'run_blocking', mobile_run_blocking)
    monkeypatch.setattr(desktop, 'run_blocking', desktop_run_blocking)

    mobile_response = await mobile.tts_synthesize(MobileTtsRequest(text='mobile', output_format='wav'), uid='user-1')
    desktop_response = await desktop.tts_synthesize(
        desktop.TtsSynthesizeRequest(text='desktop', voice_id='ignored'), uid='user-1'
    )

    assert b''.join([chunk async for chunk in mobile_response.body_iterator]) == b'local-audio'
    assert desktop_response.body == b'local-audio'
    assert calls == [('mobile', {'audio_format': 'wav'}), ('desktop', {'audio_format': 'mp3'})]
