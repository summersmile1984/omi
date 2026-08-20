from __future__ import annotations

import io
import wave
from types import SimpleNamespace

import pytest

from config.prerecorded_stt import (
    PrerecordedSTTConfigurationError,
    PrerecordedSTTService,
    providers_for_model_config,
    require_provider_environment,
)
from config.stt_provider_policy import SENSEVOICE_PROVIDER, STTServingSurface, provider_is_enabled
from utils.mimo_pipeline.prerecorded_provider import MimoPrerecordedProvider
from utils.moss_pipeline.prerecorded_provider import MossPrerecordedProvider
from utils.sensevoice.prerecorded_provider import SenseVoicePrerecordedProvider


class _SenseVoiceStream:
    def __init__(self) -> None:
        self.result = SimpleNamespace(text=' 本地转写 ')
        self.sample_rate = 0
        self.samples = []

    def accept_waveform(self, sample_rate, samples):
        self.sample_rate = sample_rate
        self.samples = samples


class _SenseVoiceRecognizer:
    def __init__(self) -> None:
        self.stream = _SenseVoiceStream()

    def create_stream(self):
        return self.stream

    def decode_stream(self, stream):
        assert stream is self.stream


def test_sensevoice_keeps_its_prerecorded_adapter_and_emits_batch_shape():
    assert provider_is_enabled(SENSEVOICE_PROVIDER, STTServingSurface.PRERECORDED)
    assert provider_is_enabled(SENSEVOICE_PROVIDER, STTServingSurface.STREAMING)
    recognizer = _SenseVoiceRecognizer()
    provider = SenseVoicePrerecordedProvider(recognizer=recognizer)

    words = provider.transcribe_bytes(b'\x00\x00' * 16000, encoding='linear16')

    assert words == [{'timestamp': [0.0, 1.0], 'speaker': 'SPEAKER_00', 'text': '本地转写'}]
    assert recognizer.stream.sample_rate == 16000


def test_sensevoice_decodes_container_audio_to_typed_mono_pcm():
    source = io.BytesIO()
    with wave.open(source, 'wb') as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(b'\x00\x00\x00\x00' * 8000)

    recognizer = _SenseVoiceRecognizer()
    provider = SenseVoicePrerecordedProvider(recognizer=recognizer)
    words = provider.transcribe_bytes(source.getvalue(), encoding='audio/wav')

    assert recognizer.stream.sample_rate == 16000
    assert 15990 <= len(recognizer.stream.samples) <= 16000
    assert words[0]['timestamp'][1] == pytest.approx(1.0, abs=0.001)


def test_mimo_raw_pcm_is_wrapped_as_wav_and_shaped():
    captured = {}

    class _Client:
        def transcribe_audio(self, audio, **kwargs):
            captured['audio'] = audio
            captured['kwargs'] = kwargs
            return SimpleNamespace(text='云端批量转写', duration=0.0)

    provider = MimoPrerecordedProvider(client=_Client())
    words = provider.transcribe_bytes(
        b'\x00\x00' * 8000,
        sample_rate=16000,
        channels=1,
        encoding='linear16',
        language='zh',
    )

    assert captured['audio'][:4] == b'RIFF'
    assert captured['kwargs'] == {'audio_format': 'wav', 'language': 'zh'}
    assert words == [{'timestamp': [0.0, 0.5], 'speaker': 'SPEAKER_00', 'text': '云端批量转写'}]


def test_moss_wraps_raw_pcm_in_a_real_wav_before_upload():
    captured = {}

    class _Client:
        def upload_file(self, path):
            with open(path, 'rb') as source:
                captured['audio'] = source.read()
            return 'file-1'

        def transcribe(self, **kwargs):
            return SimpleNamespace(text='ok', segments=[])

        def delete_file(self, file_id):
            captured['deleted'] = file_id

    provider = MossPrerecordedProvider(client=_Client())
    words = provider.transcribe_bytes(
        b'\x00\x00' * 8000,
        sample_rate=16000,
        channels=1,
        encoding='linear16',
    )

    with wave.open(io.BytesIO(captured['audio']), 'rb') as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getnframes() == 8000
    assert captured['deleted'] == 'file-1'
    assert words == [{'timestamp': [0.0, 0.0], 'speaker': 'SPEAKER_00', 'text': 'ok'}]


def test_moss_rejects_unlabelled_compressed_bytes_without_uploading():
    provider = MossPrerecordedProvider(client=SimpleNamespace())
    with pytest.raises(ValueError, match='requires WAV or raw PCM16'):
        provider.transcribe_bytes(b'not-a-wav')


def test_explicit_batch_provider_contract_does_not_require_hidden_managed_fallbacks():
    assert providers_for_model_config('sensevoice') == (PrerecordedSTTService.SENSEVOICE,)
    assert providers_for_model_config('mimo') == (PrerecordedSTTService.MIMO,)


def test_mimo_selection_requires_explicit_endpoint_and_key(monkeypatch):
    monkeypatch.setenv('MIMO_API_KEY', 'key')
    monkeypatch.delenv('MIMO_API_BASE', raising=False)
    with pytest.raises(PrerecordedSTTConfigurationError, match='MIMO_API_BASE'):
        require_provider_environment(PrerecordedSTTService.MIMO)
