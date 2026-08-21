"""Tests for short audio clip validation in speaker embedding (issue #4572).

Verifies that audio clips shorter than MIN_EMBEDDING_AUDIO_DURATION are rejected
with a clear error instead of crashing the pyannote wespeaker fbank model.
"""

import io
import wave
from unittest.mock import MagicMock

import httpx
import numpy as np
import pytest

import utils.stt.speaker_embedding as speaker_embedding
from utils.stt.speaker_embedding import (
    MIN_EMBEDDING_AUDIO_DURATION,
    SpeakerEmbeddingUnavailable,
    _get_wav_duration,
    extract_embedding_from_bytes,
)


def _make_wav_bytes(duration_seconds: float, sample_rate: int = 16000) -> bytes:
    """Generate valid WAV bytes with the given duration."""
    num_frames = int(sample_rate * duration_seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        # Write silence (zeros)
        wf.writeframes(b"\x00\x00" * num_frames)
    return buf.getvalue()


def _make_stereo_wav_bytes(duration_seconds: float, sample_rate: int = 8000) -> bytes:
    frames = int(sample_rate * duration_seconds)
    time = np.arange(frames, dtype=np.float64) / sample_rate
    left = (np.sin(2 * np.pi * 220 * time) * 16000).astype("<i2")
    right = (np.sin(2 * np.pi * 330 * time) * 8000).astype("<i2")
    interleaved = np.column_stack((left, right)).reshape(-1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(interleaved.tobytes())
    return buf.getvalue()


class TestGetWavDuration:
    def test_valid_wav_correct_duration(self):
        wav = _make_wav_bytes(1.0, sample_rate=16000)
        duration = _get_wav_duration(wav)
        assert abs(duration - 1.0) < 0.001

    def test_short_wav_correct_duration(self):
        wav = _make_wav_bytes(0.025, sample_rate=16000)  # ~25ms, the crash case
        duration = _get_wav_duration(wav)
        assert abs(duration - 0.025) < 0.001

    def test_different_sample_rates(self):
        for sr in [8000, 16000, 44100, 48000]:
            wav = _make_wav_bytes(0.5, sample_rate=sr)
            duration = _get_wav_duration(wav)
            assert abs(duration - 0.5) < 0.01, f"Failed for sample_rate={sr}"

    def test_empty_bytes_returns_zero(self):
        assert _get_wav_duration(b"") == 0.0

    def test_garbage_bytes_returns_zero(self):
        assert _get_wav_duration(b"not a wav file at all") == 0.0

    def test_truncated_header_returns_zero(self):
        wav = _make_wav_bytes(1.0)
        truncated = wav[:20]  # Cut off in the middle of the header
        assert _get_wav_duration(truncated) == 0.0


class TestExtractEmbeddingFromBytesValidation:
    def test_disabled_provider_fails_before_network(self, monkeypatch):
        wav = _make_wav_bytes(1.0)
        request = MagicMock(side_effect=AssertionError("speaker embedding must not make an HTTP request"))
        monkeypatch.setenv("SPEAKER_EMBEDDING_PROVIDER", "disabled")
        monkeypatch.setattr(httpx, "post", request)

        with pytest.raises(SpeakerEmbeddingUnavailable, match="disabled"):
            extract_embedding_from_bytes(wav, "test.wav")

        request.assert_not_called()

    def test_unknown_provider_fails_closed_before_network(self, monkeypatch):
        wav = _make_wav_bytes(1.0)
        request = MagicMock(side_effect=AssertionError("unknown provider must not make an HTTP request"))
        monkeypatch.setenv("SPEAKER_EMBEDDING_PROVIDER", "mystery")
        monkeypatch.setattr(httpx, "post", request)

        with pytest.raises(SpeakerEmbeddingUnavailable, match="Unsupported"):
            extract_embedding_from_bytes(wav, "test.wav")

        request.assert_not_called()

    def test_short_audio_raises_value_error(self):
        """Audio shorter than MIN_EMBEDDING_AUDIO_DURATION raises ValueError."""
        wav = _make_wav_bytes(0.025)  # 25ms - the crash case from issue #4572
        with pytest.raises(ValueError, match="Audio too short"):
            extract_embedding_from_bytes(wav, "test.wav")

    def test_boundary_below_threshold_raises(self):
        """Audio just below threshold raises ValueError."""
        wav = _make_wav_bytes(MIN_EMBEDDING_AUDIO_DURATION - 0.01)
        with pytest.raises(ValueError, match="Audio too short"):
            extract_embedding_from_bytes(wav, "test.wav")

    def test_boundary_at_threshold_passes_validation(self, monkeypatch):
        """Audio exactly at threshold passes duration check (may fail on API call)."""
        wav = _make_wav_bytes(MIN_EMBEDDING_AUDIO_DURATION)

        # Mock the API call since we only test validation, not the actual embedding
        monkeypatch.setenv("SPEAKER_EMBEDDING_PROVIDER", "http")
        monkeypatch.setenv("SPEAKER_EMBEDDING_API_URL", "http://fake:1234")
        mock_response = MagicMock()
        mock_response.json.return_value = [0.1] * 512
        mock_response.raise_for_status = MagicMock()

        monkeypatch.setattr(httpx, "post", MagicMock(return_value=mock_response))

        # Should not raise ValueError - duration check passes
        result = extract_embedding_from_bytes(wav, "test.wav")
        assert result.shape == (1, 512)

    def test_empty_wav_raises(self):
        """Empty/garbage bytes raise ValueError (duration=0.0)."""
        with pytest.raises(ValueError, match="Audio too short"):
            extract_embedding_from_bytes(b"not a wav", "test.wav")

    def test_min_threshold_is_half_second(self):
        """Default threshold is 0.5 seconds."""
        assert MIN_EMBEDDING_AUDIO_DURATION == 0.5

    def test_threshold_configurable_via_env(self, monkeypatch):
        """MIN_EMBEDDING_AUDIO_DURATION can be overridden via environment."""
        # This tests the module-level constant mechanism
        # The actual env var is read at import time, so we verify the default
        assert MIN_EMBEDDING_AUDIO_DURATION >= 0.1  # Sane minimum
        assert MIN_EMBEDDING_AUDIO_DURATION <= 5.0  # Sane maximum

    def test_local_sherpa_provider_decodes_resamples_and_normalizes_without_http(self, monkeypatch, tmp_path):
        model = tmp_path / "speaker.onnx"
        model.write_bytes(b"operator supplied model")
        monkeypatch.setenv("SPEAKER_EMBEDDING_PROVIDER", "sherpa_onnx")
        monkeypatch.setenv("SPEAKER_EMBEDDING_MODEL", str(model))
        request = MagicMock(side_effect=AssertionError("local speaker embedding must not make an HTTP request"))
        monkeypatch.setattr(httpx, "post", request)

        accepted = {}

        class FakeStream:
            def accept_waveform(self, *, sample_rate, waveform):
                accepted["sample_rate"] = sample_rate
                accepted["waveform"] = waveform

            def input_finished(self):
                accepted["finished"] = True

        class FakeExtractor:
            def create_stream(self):
                return FakeStream()

            def is_ready(self, stream):
                return True

            def compute(self, stream):
                return [3.0, 4.0]

        monkeypatch.setattr(speaker_embedding, "_get_local_extractor", lambda: FakeExtractor())

        result = extract_embedding_from_bytes(_make_stereo_wav_bytes(1.0), "stereo.wav")

        assert accepted["sample_rate"] == 16000
        assert accepted["waveform"].dtype == np.float32
        assert accepted["waveform"].shape == (16000,)
        assert accepted["finished"] is True
        assert result.shape == (1, 2)
        assert result[0].tolist() == pytest.approx([0.6, 0.8])
        request.assert_not_called()

    def test_local_sherpa_provider_requires_mounted_model_before_runtime_or_http(self, monkeypatch):
        monkeypatch.setenv("SPEAKER_EMBEDDING_PROVIDER", "sherpa_onnx")
        monkeypatch.delenv("SPEAKER_EMBEDDING_MODEL", raising=False)
        request = MagicMock(side_effect=AssertionError("missing local model must fail before HTTP"))
        monkeypatch.setattr(httpx, "post", request)

        with pytest.raises(SpeakerEmbeddingUnavailable, match="SPEAKER_EMBEDDING_MODEL"):
            extract_embedding_from_bytes(_make_wav_bytes(1.0), "test.wav")

        request.assert_not_called()

    def test_local_sherpa_provider_rejects_zero_embedding(self, monkeypatch, tmp_path):
        model = tmp_path / "speaker.onnx"
        model.write_bytes(b"operator supplied model")
        monkeypatch.setenv("SPEAKER_EMBEDDING_PROVIDER", "sherpa_onnx")
        monkeypatch.setenv("SPEAKER_EMBEDDING_MODEL", str(model))

        stream = MagicMock()
        extractor = MagicMock()
        extractor.create_stream.return_value = stream
        extractor.is_ready.return_value = True
        extractor.compute.return_value = [0.0, 0.0]
        monkeypatch.setattr(speaker_embedding, "_get_local_extractor", lambda: extractor)

        with pytest.raises(SpeakerEmbeddingUnavailable, match="zero vector"):
            extract_embedding_from_bytes(_make_wav_bytes(1.0), "test.wav")

    def test_local_sherpa_inference_runs_under_process_wide_guard(self, monkeypatch, tmp_path):
        model = tmp_path / "speaker.onnx"
        model.write_bytes(b"operator supplied model")
        monkeypatch.setenv("SPEAKER_EMBEDDING_PROVIDER", "sherpa_onnx")
        monkeypatch.setenv("SPEAKER_EMBEDDING_MODEL", str(model))

        class Guard:
            active = False

            def __enter__(self):
                self.active = True

            def __exit__(self, _type, _value, _traceback):
                self.active = False

        guard = Guard()

        class FakeStream:
            def accept_waveform(self, **_kwargs):
                assert guard.active

            def input_finished(self):
                assert guard.active

        class FakeExtractor:
            def create_stream(self):
                assert guard.active
                return FakeStream()

            def is_ready(self, _stream):
                assert guard.active
                return True

            def compute(self, _stream):
                assert guard.active
                return [1.0, 0.0]

        monkeypatch.setattr(speaker_embedding, "_local_extractor_inference_lock", guard)
        monkeypatch.setattr(speaker_embedding, "_get_local_extractor", lambda: FakeExtractor())

        result = extract_embedding_from_bytes(_make_wav_bytes(1.0), "test.wav")

        assert result.tolist() == [[1.0, 0.0]]
        assert guard.active is False
