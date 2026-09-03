"""Fork-owned speaker-embedding support for operator-run deployments.

Upstream extracts embeddings through a hosted API. A self-hosted install has no
such endpoint, so the fork adds a local sherpa_onnx path and a typed
``SpeakerEmbeddingUnavailable`` for callers that must fail closed rather than
silently skip diarization.

These definitions lived inside upstream's ``utils/stt/speaker_embedding.py`` on
the old shim branch; moving them here leaves that file byte-identical.
"""

from __future__ import annotations

import io
import os
import threading
import wave
from pathlib import Path
from typing import Any, Optional, Tuple

import numpy as np

SPEAKER_EMBEDDING_PROVIDER_ENV = "SPEAKER_EMBEDDING_PROVIDER"


SPEAKER_EMBEDDING_MODEL_ENV = "SPEAKER_EMBEDDING_MODEL"


SPEAKER_EMBEDDING_NUM_THREADS_ENV = "SPEAKER_EMBEDDING_NUM_THREADS"


SPEAKER_EMBEDDING_SAMPLE_RATE = 16000


_local_extractor_lock = threading.Lock()


_local_extractor_inference_lock = threading.Lock()

# Cached sherpa-onnx extractor plus the (model path, thread count) it was
# built for, so a changed model path rebuilds instead of serving stale vectors.
_local_extractor_identity: Optional[Tuple[str, int]] = None
_local_extractor: Any = None


class SpeakerEmbeddingUnavailable(RuntimeError):
    """The selected deployment intentionally has no speaker diarization provider."""


def speaker_embedding_provider() -> str:
    """Return the explicit speaker-embedding provider selection.

    ``http`` selects any operator-controlled implementation of the
    ``POST /v2/embedding`` contract. ``sherpa_onnx`` runs a mounted
    speaker-recognition ONNX model in this process; the model path is explicit
    and this module never downloads one. ``disabled`` is a deliberate
    capability boundary: ordinary transcription remains available, but a
    caller that requests speaker embeddings fails before constructing an HTTP
    request. Unknown values also fail closed.
    """

    provider = os.getenv(SPEAKER_EMBEDDING_PROVIDER_ENV, "http").strip().lower()
    if provider not in {"http", "sherpa_onnx", "disabled"}:
        raise SpeakerEmbeddingUnavailable(f"Unsupported speaker embedding provider: {provider or '<empty>'}")
    return provider


def _positive_thread_count() -> int:
    raw = os.getenv(SPEAKER_EMBEDDING_NUM_THREADS_ENV, "2").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SpeakerEmbeddingUnavailable(f"{SPEAKER_EMBEDDING_NUM_THREADS_ENV} must be a positive integer") from exc
    if value <= 0 or value > 64:
        raise SpeakerEmbeddingUnavailable(f"{SPEAKER_EMBEDDING_NUM_THREADS_ENV} must be between 1 and 64")
    return value


def _local_model_identity() -> Tuple[str, int]:
    raw_path = os.getenv(SPEAKER_EMBEDDING_MODEL_ENV, "").strip()
    if not raw_path:
        raise SpeakerEmbeddingUnavailable(f"{SPEAKER_EMBEDDING_MODEL_ENV} is required for sherpa_onnx")
    model_path = Path(raw_path).expanduser().resolve()
    if not model_path.is_file():
        raise SpeakerEmbeddingUnavailable(f"{SPEAKER_EMBEDDING_MODEL_ENV} does not name a readable file")
    return str(model_path), _positive_thread_count()


def validate_speaker_embedding_configuration() -> str:
    """Validate the selected boundary without constructing a network client."""

    provider = speaker_embedding_provider()
    if provider == "disabled":
        raise SpeakerEmbeddingUnavailable("Speaker embedding is disabled for this deployment")
    if provider == "http":
        if not os.getenv("HOSTED_SPEAKER_EMBEDDING_API_URL", "").strip():
            raise SpeakerEmbeddingUnavailable("HOSTED_SPEAKER_EMBEDDING_API_URL is required for the http provider")
        return provider
    _local_model_identity()
    return provider


def _get_local_extractor() -> Any:
    global _local_extractor, _local_extractor_identity

    identity = _local_model_identity()
    with _local_extractor_lock:
        if _local_extractor is not None and _local_extractor_identity == identity:
            return _local_extractor
        try:
            import sherpa_onnx  # type: ignore[reportMissingImports]
        except ImportError as exc:
            raise SpeakerEmbeddingUnavailable("sherpa-onnx runtime is unavailable") from exc

        config: Any = sherpa_onnx.SpeakerEmbeddingExtractorConfig(  # type: ignore[reportUnknownMemberType]
            model=identity[0],
            num_threads=identity[1],
            debug=False,
            provider="cpu",
        )
        if not config.validate():
            raise SpeakerEmbeddingUnavailable("The mounted speaker embedding model is not compatible with sherpa-onnx")
        try:
            extractor: Any = sherpa_onnx.SpeakerEmbeddingExtractor(config)  # type: ignore[reportUnknownMemberType]
        except Exception as exc:
            raise SpeakerEmbeddingUnavailable("Unable to initialize the mounted speaker embedding model") from exc
        _local_extractor = extractor
        _local_extractor_identity = identity
        return extractor


def _wav_to_float_samples(audio_data: bytes) -> np.ndarray[Any, Any]:
    """Decode bounded PCM16 WAV bytes to 16 kHz mono float32 samples."""

    try:
        with wave.open(io.BytesIO(audio_data), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE" or wav_file.getsampwidth() != 2:
                raise SpeakerEmbeddingUnavailable("Speaker embedding requires uncompressed PCM16 WAV audio")
            sample_rate = wav_file.getframerate()
            channels = wav_file.getnchannels()
            frames = wav_file.getnframes()
            if sample_rate <= 0 or channels <= 0 or frames <= 0:
                raise SpeakerEmbeddingUnavailable("Speaker embedding WAV metadata is invalid")
            pcm = wav_file.readframes(frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise SpeakerEmbeddingUnavailable("Speaker embedding input is not a valid WAV file") from exc

    samples = np.frombuffer(pcm, dtype="<i2")
    if samples.size != frames * channels:
        raise SpeakerEmbeddingUnavailable("Speaker embedding WAV payload is truncated")
    if channels > 1:
        samples = samples.reshape(-1, channels).astype(np.float32).mean(axis=1)
    else:
        samples = samples.astype(np.float32)
    samples /= 32768.0

    if sample_rate != SPEAKER_EMBEDDING_SAMPLE_RATE:
        output_size = int(round(samples.size * SPEAKER_EMBEDDING_SAMPLE_RATE / sample_rate))
        if output_size <= 0:
            raise SpeakerEmbeddingUnavailable("Speaker embedding audio is empty after resampling")
        source_positions = np.arange(samples.size, dtype=np.float64)
        target_positions = np.arange(output_size, dtype=np.float64) * sample_rate / SPEAKER_EMBEDDING_SAMPLE_RATE
        samples = np.interp(target_positions, source_positions, samples).astype(np.float32)
    return samples


def _validate_embedding(values: Any) -> np.ndarray[Any, Any]:
    embedding = np.asarray(values, dtype=np.float32).reshape(-1)
    if embedding.size == 0 or not np.all(np.isfinite(embedding)):
        raise SpeakerEmbeddingUnavailable("Speaker embedding provider returned an invalid vector")
    norm = float(np.linalg.norm(embedding.astype(np.float64)))
    if not np.isfinite(norm) or norm <= 0:
        raise SpeakerEmbeddingUnavailable("Speaker embedding provider returned a zero vector")
    return (embedding / norm).reshape(1, -1)


def extract_local_embedding(audio_data: bytes) -> np.ndarray[Any, Any]:
    extractor = _get_local_extractor()
    samples = _wav_to_float_samples(audio_data)
    # One process-wide extractor is shared by live and prerecorded sessions.
    # sherpa-onnx does not promise that a single extractor instance is safe for
    # concurrent create/compute calls, so serialize only its CPU inference.
    with _local_extractor_inference_lock:
        stream: Any = extractor.create_stream()
        stream.accept_waveform(sample_rate=SPEAKER_EMBEDDING_SAMPLE_RATE, waveform=samples)
        stream.input_finished()
        if not extractor.is_ready(stream):
            raise SpeakerEmbeddingUnavailable("Audio is too short for the mounted speaker embedding model")
        try:
            return _validate_embedding(extractor.compute(stream))
        except SpeakerEmbeddingUnavailable:
            raise
        except Exception as exc:
            raise SpeakerEmbeddingUnavailable("The mounted speaker embedding model failed to process audio") from exc
