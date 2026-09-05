"""Hermetic contracts; actual licensed-model inference is a separate Docker probe."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

from fork import profile, speech
from fork.model_contract import validate_speech
from fork.speech_assets import manifest_bytes, verify
from utils.sensevoice.socket import SenseVoiceSocket, decode_pcm
from utils.sensevoice.prerecorded_provider import SenseVoicePrerecordedProvider


def selected():
    # Load the production profile renderer, not a second model identity fixture.
    import sys
    from importlib.util import spec_from_file_location, module_from_spec

    root = Path(__file__).resolve().parents[3]
    with mock.patch.object(sys, 'path', [str(root / 'scripts/profiles'), *sys.path]):
        spec = spec_from_file_location('speech_test_renderer', root / 'scripts/profiles/render.py')
        renderer = module_from_spec(spec)
        spec.loader.exec_module(renderer)
        return renderer.resolve('self_hosted', stage='local')['profiles']['self_hosted.local']


def test_bundle_verification_rejects_corruption_missing_and_unlisted_files(tmp_path):
    value = validate_speech(selected()['speech'])
    for model in (value.stt_model, value.tts_model):
        (tmp_path / model).mkdir()
        (tmp_path / model / 'model.onnx').write_bytes(b'controlled artifact')
    encoded = manifest_bytes(tmp_path, value)
    value = replace(value, bundle_digest='sha256:' + hashlib.sha256(encoded).hexdigest())
    (tmp_path / 'manifest.json').write_bytes(encoded)
    verify(tmp_path, value)
    victim = tmp_path / value.stt_model / 'model.onnx'
    victim.write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        verify(tmp_path, value)
    victim.write_bytes(b'controlled artifact')
    (victim.parent / 'extra').write_bytes(b'unlisted')
    with pytest.raises(ValueError):
        verify(tmp_path, value)
    (victim.parent / 'extra').unlink()
    victim.unlink()
    with pytest.raises(ValueError):
        verify(tmp_path, value)


def test_selection_has_no_vendor_failover_and_rejects_unknown_language(monkeypatch):
    row = selected()
    monkeypatch.setattr(profile, 'current', lambda: row)
    assert speech.prerecorded_selection('zh-CN') == ('sensevoice', 'zh', row['speech']['stt_model'])
    assert speech.streaming_selection('en', exclude=frozenset({'sensevoice'})) == (None, None, None)
    with pytest.raises(Exception) as error:
        speech.streaming_selection('de')
    assert error.value.retryable is False
    monkeypatch.setattr(profile, 'current', lambda: {'target': 'self_hosted'})
    from fork.capabilities import CapabilityDisabled

    with pytest.raises(CapabilityDisabled):
        speech.prerecorded_selection('en')


def test_runtime_version_and_missing_store_are_fatal(monkeypatch, tmp_path):
    import sys

    value = validate_speech(selected()['speech'])
    with monkeypatch.context() as context:
        context.setitem(sys.modules, 'sherpa_onnx', SimpleNamespace(__version__='wrong'))
        with pytest.raises(speech.SpeechError, match='version_mismatch'):
            speech.Runtime(value, tmp_path)
    with monkeypatch.context() as context:
        context.setitem(sys.modules, 'sherpa_onnx', SimpleNamespace(__version__=value.runtime_version))
        context.setattr(speech, 'verify', lambda *args: None)
        with pytest.raises(speech.SpeechError, match='vad_artifact_mismatch'):
            speech.Runtime(replace(value, vad_digest='sha256:' + '0' * 64), tmp_path)
    monkeypatch.setattr(profile, 'current', selected)
    monkeypatch.delenv('SPEECH_MODEL_STORE', raising=False)
    speech.runtime.cache_clear()
    with pytest.raises(speech.SpeechError, match='model_store_required'):
        speech.runtime()


def test_bootstrap_rejects_independent_speech_configuration_before_model_load(monkeypatch):
    from fork import bootstrap

    monkeypatch.setattr(profile, 'current', selected)
    monkeypatch.setattr(bootstrap, '_require_modules', lambda *args: None)
    monkeypatch.setenv('FIRESTORE_PG_DSN', 'postgresql+psycopg://unused')
    monkeypatch.setenv('REDIS_DB_HOST', 'unused')
    monkeypatch.setenv('REDIS_DB_PASSWORD', 'synthetic')
    monkeypatch.setenv('SPEECH_MODEL_STORE', '/models/speech')
    monkeypatch.setenv('SENSEVOICE_NUM_THREADS', '99')
    bootstrap.bootstrap.cache_clear()
    try:
        with pytest.raises(profile.ProfileError, match='SENSEVOICE_NUM_THREADS conflicts'):
            bootstrap.bootstrap()
    finally:
        bootstrap.bootstrap.cache_clear()


def test_url_egress_redirect_and_unsupported_options_never_reach_inference(monkeypatch):
    from utils.sensevoice import prerecorded_provider as module
    import httpx

    provider = SenseVoicePrerecordedProvider(object())
    monkeypatch.setattr(module, 'decode_pcm', mock.Mock(side_effect=AssertionError('unexpected inference')))
    network = mock.Mock(side_effect=AssertionError('unexpected network'))
    monkeypatch.setattr(module.httpx, 'stream', network)
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    with pytest.raises(Exception):
        provider.transcribe_url('https://api.deepgram.com/private')
    with pytest.raises(Exception):
        provider.transcribe_url('http://minio:9000/audio.wav', keywords=['unsupported'])
    network.assert_not_called()
    response = httpx.Response(
        302,
        headers={'location': 'https://api.deepgram.com/private'},
        request=httpx.Request('GET', 'http://minio:9000/audio.wav'),
    )
    context = mock.MagicMock()
    context.__enter__.return_value = response
    network.side_effect = None
    network.return_value = context
    with pytest.raises(httpx.HTTPStatusError):
        provider.transcribe_url('http://minio:9000/audio.wav')
    assert network.call_args.kwargs['follow_redirects'] is False


def test_provisioning_rejects_archive_path_escape(tmp_path):
    import io
    import tarfile
    from importlib.util import spec_from_file_location, module_from_spec

    root = Path(__file__).resolve().parents[3]
    spec = spec_from_file_location('speech_provision_test', root / 'deploy/self-host/prepare-speech.py')
    provision = module_from_spec(spec)
    spec.loader.exec_module(provision)
    archive = tmp_path / 'unsafe.tar.bz2'
    with tarfile.open(archive, 'w:bz2') as output:
        entry = tarfile.TarInfo('../escape')
        entry.size = 4
        output.addfile(entry, io.BytesIO(b'evil'))
    with pytest.raises(ValueError, match='unsafe paths'):
        provision.extract(archive, tmp_path / 'models')
    assert not (tmp_path / 'escape').exists()


class Recognizer:
    def __init__(self, fail=False):
        self.samples = None
        self.fail = fail

    def create_stream(self):
        def accept(rate, samples):
            self.samples = list(samples)

        return SimpleNamespace(accept_waveform=accept, result=SimpleNamespace(text='controlled decoder result'))

    def decode_stream(self, stream):
        if self.fail:
            raise RuntimeError('controlled native failure')


def test_pcm_is_normalized_and_invalid_frames_never_reach_native_decoder():
    recognizer = Recognizer()
    assert decode_pcm(recognizer, 16000, np.array([-32768, 0, 16384], dtype='<i2').tobytes())
    assert recognizer.samples == [-1.0, 0.0, 0.5]
    with pytest.raises(ValueError):
        decode_pcm(recognizer, 16000, b'odd')
    provider = SenseVoicePrerecordedProvider(recognizer)
    words = provider.transcribe_bytes(b'\x00\x01' * 160, encoding='linear16', diarize=False, language='en')
    assert words[0]['text'] == 'controlled decoder result'
    with pytest.raises(Exception) as error:
        provider.transcribe_bytes(b'odd', encoding='linear16', diarize=False)
    assert error.value.retryable is False


@pytest.mark.parametrize('timeout', [False, True])
def test_encoded_audio_decode_errors_keep_typed_outcome(monkeypatch, timeout):
    import subprocess
    from utils.sensevoice import prerecorded_provider as module

    failure = subprocess.TimeoutExpired('ffmpeg', 20) if timeout else subprocess.CalledProcessError(1, 'ffmpeg')
    monkeypatch.setattr(module.subprocess, 'run', mock.Mock(side_effect=failure))
    with pytest.raises(Exception) as error:
        module._as_pcm16_mono(b'encoded fixture', sample_rate=16000, channels=1, encoding='audio/wav')
    assert error.value.outcome.value == ('timeout' if timeout else 'invalid_input')


@pytest.mark.asyncio
async def test_stream_finalization_is_not_success_after_native_failure():
    socket = SenseVoiceSocket(recognizer=Recognizer(fail=True), poll_seconds=0.001)
    socket.start()
    assert socket.send(b'\x00\x01' * 160)
    with pytest.raises(RuntimeError, match='finalization'):
        await socket.drain_and_close()
    assert socket.is_connection_dead
    assert socket.send(b'\x00\x01') is False


@pytest.mark.asyncio
async def test_stream_vad_silence_skips_decoder_but_vad_fault_fails_drain():
    seen = []
    detector = mock.Mock(return_value=True)
    socket = SenseVoiceSocket(
        recognizer=Recognizer(fail=True), silence_detector=detector, transcript_callback=seen.extend
    )
    socket.start()
    assert socket.send(b'\x00\x00' * 1600)
    await socket.drain_and_close()
    assert seen == [] and detector.call_count == 1
    detector.side_effect = RuntimeError('controlled VAD failure')
    socket = SenseVoiceSocket(recognizer=Recognizer(), silence_detector=detector)
    socket.start()
    assert socket.send(b'\x00\x01' * 1600)
    with pytest.raises(RuntimeError, match='finalization'):
        await socket.drain_and_close()


def test_stream_backlog_and_misaligned_frames_reject_before_buffer_growth():
    socket = SenseVoiceSocket(recognizer=Recognizer())
    assert not socket.send(b'\x00\x01' * (16000 * 15 + 1))
    assert not socket._pcm
    socket = SenseVoiceSocket(recognizer=Recognizer())
    assert not socket.send(b'odd')
    assert not socket._pcm


@pytest.mark.parametrize('samples', [[], [float('nan')], [0.0]])
def test_native_tts_invalid_result_never_becomes_successful_audio(samples):
    import threading

    runtime = object.__new__(speech.Runtime)
    runtime.tts_gate = threading.Lock()
    runtime.tts = SimpleNamespace(generate=lambda *args, **kwargs: SimpleNamespace(samples=samples, sample_rate=24000))
    with pytest.raises(speech.SpeechError, match='invalid_audio_result'):
        runtime.synthesize('test', 'af_heart')
    assert not runtime.tts_gate.locked()
    with runtime.tts_gate:
        with pytest.raises(speech.SpeechError, match='busy'):
            runtime.synthesize('test', 'af_heart')


@pytest.mark.asyncio
async def test_listen_receiver_socket_is_owned_by_the_fork_patch(monkeypatch):
    from fork.patches.speech import patches
    from routers.listen.receiver import ListenReceiver
    from utils.sensevoice import socket as local_socket
    from utils.stt.streaming import STTService

    patch = next(
        patch
        for patch in patches()
        if patch.module == 'routers.listen.receiver' and patch.attribute == 'ListenReceiver'
    )
    created = object()
    monkeypatch.setattr(local_socket, 'SenseVoiceSocket', lambda **kwargs: created)
    receiver = type('Receiver', (), {'host': type('Host', (), {'stt_service': STTService.sensevoice})()})()

    patched = patch.build(ListenReceiver)
    assert await patched._create_stt_socket(receiver, lambda _segments: None, 16000) is created
