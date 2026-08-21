from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import numpy as np
import pytest

import utils.sensevoice.prerecorded_provider as prerecorded_module
import utils.sensevoice.speaker as speaker_module
from utils.sensevoice.prerecorded_provider import SenseVoicePrerecordedProvider
from utils.sensevoice.socket import SenseVoiceSocket
from utils.sensevoice.speaker import SenseVoiceSpeakerError, WindowSpeakerClusterer, build_window_speaker_clusterer
from utils.egress_policy import EgressPolicyUnavailable


def _clusterer(vectors: list[list[float]], *, max_clusters: int = 4) -> WindowSpeakerClusterer:
    pending = [np.asarray(vector, dtype=np.float32) for vector in vectors]
    return WindowSpeakerClusterer(
        threshold=0.2,
        max_clusters=max_clusters,
        embedding_extractor=lambda _wav: pending.pop(0),
    )


def test_window_clustering_emits_stable_ids_for_two_speakers() -> None:
    clusterer = _clusterer([[1.0, 0.0], [0.0, 1.0], [0.99, 0.01]])

    assert clusterer.assign_pcm(0, b'\x00\x00' * 4, 4) == 'SPEAKER_00'
    assert clusterer.assign_pcm(1, b'\x01\x00' * 4, 4) == 'SPEAKER_01'
    assert clusterer.assign_pcm(2, b'\x02\x00' * 4, 4) == 'SPEAKER_00'


def test_window_clustering_rejects_out_of_order_result_without_advancing_fence() -> None:
    clusterer = _clusterer([[1.0, 0.0]])

    with pytest.raises(SenseVoiceSpeakerError) as raised:
        clusterer.assign_embedding(1, np.asarray([1.0, 0.0]))

    assert raised.value.reason == 'speaker_window_out_of_order'
    assert raised.value.retryable is False
    assert clusterer.assign_embedding(0, np.asarray([1.0, 0.0])) == 'SPEAKER_00'


def test_window_clustering_uses_nearest_existing_id_at_limit_with_fallback_telemetry(monkeypatch) -> None:
    fallbacks: list[dict] = []
    monkeypatch.setattr(speaker_module, 'record_fallback', lambda **fields: fallbacks.append(fields))
    clusterer = _clusterer([[1.0, 0.0], [0.0, 1.0]], max_clusters=1)
    assert clusterer.assign_pcm(0, b'\x00\x00' * 4, 4) == 'SPEAKER_00'

    assert clusterer.assign_pcm(1, b'\x00\x00' * 4, 4) == 'SPEAKER_00'
    assert fallbacks == [
        {
            'component': 'stt_selection',
            'from_mode': 'new_speaker_cluster',
            'to_mode': 'nearest_speaker_cluster',
            'reason': 'capacity_full',
            'outcome': 'degraded',
        }
    ]


def test_window_clustering_requires_local_provider_before_embedding_or_http(monkeypatch) -> None:
    calls = {'embedding': 0, 'http': 0}
    monkeypatch.setenv('SENSEVOICE_SPEAKER_MODE', 'window_clustering')
    monkeypatch.setenv('SPEAKER_EMBEDDING_PROVIDER', 'http')
    monkeypatch.setenv('SPEAKER_EMBEDDING_API_URL', 'https://speaker.example.invalid')
    monkeypatch.setattr(
        speaker_module,
        'extract_embedding_from_bytes',
        lambda _audio: calls.__setitem__('embedding', calls['embedding'] + 1),
    )
    monkeypatch.setattr(
        'utils.stt.speaker_embedding.httpx.post',
        lambda *_args, **_kwargs: calls.__setitem__('http', calls['http'] + 1),
    )

    with pytest.raises(SenseVoiceSpeakerError) as raised:
        build_window_speaker_clusterer()

    assert raised.value.reason == 'local_speaker_embedding_required'
    assert calls == {'embedding': 0, 'http': 0}


class _Stream:
    def __init__(self, text: str) -> None:
        self.result = SimpleNamespace(text=text)

    def accept_waveform(self, _sample_rate: int, _samples: list[int]) -> None:
        pass


class _Recognizer:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    def create_stream(self) -> _Stream:
        return _Stream(self._texts.pop(0))

    def decode_stream(self, _stream: _Stream) -> None:
        pass


class _ThreadRecordingClusterer:
    def __init__(self, labels: list[str]) -> None:
        self._labels = labels
        self.thread_ids: list[int] = []

    def assign_pcm(self, _index: int, _pcm: bytes, _sample_rate: int) -> str:
        self.thread_ids.append(threading.get_ident())
        return self._labels.pop(0)


@pytest.mark.asyncio
async def test_streaming_assigns_multiple_speakers_on_shared_executor() -> None:
    emitted: list[list[dict]] = []
    clusterer = _ThreadRecordingClusterer(['SPEAKER_00', 'SPEAKER_01'])
    event_loop_thread = threading.get_ident()
    socket = SenseVoiceSocket(
        sample_rate=4,
        transcript_callback=emitted.append,
        recognizer=_Recognizer(['first', 'second']),
        window_seconds=1,
        poll_seconds=0.001,
        speaker_clusterer=clusterer,  # type: ignore[arg-type]
    )
    socket.start()
    assert socket.send(b'\x00\x00' * 8)
    await socket.drain_and_close()

    assert [batch[0]['speaker'] for batch in emitted] == ['SPEAKER_00', 'SPEAKER_01']
    assert clusterer.thread_ids
    assert all(thread_id != event_loop_thread for thread_id in clusterer.thread_ids)


@pytest.mark.asyncio
async def test_streaming_exposes_typed_speaker_failure_without_emitting_segment() -> None:
    class _FailingClusterer:
        def assign_pcm(self, _index: int, _pcm: bytes, _sample_rate: int) -> str:
            raise SenseVoiceSpeakerError('speaker_embedding_unavailable')

    emitted: list[list[dict]] = []
    socket = SenseVoiceSocket(
        sample_rate=4,
        transcript_callback=emitted.append,
        recognizer=_Recognizer(['text']),
        window_seconds=1,
        poll_seconds=0.001,
        speaker_clusterer=_FailingClusterer(),  # type: ignore[arg-type]
    )
    socket.start()
    assert socket.send(b'\x00\x00' * 4)
    await asyncio.sleep(0.02)
    await socket.drain_and_close()

    assert emitted == []
    assert socket.is_connection_dead
    assert socket.death_reason == 'sensevoice_speaker:speaker_embedding_unavailable'


def test_prerecorded_diarization_segments_and_labels_two_windows(monkeypatch) -> None:
    clusterer = _ThreadRecordingClusterer(['SPEAKER_00', 'SPEAKER_01'])
    monkeypatch.setenv('SENSEVOICE_SPEAKER_MODE', 'window_clustering')
    monkeypatch.setenv('SENSEVOICE_SPEAKER_WINDOW_SECONDS', '1')
    monkeypatch.setattr(prerecorded_module, 'build_window_speaker_clusterer', lambda **_kwargs: clusterer)
    provider = SenseVoicePrerecordedProvider(recognizer=_Recognizer(['first', 'second']))

    words = provider.transcribe_bytes(b'\x00\x00' * 8, sample_rate=4, encoding='linear16')

    assert words == [
        {'timestamp': [0.0, 1.0], 'speaker': 'SPEAKER_00', 'text': 'first'},
        {'timestamp': [1.0, 2.0], 'speaker': 'SPEAKER_01', 'text': 'second'},
    ]


def test_prerecorded_diarization_rejects_oversize_before_model_construction(monkeypatch) -> None:
    monkeypatch.setenv('SENSEVOICE_SPEAKER_MODE', 'window_clustering')
    monkeypatch.setenv('SENSEVOICE_SPEAKER_MAX_AUDIO_SECONDS', '1')
    monkeypatch.setattr(
        prerecorded_module,
        'build_window_speaker_clusterer',
        lambda **_kwargs: pytest.fail('model must not be constructed'),
    )
    provider = SenseVoicePrerecordedProvider(recognizer=_Recognizer(['unused']))

    with pytest.raises(SenseVoiceSpeakerError) as raised:
        provider.transcribe_bytes(b'\x00\x00' * 8, sample_rate=4, encoding='linear16')

    assert raised.value.reason == 'speaker_audio_too_large'
    assert raised.value.outcome.value == 'invalid_input'


def test_sensevoice_url_rejects_undeclared_audio_origin_before_download(monkeypatch) -> None:
    monkeypatch.setenv('OMI_DEPLOYMENT_PROFILE', 'self_hosted')
    monkeypatch.delenv('SELF_HOST_EGRESS_ALLOWLIST', raising=False)
    monkeypatch.setattr(prerecorded_module.httpx, 'get', lambda *_args, **_kwargs: pytest.fail('network call'))

    provider = SenseVoicePrerecordedProvider(recognizer=_Recognizer(['unused']))
    with pytest.raises(EgressPolicyUnavailable, match='egress_allowlist_not_configured'):
        provider.transcribe_url('https://audio.operator.example/recording.wav')
