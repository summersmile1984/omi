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


def test_explicit_mimo_profile_preserves_embedding_and_bounds_egress(monkeypatch):
    from fork import operator_ai, capabilities
    from fork.egress_policy import assert_http_endpoint_allowed, EgressPolicyUnavailable

    original = selected()
    row = operator_ai.configure(original, 'mimo-cn')
    assert row['embedding'] == original['embedding']
    assert 'llm' not in row and 'speech' not in row
    capabilities.validate(row)
    monkeypatch.setattr(profile, 'current', lambda: row)
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    endpoint = operator_ai.MiMo().base_url + '/chat/completions'
    assert assert_http_endpoint_allowed(endpoint) == 'token-plan-cn.xiaomimimo.com'
    assert speech.prerecorded_selection('zh-CN') == ('mimo', 'zh', 'mimo-v2.5-asr')
    assert speech.streaming_selection('en', exclude={'mimo'}) == (None, None, None)
    for url in (endpoint + '?extra=1', endpoint.replace('https:', 'http:'), endpoint.replace('token-plan-cn', 'api')):
        with pytest.raises(EgressPolicyUnavailable):
            assert_http_endpoint_allowed(url)
    for stage in ('beta', 'production'):
        released = operator_ai.configure({**original, 'stage': stage}, 'mimo-cn')
        assert operator_ai.select(released).model == 'mimo-v2.5'
    with pytest.raises(ValueError):
        operator_ai.configure({**original, 'target': 'cloudflare'}, 'mimo-cn')
    monkeypatch.setattr(profile, 'current', lambda: original)
    with pytest.raises(EgressPolicyUnavailable):
        assert_http_endpoint_allowed(endpoint)


def test_mimo_speech_uses_documented_audio_protocol_and_hides_provider_error(monkeypatch):
    import httpx
    from fork import operator_ai, mimo_speech

    row = operator_ai.configure(selected(), 'mimo-cn')
    monkeypatch.setattr(profile, 'current', lambda: row)
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted.local')
    monkeypatch.setenv('MIMO_API_KEY', 'synthetic-secret')
    monkeypatch.delenv('MIMO_SECRET_FILE', raising=False)
    sent = []

    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        assert request.url.path == '/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer synthetic-secret'
        return httpx.Response(
            200,
            json={
                'model': payload['model'],
                'choices': [{'finish_reason': 'stop', 'message': {'content': '茉莉花茶'}}],
                'usage': {'seconds': 2},
            },
        )

    request = mimo_speech.request
    monkeypatch.setattr(
        mimo_speech, 'request', lambda payload: request(payload, transport=httpx.MockTransport(handler))
    )
    result = mimo_speech.Client().transcribe_audio(b'controlled-audio', language='zh-CN')
    assert result.text == '茉莉花茶' and result.duration == 2
    assert sent[0]['asr_options'] == {'language': 'zh'}
    assert sent[0]['messages'][0]['content'][0]['input_audio']['data'].startswith('data:audio/wav;base64,')
    with pytest.raises(speech.SpeechError, match='speech_provider_http_401') as failure:
        request(
            {'model': 'mimo-v2.5-asr'},
            transport=httpx.MockTransport(lambda _: httpx.Response(401, text='private audio and synthetic-secret')),
        )
    assert 'synthetic-secret' not in str(failure.value)
    assert not failure.value.retryable


@pytest.mark.asyncio
async def test_selected_mimo_socket_does_not_construct_the_local_recognizer(monkeypatch):
    from contextlib import ExitStack
    from fork import operator_ai, mimo_speech
    from fork.patches.speech import patches

    row = operator_ai.configure(selected(), 'mimo-cn')
    monkeypatch.setattr(profile, 'current', lambda: row)
    monkeypatch.setattr(speech, 'recognizer', mock.Mock(side_effect=AssertionError('local ASR was constructed')))
    monkeypatch.setenv('SENSEVOICE_SPEAKER_MODE', 'single_speaker')
    monkeypatch.setattr('utils.stt.vad.linear16_pcm_is_silent', lambda *args, **kwargs: False)
    monkeypatch.setattr(
        mimo_speech.Client, 'transcribe_audio', lambda *args, **kwargs: SimpleNamespace(text='MiMo transcript')
    )
    received = []
    with ExitStack() as stack:
        for patch in patches():
            if patch.applies_to(row):
                module, original = patch.target()
                stack.enter_context(mock.patch.object(module, patch.attribute, patch.build(original)))
        socket = speech.new_socket(16000, received.extend, 'en')
        assert socket.send(b'\x00\x01' * 1600)
        await socket.drain_and_close()
    assert received[0]['text'] == 'MiMo transcript'
    assert received[0]['end'] == 0.1
    speech.recognizer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [False, True])
async def test_disconnect_waits_for_late_audio_before_persisting_or_refuses_finalization(monkeypatch, failed):
    import asyncio
    from starlette.websockets import WebSocketState
    from utils.async_tasks import WebSocketTaskSupervisor
    from fork import mimo_listen, operator_ai

    monkeypatch.setattr(mimo_listen, 'current', operator_ai.MiMo)
    release, observed = asyncio.Event(), asyncio.Event()
    segments, saved = [], []
    supervisor = WebSocketTaskSupervisor(uid='synthetic', label='listen')

    async def producer():
        await release.wait()
        if failed:
            raise RuntimeError('controlled ASR failure')
        segments.append('last four seconds')

    async def early_consumer():
        return

    async def persist():
        saved.extend(segments)
        segments.clear()

    receiver = supervisor.create_task(producer(), name='receive')
    supervisor.create_lifetime_task(early_consumer(), name='stream_transcript')
    host = SimpleNamespace(
        use_custom_stt=False,
        request=SimpleNamespace(
            websocket=SimpleNamespace(client_state=WebSocketState.DISCONNECTED),
            owner_persistence_blocked=asyncio.Event(),
        ),
        state=SimpleNamespace(close_code=1000, stt_terminal_failure=False),
        task_supervisor=supervisor,
        transcripts=SimpleNamespace(segment_buffer=segments, photo_buffer=[], process_loop=persist),
    )

    async def supervise(**kwargs):
        result = await supervisor.supervise(**kwargs)
        observed.set()
        return result

    completion = asyncio.create_task(mimo_listen.supervise_disconnect(host, supervise, receiver))
    await asyncio.wait_for(observed.wait(), 1)
    assert not completion.done() and not saved
    release.set()
    if failed:
        with pytest.raises(RuntimeError, match='controlled ASR failure'):
            await completion
        assert host.state.close_code == 1011 and host.state.stt_terminal_failure
        assert not saved
    else:
        result = await completion
        assert result.reason == 'disconnect'
        assert saved == ['last four seconds'] and not segments


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
    assert speech.streaming_selection('en') == ('sensevoice', 'en', row['speech']['stt_model'])
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

    monkeypatch.setattr(profile, 'current', selected)

    receiver_patch = next(
        patch
        for patch in patches()
        if patch.module == 'routers.listen.receiver' and patch.attribute == 'ListenReceiver'
    )
    created = object()

    class Original:
        async def _create_stt_socket(self, callback, sample_rate, modulate_callback=None):
            return 'upstream-socket'

    monkeypatch.setattr('utils.sensevoice.socket.SenseVoiceSocket', lambda **kwargs: created)
    patched = receiver_patch.build(Original)
    receiver = patched()
    receiver.host = type('Host', (), {'stt_service': 'sensevoice', 'stt_language': 'en'})()
    assert patched is not Original
    assert await receiver._create_stt_socket(lambda _segments: None, 16000) is created
    receiver.host.stt_service = 'upstream'
    assert await receiver._create_stt_socket(lambda _segments: None, 16000) == 'upstream-socket'
    assert await Original()._create_stt_socket(None, 16000) == 'upstream-socket'
    assert ListenReceiver is not patched
