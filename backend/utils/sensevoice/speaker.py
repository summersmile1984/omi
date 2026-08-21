"""Bounded, local speaker attribution for SenseVoice transcript windows.

SenseVoice does not emit speaker turns.  A self-hosted deployment that already
selects the mounted ``sherpa_onnx`` speaker-embedding provider can label each
ASR/VAD window by clustering its local embedding.  This is deliberately a
window-level contract: it never claims to find a speaker change inside one ASR
window, and it never downloads or calls an HTTP embedding service.
"""

from __future__ import annotations

import io
import math
import os
import wave
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from config.prerecorded_stt import TranscriptionOutcome
from utils.observability.fallback import record_fallback
from utils.stt.outcomes import TranscriptionFailure
from utils.stt.speaker_embedding import (
    MIN_EMBEDDING_AUDIO_DURATION,
    SpeakerEmbeddingUnavailable,
    compare_embeddings,
    extract_embedding_from_bytes,
    speaker_embedding_provider,
    validate_speaker_embedding_configuration,
)

SENSEVOICE_SPEAKER_MODE_ENV = 'SENSEVOICE_SPEAKER_MODE'
SENSEVOICE_SPEAKER_THRESHOLD_ENV = 'SENSEVOICE_SPEAKER_CLUSTER_THRESHOLD'
SENSEVOICE_SPEAKER_MAX_CLUSTERS_ENV = 'SENSEVOICE_SPEAKER_MAX_CLUSTERS'
SENSEVOICE_SPEAKER_WINDOW_SECONDS_ENV = 'SENSEVOICE_SPEAKER_WINDOW_SECONDS'
SENSEVOICE_SPEAKER_MAX_AUDIO_SECONDS_ENV = 'SENSEVOICE_SPEAKER_MAX_AUDIO_SECONDS'

_DEFAULT_THRESHOLD = 0.45
_DEFAULT_MAX_CLUSTERS = 8
_DEFAULT_WINDOW_SECONDS = 5.0
_DEFAULT_MAX_AUDIO_SECONDS = 21600.0
_MAX_CLUSTER_LIMIT = 32
_MAX_WINDOW_SECONDS = 30.0
_MAX_AUDIO_SECONDS = 86400.0

EmbeddingExtractor = Callable[[bytes], np.ndarray]


class SenseVoiceSpeakerError(TranscriptionFailure):
    """Typed, privacy-safe terminal failure for local speaker attribution."""

    def __init__(self, reason: str, *, invalid_input: bool = False) -> None:
        self.reason = reason
        super().__init__(
            TranscriptionOutcome.INVALID_INPUT if invalid_input else TranscriptionOutcome.CONFIG_ERROR,
            provider='sensevoice',
            retryable=False,
        )


def _bounded_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise SenseVoiceSpeakerError('invalid_speaker_configuration') from exc
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise SenseVoiceSpeakerError('invalid_speaker_configuration')
    return value


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SenseVoiceSpeakerError('invalid_speaker_configuration') from exc
    if value < minimum or value > maximum:
        raise SenseVoiceSpeakerError('invalid_speaker_configuration')
    return value


def sensevoice_speaker_mode() -> str:
    """Resolve the explicit speaker capability without constructing a client.

    ``auto`` activates local window clustering only when the deployment has
    already selected ``SPEAKER_EMBEDDING_PROVIDER=sherpa_onnx``.  Managed HTTP
    embeddings are never borrowed by this path; every other provider retains
    the historical, explicit single-speaker capability.
    """

    raw = os.getenv(SENSEVOICE_SPEAKER_MODE_ENV, 'auto').strip().lower()
    if raw == 'auto':
        try:
            return 'window_clustering' if speaker_embedding_provider() == 'sherpa_onnx' else 'single_speaker'
        except SpeakerEmbeddingUnavailable as exc:
            raise SenseVoiceSpeakerError('invalid_speaker_configuration') from exc
    if raw not in {'single_speaker', 'window_clustering'}:
        raise SenseVoiceSpeakerError('unsupported_speaker_mode')
    return raw


def speaker_window_seconds() -> float:
    return _bounded_float(
        SENSEVOICE_SPEAKER_WINDOW_SECONDS_ENV,
        _DEFAULT_WINDOW_SECONDS,
        minimum=MIN_EMBEDDING_AUDIO_DURATION,
        maximum=_MAX_WINDOW_SECONDS,
    )


def validate_prerecorded_audio_bound(pcm_bytes: int, sample_rate: int) -> None:
    if sample_rate <= 0 or pcm_bytes < 0 or pcm_bytes % 2:
        raise SenseVoiceSpeakerError('invalid_speaker_audio', invalid_input=True)
    duration = pcm_bytes / (2 * sample_rate)
    maximum = _bounded_float(
        SENSEVOICE_SPEAKER_MAX_AUDIO_SECONDS_ENV,
        _DEFAULT_MAX_AUDIO_SECONDS,
        minimum=1.0,
        maximum=_MAX_AUDIO_SECONDS,
    )
    if duration > maximum:
        raise SenseVoiceSpeakerError('speaker_audio_too_large', invalid_input=True)


def _pcm16_wav(pcm: bytes, sample_rate: int) -> bytes:
    if sample_rate <= 0 or len(pcm) % 2:
        raise SenseVoiceSpeakerError('invalid_speaker_audio', invalid_input=True)
    minimum_bytes = math.ceil(MIN_EMBEDDING_AUDIO_DURATION * sample_rate) * 2
    padded = pcm if len(pcm) >= minimum_bytes else pcm + bytes(minimum_bytes - len(pcm))
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(padded)
    return output.getvalue()


def _normalized_embedding(raw: np.ndarray) -> np.ndarray:
    value = np.asarray(raw, dtype=np.float32).reshape(1, -1)
    if value.shape[1] == 0 or not np.all(np.isfinite(value)):
        raise SenseVoiceSpeakerError('invalid_speaker_embedding')
    norm = float(np.linalg.norm(value.astype(np.float64)))
    if not math.isfinite(norm) or norm <= 0:
        raise SenseVoiceSpeakerError('invalid_speaker_embedding')
    return value / norm


@dataclass
class _Cluster:
    centroid: np.ndarray
    observations: int


class WindowSpeakerClusterer:
    """Assign stable speaker ids to monotonically ordered ASR windows."""

    def __init__(
        self,
        *,
        threshold: float,
        max_clusters: int,
        embedding_extractor: EmbeddingExtractor,
    ) -> None:
        if not math.isfinite(threshold) or threshold <= 0 or threshold > 2:
            raise SenseVoiceSpeakerError('invalid_speaker_configuration')
        if max_clusters <= 0 or max_clusters > _MAX_CLUSTER_LIMIT:
            raise SenseVoiceSpeakerError('invalid_speaker_configuration')
        self._threshold = threshold
        self._max_clusters = max_clusters
        self._extract = embedding_extractor
        self._clusters: list[_Cluster] = []
        self._next_window_index = 0

    def assign_pcm(self, window_index: int, pcm: bytes, sample_rate: int) -> str:
        """Extract and assign one window; callers run this on ``sync_executor``."""

        try:
            embedding = self._extract(_pcm16_wav(pcm, sample_rate))
        except SenseVoiceSpeakerError:
            raise
        except (SpeakerEmbeddingUnavailable, ValueError, TypeError) as exc:
            raise SenseVoiceSpeakerError('speaker_embedding_unavailable') from exc
        except Exception as exc:
            raise SenseVoiceSpeakerError('speaker_embedding_failed') from exc
        return self.assign_embedding(window_index, embedding)

    def assign_embedding(self, window_index: int, embedding: np.ndarray) -> str:
        """Cluster one embedding, rejecting duplicate or out-of-order results."""

        if window_index != self._next_window_index:
            raise SenseVoiceSpeakerError('speaker_window_out_of_order')
        value = _normalized_embedding(embedding)

        best_index: Optional[int] = None
        best_distance = math.inf
        for index, cluster in enumerate(self._clusters):
            distance = compare_embeddings(value, cluster.centroid)
            if math.isfinite(distance) and distance < best_distance:
                best_index, best_distance = index, distance

        if best_index is None or best_distance > self._threshold:
            if len(self._clusters) >= self._max_clusters:
                if best_index is None:
                    raise SenseVoiceSpeakerError('invalid_speaker_embedding')
                # The upper bound is a resource/identity cap, not a reason to
                # terminate transcription. Preserve the nearest stable id but
                # do not contaminate its centroid with a low-confidence sample.
                record_fallback(
                    component='stt_selection',
                    from_mode='new_speaker_cluster',
                    to_mode='nearest_speaker_cluster',
                    reason='capacity_full',
                    outcome='degraded',
                )
                self._next_window_index += 1
                return f'SPEAKER_{best_index:02d}'
            best_index = len(self._clusters)
            self._clusters.append(_Cluster(centroid=value, observations=1))
        else:
            cluster = self._clusters[best_index]
            combined = cluster.centroid * cluster.observations + value
            cluster.centroid = _normalized_embedding(combined)
            cluster.observations += 1

        self._next_window_index += 1
        return f'SPEAKER_{best_index:02d}'


def build_window_speaker_clusterer(*, requested_speakers: Optional[int] = None) -> WindowSpeakerClusterer:
    """Build the production local clusterer and fail before any HTTP/client call."""

    if sensevoice_speaker_mode() != 'window_clustering':
        raise SenseVoiceSpeakerError('speaker_clustering_not_selected')
    try:
        if validate_speaker_embedding_configuration() != 'sherpa_onnx':
            raise SenseVoiceSpeakerError('local_speaker_embedding_required')
    except SpeakerEmbeddingUnavailable as exc:
        raise SenseVoiceSpeakerError('speaker_embedding_unavailable') from exc

    configured_max = _bounded_int(
        SENSEVOICE_SPEAKER_MAX_CLUSTERS_ENV,
        _DEFAULT_MAX_CLUSTERS,
        minimum=1,
        maximum=_MAX_CLUSTER_LIMIT,
    )
    if requested_speakers is not None:
        if requested_speakers <= 0 or requested_speakers > configured_max:
            raise SenseVoiceSpeakerError('invalid_speaker_count', invalid_input=True)
        configured_max = requested_speakers
    threshold = _bounded_float(
        SENSEVOICE_SPEAKER_THRESHOLD_ENV,
        _DEFAULT_THRESHOLD,
        minimum=0.000001,
        maximum=2.0,
    )
    return WindowSpeakerClusterer(
        threshold=threshold,
        max_clusters=configured_max,
        embedding_extractor=extract_embedding_from_bytes,
    )
