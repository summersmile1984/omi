"""Hermetic hosted-speech contracts; real vendor audio is a separate live probe."""

import io
import json
import wave
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from fork import capabilities, hosted_speech, operator_ai, profile, speech, speech_transport
from utils.mimo_pipeline.mimo_client import MimoTranscription


def selected():
    import sys
    from importlib.util import spec_from_file_location, module_from_spec
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    with mock.patch.object(sys, 'path', [str(root / 'scripts/profiles'), *sys.path]):
        spec = spec_from_file_location('hosted_speech_test_renderer', root / 'scripts/profiles/render.py')
        renderer = module_from_spec(spec)
        spec.loader.exec_module(renderer)
        return renderer.resolve('self_hosted', stage='local')['profiles']['self_hosted.local']


def hosted(name='openrouter'):
    row = operator_ai.configure(selected(), name)
    capabilities.validate(row)
    return operator_ai.select(row)


def mono_wav(seconds=1, sample_rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b'\x00\x01' * sample_rate * seconds)
    return buffer.getvalue()


def stereo_wav(seconds=1, sample_rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b'\x00\x01\x00\x02' * sample_rate * seconds)
    return buffer.getvalue()


def test_transcribe_posts_the_standard_multipart_contract(monkeypatch):
    spec = hosted()
    captured = {}

    def handler(request):
        captured['url'] = str(request.url)
        captured['auth'] = request.headers.get('authorization')
        captured['type'] = request.headers.get('content-type', '')
        captured['body'] = request.read()
        return httpx.Response(
            200,
            content=json.dumps({'text': '你好 世界', 'usage': {'seconds': 1.5}}).encode(),
            headers={'content-type': 'application/json'},
        )

    monkeypatch.setattr(profile, 'current', lambda: operator_ai.configure(selected(), 'openrouter'))
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
    result = hosted_speech.Client().transcribe_audio(
        mono_wav(1), language='zh-CN', transport=httpx.MockTransport(handler)
    )
    assert isinstance(result, MimoTranscription)
    assert result.text == '你好 世界'
    assert result.duration == 1.5
    assert captured['url'].endswith('/audio/transcriptions')
    assert captured['auth'] == 'Bearer or-key'
    assert 'multipart/form-data' in captured['type']
    assert b'name="model"' in captured['body']
    assert spec.asr_model.encode() in captured['body']


def test_transcribe_typed_errors_never_expose_provider_bodies(monkeypatch):
    hosted()
    monkeypatch.setattr(profile, 'current', lambda: operator_ai.configure(selected(), 'openrouter'))
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
    for status, retryable in ((429, True), (500, True), (401, False)):

        def handler(request, status=status):
            return httpx.Response(status, content=b'{"error": "provider-secret-body"}')

        with pytest.raises(speech.SpeechError) as error:
            hosted_speech.Client().transcribe_audio(mono_wav(1), transport=httpx.MockTransport(handler))
        assert error.value.retryable is retryable
        assert 'provider-secret-body' not in error.value.code

    def malformed(request):
        return httpx.Response(200, content=b'not-json')

    with pytest.raises(speech.SpeechError):
        hosted_speech.Client().transcribe_audio(mono_wav(1), transport=httpx.MockTransport(malformed))

    def missing_text(request):
        return httpx.Response(200, content=json.dumps({'usage': {'seconds': 1}}).encode())

    with pytest.raises(speech.SpeechError):
        hosted_speech.Client().transcribe_audio(mono_wav(1), transport=httpx.MockTransport(missing_text))
    with pytest.raises(speech.SpeechError):
        hosted_speech.Client().transcribe_audio(
            b'', transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'{}'))
        )
    with pytest.raises(speech.SpeechError):
        hosted_speech.Client().transcribe_audio(
            b'x' * (10 * 1024 * 1024 + 1),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'{}')),
        )
    assert MimoTranscription is not None


def test_synthesize_uses_the_frozen_voice_and_normalizes_to_mono_wav(monkeypatch):
    spec = hosted()
    captured = {}

    def handler(request):
        captured['url'] = str(request.url)
        captured['payload'] = json.loads(request.read())
        return httpx.Response(200, content=stereo_wav(1), headers={'content-type': 'audio/mpeg'})

    monkeypatch.setattr(profile, 'current', lambda: operator_ai.configure(selected(), 'openrouter'))
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
    audio = hosted_speech.synthesize('hello', transport=httpx.MockTransport(handler))
    assert captured['url'].endswith('/audio/speech')
    assert captured['payload']['model'] == spec.tts_model
    assert captured['payload']['voice'] == spec.tts_voice
    assert captured['payload']['response_format'] == spec.tts_response_format
    with wave.open(io.BytesIO(audio)) as decoded:
        assert decoded.getnchannels() == 1
        assert decoded.getsampwidth() == 2
        assert 0 < decoded.getnframes() <= decoded.getframerate() * speech.MAX_AUDIO_SECONDS
    mono = mono_wav(1)
    passthrough = hosted_speech.synthesize(
        'hello', transport=httpx.MockTransport(lambda request: httpx.Response(200, content=mono))
    )
    assert passthrough == mono


def test_synthesize_rejects_non_audio_and_oversized_results(monkeypatch):
    hosted()
    monkeypatch.setattr(profile, 'current', lambda: operator_ai.configure(selected(), 'openrouter'))
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')

    def garbage(request):
        return httpx.Response(200, content=b'this is not audio at all')

    with pytest.raises(speech.SpeechError) as error:
        hosted_speech.synthesize('hello', transport=httpx.MockTransport(garbage))
    assert error.value.code == 'speech_invalid_audio_result'

    def oversized(request):
        return httpx.Response(200, content=b'\x00' * (4_000_000 + 1))

    with pytest.raises(speech.SpeechError) as error:
        hosted_speech.synthesize('hello', transport=httpx.MockTransport(oversized))
    assert error.value.code == 'speech_provider_response_limit'


def test_tts_transport_admits_only_the_frozen_hosted_model_and_default_voice(monkeypatch):
    spec = hosted()
    monkeypatch.setattr(profile, 'current', lambda: operator_ai.configure(selected(), 'openrouter'))
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
    with pytest.raises(speech.SpeechError):
        speech_transport._synthesize(SimpleNamespace(text='hi', voice_id='', model_id='kokoro', output_format='wav'))
    with pytest.raises(speech.SpeechError):
        speech_transport._synthesize(
            SimpleNamespace(text='hi', voice_id='custom-vendor-voice', model_id=spec.tts_model, output_format='wav')
        )
    with mock.patch.object(hosted_speech, 'synthesize', return_value=mono_wav(1)) as synthesize:
        audio, media_type = speech_transport._synthesize(
            SimpleNamespace(text='hi', voice_id='alloy', model_id=spec.tts_model, output_format='wav')
        )
        assert media_type == 'audio/wav'
        synthesize.assert_called_once_with('hi')


async def test_hosted_live_socket_replaces_only_inference(monkeypatch):
    hosted()
    row = operator_ai.configure(selected(), 'openrouter')
    monkeypatch.setattr(profile, 'current', lambda: row)
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'or-key')
    from utils.sensevoice.socket import SenseVoiceSocket

    monkeypatch.setattr(speech, 'recognizer', mock.Mock(side_effect=AssertionError('local ASR was constructed')))
    monkeypatch.setenv('SENSEVOICE_SPEAKER_MODE', 'single_speaker')
    monkeypatch.setattr('utils.stt.vad.linear16_pcm_is_silent', lambda *args, **kwargs: False)
    monkeypatch.setattr(
        hosted_speech.Client, 'transcribe_audio', lambda *args, **kwargs: SimpleNamespace(text='hosted transcript')
    )
    received = []
    socket = speech.new_socket(16000, received.extend, 'en')
    assert isinstance(socket, SenseVoiceSocket)
    assert socket.send(b'\x00\x01' * 1600)
    await socket.drain_and_close()
    assert received[0]['text'] == 'hosted transcript'
    assert received[0]['end'] == 0.1
    speech.recognizer.assert_not_called()
    assert speech.streaming_selection('en', exclude={'openrouter'}) == (None, None, None)


def test_prerecorded_dispatch_returns_the_hosted_provider(monkeypatch):
    hosted()
    monkeypatch.setattr(profile, 'current', lambda: operator_ai.configure(selected(), 'openrouter'))
    from fork.patches.speech import patches as speech_patches
    from utils.sensevoice.prerecorded_provider import SenseVoicePrerecordedProvider

    provider_patch = next(
        patch for patch in speech_patches() if patch.name.endswith('utils.stt.pre_recorded.get_prerecorded_provider')
    )
    provider = provider_patch.build(object())()
    assert not isinstance(provider, SenseVoicePrerecordedProvider)
    assert isinstance(provider._client, hosted_speech.Client)
