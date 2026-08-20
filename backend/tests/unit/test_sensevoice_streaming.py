from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from utils.sensevoice.socket import SenseVoiceSocket, sensevoice_model_is_ready
from utils.stt import streaming


class _Stream:
    def __init__(self, text: str) -> None:
        self.result = SimpleNamespace(text=text)
        self.sample_rate = 0
        self.samples: list[int] = []

    def accept_waveform(self, sample_rate: int, samples: list[int]) -> None:
        self.sample_rate = sample_rate
        self.samples = samples


class _Recognizer:
    def __init__(self) -> None:
        self.created = 0
        self.streams: list[_Stream] = []

    def create_stream(self) -> _Stream:
        self.created += 1
        stream = _Stream(f'window-{self.created}')
        self.streams.append(stream)
        return stream

    def decode_stream(self, stream: _Stream) -> None:
        assert stream in self.streams


@pytest.mark.asyncio
async def test_sensevoice_emits_before_session_end_and_drains_the_tail() -> None:
    emitted: list[list[dict]] = []
    first_window = asyncio.Event()

    def callback(segments: list[dict]) -> None:
        emitted.append(segments)
        first_window.set()

    recognizer = _Recognizer()
    socket = SenseVoiceSocket(
        sample_rate=4,
        transcript_callback=callback,
        recognizer=recognizer,
        window_seconds=1,
        poll_seconds=0.001,
    )
    socket.start()

    assert socket.send(b'\x00\x00' * 4)
    await asyncio.wait_for(first_window.wait(), timeout=1)
    assert not socket.is_connection_dead
    assert emitted[0] == [
        {
            'speaker': 'SPEAKER_00',
            'start': 0.0,
            'end': 1.0,
            'text': 'window-1',
            'is_user': False,
            'person_id': None,
        }
    ]

    assert socket.send(b'\x00\x00' * 2)
    await socket.drain_and_close()
    assert emitted[1][0]['text'] == 'window-2'
    assert emitted[1][0]['start'] == 1.0
    assert emitted[1][0]['end'] == 1.5
    assert not socket.send(b'\x00\x00')


@pytest.mark.asyncio
async def test_sensevoice_finalize_flushes_a_vad_utterance_without_closing() -> None:
    emitted: list[list[dict]] = []
    flushed = asyncio.Event()

    def callback(segments: list[dict]) -> None:
        emitted.append(segments)
        flushed.set()

    socket = SenseVoiceSocket(
        sample_rate=4,
        transcript_callback=callback,
        recognizer=_Recognizer(),
        window_seconds=5,
        poll_seconds=0.001,
    )
    socket.start()
    assert socket.send(b'\x00\x00' * 2)
    socket.finalize()

    await asyncio.wait_for(flushed.wait(), timeout=1)
    assert emitted[0][0]['end'] == 0.5
    assert socket.send(b'\x00\x00' * 2)
    await socket.drain_and_close()
    assert len(emitted) == 2


def test_sensevoice_readiness_requires_model_and_tokens(tmp_path) -> None:
    (tmp_path / 'model.int8.onnx').write_bytes(b'model')
    assert not sensevoice_model_is_ready(str(tmp_path))
    (tmp_path / 'tokens.txt').write_text('tokens', encoding='utf-8')
    assert sensevoice_model_is_ready(str(tmp_path))


def test_self_host_route_selects_ready_sensevoice(monkeypatch) -> None:
    monkeypatch.setattr(streaming, 'stt_service_models', ['sensevoice'])
    monkeypatch.setattr(streaming, 'sensevoice_model_is_ready', lambda: True)

    service, language, model = streaming.get_stt_service_for_language('zh-CN', multi_lang_enabled=False)

    assert service == streaming.STTService.sensevoice
    assert language == 'zh'
    assert model == 'sensevoice'


def test_self_host_route_does_not_fall_through_to_a_managed_default(monkeypatch) -> None:
    monkeypatch.setattr(streaming, 'stt_service_models', ['sensevoice'])
    monkeypatch.setattr(streaming, 'sensevoice_model_is_ready', lambda: False)
    monkeypatch.setenv('STT_ROUTE_FALLBACK_TO_DEFAULT', 'false')

    assert streaming.get_stt_service_for_language('en') == (None, None, None)


@pytest.mark.asyncio
async def test_process_audio_sensevoice_fails_before_returning_a_socket(monkeypatch) -> None:
    def unavailable() -> None:
        raise ModuleNotFoundError('sherpa_onnx')

    monkeypatch.setattr(streaming, 'get_sensevoice_recognizer', unavailable)

    with pytest.raises(ModuleNotFoundError, match='sherpa_onnx'):
        await streaming.process_audio_sensevoice(lambda _segments: None, 16000)
