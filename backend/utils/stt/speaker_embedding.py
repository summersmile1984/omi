import io
import logging
import os
import struct
import threading
import wave
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np
import httpx
from scipy.spatial.distance import cdist

from utils.executors import storage_executor, sync_executor, run_blocking
from utils.egress_policy import assert_http_endpoint_allowed
from utils.http_client import get_stt_client

logger = logging.getLogger(__name__)

# Cosine distance threshold for enrolled-voiceprint verification only.
# Based on VoxCeleb 1 test set EER of 2.8%. In-session clustering has its own
# policy in speaker_clustering.py and must not silently retune this boundary.
SPEAKER_MATCH_THRESHOLD = 0.45

# Minimum audio duration (seconds) for speaker embedding extraction.
# Audio shorter than this crashes pyannote wespeaker fbank (see issue #4572).
MIN_EMBEDDING_AUDIO_DURATION = float(os.getenv("MIN_EMBEDDING_AUDIO_DURATION", "0.5"))
SPEAKER_EMBEDDING_PROVIDER_ENV = "SPEAKER_EMBEDDING_PROVIDER"
SPEAKER_EMBEDDING_MODEL_ENV = "SPEAKER_EMBEDDING_MODEL"
SPEAKER_EMBEDDING_NUM_THREADS_ENV = "SPEAKER_EMBEDDING_NUM_THREADS"
SPEAKER_EMBEDDING_SAMPLE_RATE = 16000

_local_extractor_lock = threading.Lock()
_local_extractor_inference_lock = threading.Lock()
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
        if not os.getenv("SPEAKER_EMBEDDING_API_URL", "").strip():
            raise SpeakerEmbeddingUnavailable("SPEAKER_EMBEDDING_API_URL is required for the http provider")
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


def _extract_local_embedding(audio_data: bytes) -> np.ndarray[Any, Any]:
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


def _get_wav_duration(audio_data: bytes) -> float:
    """Get duration in seconds from WAV bytes. Returns 0.0 on parse failure."""
    try:
        with wave.open(io.BytesIO(audio_data), "rb") as wf:
            framerate = wf.getframerate()
            if framerate <= 0:
                return 0.0
            return wf.getnframes() / framerate
    except (wave.Error, EOFError, struct.error):
        return 0.0


def _get_api_url() -> str:
    """Get the speaker embedding API URL from environment."""
    provider = speaker_embedding_provider()
    if provider == "disabled":
        raise SpeakerEmbeddingUnavailable("Speaker embedding is disabled for this deployment")
    if provider != "http":
        raise SpeakerEmbeddingUnavailable(f"Speaker embedding provider {provider} does not use an HTTP endpoint")
    url = os.getenv('SPEAKER_EMBEDDING_API_URL')
    if not url:
        raise SpeakerEmbeddingUnavailable("SPEAKER_EMBEDDING_API_URL is required for the http provider")
    return url


def extract_embedding(audio_path: str) -> np.ndarray[Any, Any]:
    """
    Extract speaker embedding from an audio file using hosted API.

    Args:
        audio_path: Path to audio file (wav format recommended)

    Returns:
        numpy array of shape (1, D) where D is embedding dimension
    """
    provider = validate_speaker_embedding_configuration()
    if provider == "sherpa_onnx":
        return _extract_local_embedding(_read_file(audio_path))
    api_url = _get_api_url()
    # The synchronous compatibility path cannot use the shared AsyncClient
    # hook. Apply the same deployment authority before handing the URL to
    # httpx, otherwise a neutral process can bypass its egress allowlist.
    assert_http_endpoint_allowed(api_url)

    with open(audio_path, 'rb') as f:
        files = {'file': (os.path.basename(audio_path), f, 'audio/wav')}
        response = httpx.post(f"{api_url}/v2/embedding", files=files, timeout=300.0)
        response.raise_for_status()

    result = response.json()

    # Handle both formats: direct array or {"embedding": [...]}
    return _validate_embedding(result if isinstance(result, list) else result['embedding'])


def extract_embedding_from_bytes(audio_data: bytes, filename: str = "audio.wav") -> np.ndarray[Any, Any]:
    """
    Extract speaker embedding from audio bytes using hosted API.

    Args:
        audio_data: Raw audio bytes (wav format)
        filename: Filename to use in the request

    Returns:
        numpy array of shape (1, D) where D is embedding dimension

    Raises:
        ValueError: If audio is too short for speaker embedding
    """
    duration = _get_wav_duration(audio_data)
    if duration < MIN_EMBEDDING_AUDIO_DURATION:
        raise ValueError(f"Audio too short for speaker embedding: {duration:.3f}s < {MIN_EMBEDDING_AUDIO_DURATION}s")

    provider = validate_speaker_embedding_configuration()
    if provider == "sherpa_onnx":
        return _extract_local_embedding(audio_data)
    api_url = _get_api_url()
    # Keep this sync path subject to the same pre-transport authority as the
    # async speaker-embedding client below.
    assert_http_endpoint_allowed(api_url)

    files = {'file': (filename, audio_data, 'audio/wav')}
    response = httpx.post(f"{api_url}/v2/embedding", files=files, timeout=300.0)
    response.raise_for_status()

    result = response.json()

    # Handle both formats: direct array or {"embedding": [...]}
    return _validate_embedding(result if isinstance(result, list) else result['embedding'])


def _read_file(path: str) -> bytes:
    with open(path, 'rb') as f:
        return f.read()


async def async_extract_embedding(audio_path: str) -> np.ndarray[Any, Any]:
    """Async version of extract_embedding using httpx.AsyncClient."""
    provider = validate_speaker_embedding_configuration()
    if provider == "sherpa_onnx":
        return await run_blocking(sync_executor, extract_embedding, audio_path)
    api_url = _get_api_url()
    client = get_stt_client()

    file_data = await run_blocking(storage_executor, _read_file, audio_path)

    files = {'file': (os.path.basename(audio_path), file_data, 'audio/wav')}
    try:
        response = await client.post(f"{api_url}/v2/embedding", files=files)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"async_extract_embedding failed for {audio_path}: {e}")
        raise

    result = response.json()
    return _validate_embedding(result if isinstance(result, list) else result['embedding'])


async def async_extract_embedding_from_bytes(audio_data: bytes, filename: str = "audio.wav") -> np.ndarray[Any, Any]:
    """Async version of extract_embedding_from_bytes using httpx.AsyncClient."""
    duration = _get_wav_duration(audio_data)
    if duration < MIN_EMBEDDING_AUDIO_DURATION:
        raise ValueError(f"Audio too short for speaker embedding: {duration:.3f}s < {MIN_EMBEDDING_AUDIO_DURATION}s")

    provider = validate_speaker_embedding_configuration()
    if provider == "sherpa_onnx":
        return await run_blocking(sync_executor, _extract_local_embedding, audio_data)
    api_url = _get_api_url()
    client = get_stt_client()

    files = {'file': (filename, audio_data, 'audio/wav')}
    try:
        response = await client.post(f"{api_url}/v2/embedding", files=files)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"async_extract_embedding_from_bytes failed: {e}")
        raise

    result = response.json()
    return _validate_embedding(result if isinstance(result, list) else result['embedding'])


def compare_embeddings(embedding1: np.ndarray[Any, Any], embedding2: np.ndarray[Any, Any]) -> float:
    """
    Compare two speaker embeddings using cosine distance.

    Args:
        embedding1: First embedding array (1, D)
        embedding2: Second embedding array (1, D)

    Returns:
        Cosine distance (0.0 = identical, 2.0 = opposite)
        Lower values indicate more similar speakers.
        Returns 2.0 (max distance) if embeddings have different dimensions.
    """
    if embedding1.shape[1] != embedding2.shape[1]:
        return 2.0
    distance = cdist(embedding1, embedding2, metric="cosine")[0, 0]
    return float(distance)


def is_same_speaker(
    embedding1: np.ndarray[Any, Any], embedding2: np.ndarray[Any, Any], threshold: float = SPEAKER_MATCH_THRESHOLD
) -> Tuple[bool, float]:
    """
    Determine if two embeddings belong to the same speaker.

    Args:
        embedding1: First embedding array
        embedding2: Second embedding array
        threshold: Cosine distance threshold for matching

    Returns:
        Tuple of (is_match, distance)
    """
    distance = compare_embeddings(embedding1, embedding2)
    return distance < threshold, distance


def embedding_to_bytes(embedding: np.ndarray[Any, Any]) -> bytes:
    """
    Serialize embedding to bytes for storage.

    Args:
        embedding: numpy array embedding

    Returns:
        Bytes representation of the embedding
    """
    return embedding.astype(np.float32).tobytes()


def bytes_to_embedding(data: bytes, dim: int = 512) -> np.ndarray[Any, Any]:
    """
    Deserialize embedding from bytes.

    Args:
        data: Bytes representation of embedding
        dim: Embedding dimension (default 512 for pyannote/embedding)

    Returns:
        numpy array of shape (1, D)
    """
    embedding = np.frombuffer(data, dtype=np.float32)
    return embedding.reshape(1, -1)


def find_best_match(
    query_embedding: np.ndarray[Any, Any],
    candidate_embeddings: List[np.ndarray[Any, Any]],
    threshold: float = SPEAKER_MATCH_THRESHOLD,
) -> Optional[Tuple[int, float]]:
    """
    Find the best matching speaker from a list of candidates.

    Args:
        query_embedding: Embedding to match
        candidate_embeddings: List of candidate embeddings
        threshold: Maximum distance for a valid match

    Returns:
        Tuple of (best_index, distance) or None if no match found
    """
    if not candidate_embeddings:
        return None

    best_idx = -1
    best_distance = float('inf')

    for idx, candidate in enumerate(candidate_embeddings):
        distance = compare_embeddings(query_embedding, candidate)
        if distance < best_distance:
            best_distance = distance
            best_idx = idx

    if best_distance < threshold:
        return best_idx, best_distance

    return None
