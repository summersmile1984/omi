"""Deployment-selected local or OpenAI-compatible text-to-speech boundary.

The Sherpa transport runs a mounted VITS model without networking. The
compatible transport deliberately has no vendor default: a deployment must
provide its own base URL, credential, model and voice or the request fails
before an HTTP client or provider URL is constructed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import importlib
import math
import os
from pathlib import Path
import threading
from typing import Any
import wave
import weakref
from urllib.parse import urlparse

import httpx
import numpy as np
from pydub import AudioSegment

from utils.egress_policy import EgressPolicyUnavailable
from utils.executors import llm_executor, run_blocking
from utils.http_client import get_tts_client, get_tts_semaphore, get_webhook_circuit_breaker
from utils.llm.capabilities import ModelCapabilityUnavailableError

_MAX_AUDIO_BYTES = 32 * 1024 * 1024
_SUPPORTED_FORMATS = frozenset({'aac', 'flac', 'mp3', 'opus', 'pcm', 'wav'})
_SHERPA_SUPPORTED_FORMATS = frozenset({'mp3', 'pcm', 'wav'})

_sherpa_lock = threading.Lock()
_sherpa_generation_lock = threading.Lock()
_sherpa_async_locks_guard = threading.Lock()
_sherpa_async_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()
_sherpa_identity: tuple[str, str, str, int] | None = None
_sherpa_engine: object | None = None


@dataclass(frozen=True)
class TtsAudio:
    content: bytes
    media_type: str


@dataclass(frozen=True)
class OpenAICompatibleTtsConfig:
    endpoint: str
    api_key: str
    model: str
    default_voice: str
    timeout_seconds: float


@dataclass(frozen=True)
class SherpaTtsConfig:
    model: str
    tokens: str
    data_dir: str
    num_threads: int
    speaker_id: int


def selected_tts_provider() -> str:
    """Return the authoritative provider token at the request boundary."""

    return os.getenv('TTS_PROVIDER', '').strip().lower()


def _required_env(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise ModelCapabilityUnavailableError('tts', f'{name.lower()}_not_configured', retryable=False)
    return value


def _compatible_endpoint(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ModelCapabilityUnavailableError('tts', 'invalid_compatible_endpoint', retryable=False)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelCapabilityUnavailableError('tts', 'invalid_compatible_endpoint', retryable=False)
    return f"{base_url.rstrip('/')}/audio/speech"


def resolve_openai_compatible_tts_config() -> OpenAICompatibleTtsConfig:
    """Resolve an explicit compatible endpoint without any official fallback."""

    base_url = _required_env('TTS_OPENAI_COMPATIBLE_BASE_URL')
    api_key = _required_env('TTS_OPENAI_COMPATIBLE_API_KEY')
    model = _required_env('TTS_OPENAI_COMPATIBLE_MODEL')
    default_voice = _required_env('TTS_OPENAI_COMPATIBLE_VOICE')
    try:
        timeout_seconds = float(os.getenv('TTS_OPENAI_COMPATIBLE_TIMEOUT_SECONDS', '60'))
    except ValueError as exc:
        raise ModelCapabilityUnavailableError('tts', 'invalid_compatible_timeout', retryable=False) from exc
    if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 180:
        raise ModelCapabilityUnavailableError('tts', 'invalid_compatible_timeout', retryable=False)
    return OpenAICompatibleTtsConfig(
        endpoint=_compatible_endpoint(base_url),
        api_key=api_key,
        model=model,
        default_voice=default_voice,
        timeout_seconds=timeout_seconds,
    )


def _required_file(name: str) -> str:
    path = Path(_required_env(name)).expanduser().resolve()
    if not path.is_file():
        raise ModelCapabilityUnavailableError('tts', f'{name.lower()}_not_readable', retryable=False)
    return str(path)


def resolve_sherpa_tts_config() -> SherpaTtsConfig:
    """Resolve a network-free VITS model mounted by the operator."""

    model = _required_file('TTS_SHERPA_MODEL')
    tokens = _required_file('TTS_SHERPA_TOKENS')
    data_dir = Path(_required_env('TTS_SHERPA_DATA_DIR')).expanduser().resolve()
    if not data_dir.is_dir():
        raise ModelCapabilityUnavailableError('tts', 'tts_sherpa_data_dir_not_readable', retryable=False)
    try:
        num_threads = int(os.getenv('TTS_SHERPA_NUM_THREADS', '2'))
        speaker_id = int(os.getenv('TTS_SHERPA_SPEAKER_ID', '0'))
    except ValueError as exc:
        raise ModelCapabilityUnavailableError('tts', 'invalid_sherpa_configuration', retryable=False) from exc
    if not 1 <= num_threads <= 64 or speaker_id < 0:
        raise ModelCapabilityUnavailableError('tts', 'invalid_sherpa_configuration', retryable=False)
    return SherpaTtsConfig(
        model=model,
        tokens=tokens,
        data_dir=str(data_dir),
        num_threads=num_threads,
        speaker_id=speaker_id,
    )


def _get_sherpa_engine(config: SherpaTtsConfig) -> object:
    global _sherpa_engine, _sherpa_identity

    identity = (config.model, config.tokens, config.data_dir, config.num_threads)
    with _sherpa_lock:
        if _sherpa_engine is not None and _sherpa_identity == identity:
            return _sherpa_engine
        try:
            sherpa_onnx: Any = importlib.import_module('sherpa_onnx')
        except (ImportError, OSError) as exc:
            raise ModelCapabilityUnavailableError('tts', 'sherpa_runtime_unavailable', retryable=False) from exc
        vits: object = sherpa_onnx.OfflineTtsVitsModelConfig(  # type: ignore[reportUnknownMemberType]
            model=config.model,
            tokens=config.tokens,
            data_dir=config.data_dir,
        )
        model_config: object = sherpa_onnx.OfflineTtsModelConfig(  # type: ignore[reportUnknownMemberType]
            vits=vits,
            num_threads=config.num_threads,
            debug=False,
            provider='cpu',
        )
        tts_config: object = sherpa_onnx.OfflineTtsConfig(model=model_config)  # type: ignore[reportUnknownMemberType]
        if not tts_config.validate():  # type: ignore[reportAttributeAccessIssue]
            raise ModelCapabilityUnavailableError('tts', 'invalid_sherpa_model', retryable=False)
        try:
            engine: object = sherpa_onnx.OfflineTts(tts_config)  # type: ignore[reportUnknownMemberType]
        except Exception as exc:
            raise ModelCapabilityUnavailableError('tts', 'sherpa_model_initialization_failed', retryable=False) from exc
        _sherpa_engine = engine
        _sherpa_identity = identity
        return engine


def _pcm16_wav(pcm: bytes, sample_rate: int) -> bytes:
    output = BytesIO()
    with wave.open(output, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return output.getvalue()


def _encode_sherpa_audio(pcm: bytes, sample_rate: int, audio_format: str) -> TtsAudio:
    if audio_format == 'pcm':
        return TtsAudio(content=pcm, media_type='audio/L16')
    wav_bytes = _pcm16_wav(pcm, sample_rate)
    if audio_format == 'wav':
        return TtsAudio(content=wav_bytes, media_type='audio/wav')
    output = BytesIO()
    try:
        AudioSegment(data=pcm, sample_width=2, frame_rate=sample_rate, channels=1).export(output, format='mp3')
    except Exception as exc:
        raise ModelCapabilityUnavailableError('tts', 'sherpa_audio_encoding_failed', retryable=False) from exc
    encoded = output.getvalue()
    if not encoded or len(encoded) > _MAX_AUDIO_BYTES:
        raise ModelCapabilityUnavailableError('tts', 'provider_invalid_audio', retryable=False)
    return TtsAudio(content=encoded, media_type='audio/mpeg')


def _generate_sherpa_tts(text: str, audio_format: str) -> TtsAudio:
    config = resolve_sherpa_tts_config()
    if audio_format not in _SHERPA_SUPPORTED_FORMATS:
        raise ModelCapabilityUnavailableError('tts', 'unsupported_audio_format', retryable=False)
    engine = _get_sherpa_engine(config)
    try:
        # sherpa-onnx does not promise that one OfflineTts instance is safe for
        # concurrent generate calls. The instance is cached process-wide, so
        # serialize only model inference; format encoding remains concurrent.
        with _sherpa_generation_lock:
            generated: object = engine.generate(  # type: ignore[reportAttributeAccessIssue]
                text,
                sid=config.speaker_id,
                speed=1.0,
            )
        samples = np.asarray(generated.samples, dtype=np.float32).reshape(-1)  # type: ignore[reportAttributeAccessIssue]
        sample_rate = int(generated.sample_rate)  # type: ignore[reportAttributeAccessIssue]
    except ModelCapabilityUnavailableError:
        raise
    except Exception as exc:
        raise ModelCapabilityUnavailableError('tts', 'sherpa_generation_failed', retryable=True) from exc
    if (
        samples.size == 0
        or samples.nbytes > _MAX_AUDIO_BYTES * 2
        or sample_rate < 8000
        or sample_rate > 192000
        or not np.all(np.isfinite(samples))
    ):
        raise ModelCapabilityUnavailableError('tts', 'provider_invalid_audio', retryable=False)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype('<i2').tobytes()
    return _encode_sherpa_audio(pcm, sample_rate, audio_format)


def _sherpa_async_lock() -> asyncio.Lock:
    """Return one loop-local admission lock without leaking retired loops."""

    loop = asyncio.get_running_loop()
    with _sherpa_async_locks_guard:
        lock = _sherpa_async_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _sherpa_async_locks[loop] = lock
        return lock


async def synthesize_sherpa_tts(text: str, *, audio_format: str | None = None) -> TtsAudio:
    """Queue locally, then borrow one LLM worker for the actual inference.

    Waiting requests must not occupy all ``llm_executor`` workers while the
    process-wide Sherpa engine serializes ``generate``. The inner thread lock
    remains the final engine-safety boundary for unusual multi-loop callers.
    """

    normalized = normalize_audio_format(audio_format)
    async with _sherpa_async_lock():
        return await run_blocking(llm_executor, _generate_sherpa_tts, text, normalized)


def normalize_audio_format(value: str | None) -> str:
    """Map legacy client format tokens onto the compatible wire contract."""

    normalized = (value or 'mp3').strip().lower()
    if normalized.startswith('mp3'):
        normalized = 'mp3'
    elif normalized.startswith('pcm'):
        normalized = 'pcm'
    if normalized not in _SUPPORTED_FORMATS:
        raise ModelCapabilityUnavailableError('tts', 'unsupported_audio_format', retryable=False)
    return normalized


def _media_type_for_format(audio_format: str) -> str:
    return {
        'aac': 'audio/aac',
        'flac': 'audio/flac',
        'mp3': 'audio/mpeg',
        'opus': 'audio/ogg',
        'pcm': 'audio/L16',
        'wav': 'audio/wav',
    }[audio_format]


async def synthesize_openai_compatible_tts(
    text: str,
    *,
    voice: str | None = None,
    audio_format: str | None = None,
    instructions: str | None = None,
) -> TtsAudio:
    """Synthesize through an explicitly configured compatible service."""

    config = resolve_openai_compatible_tts_config()
    response_format = normalize_audio_format(audio_format)
    payload: dict[str, object] = {
        'model': config.model,
        'input': text,
        'voice': voice.strip() if voice and voice.strip() else config.default_voice,
        'response_format': response_format,
    }
    if instructions and instructions.strip():
        payload['instructions'] = instructions.strip()

    circuit_breaker = get_webhook_circuit_breaker(config.endpoint)
    if not circuit_breaker.allow_request():
        raise ModelCapabilityUnavailableError('tts', 'transport_circuit_open', retryable=True)

    try:
        async with get_tts_semaphore():
            response = await get_tts_client().post(
                config.endpoint,
                headers={
                    'Authorization': f'Bearer {config.api_key}',
                    'Accept': 'audio/*',
                    'Content-Type': 'application/json',
                },
                json=payload,
                timeout=config.timeout_seconds,
            )
    except EgressPolicyUnavailable as exc:
        circuit_breaker.record_failure()
        raise ModelCapabilityUnavailableError('tts', exc.reason, retryable=False) from exc
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        circuit_breaker.record_failure()
        raise ModelCapabilityUnavailableError('tts', 'transport_unavailable', retryable=True) from exc

    if response.status_code >= 400:
        circuit_breaker.record_failure()
        retryable = response.status_code == 429 or response.status_code >= 500
        raise ModelCapabilityUnavailableError('tts', 'provider_http_error', retryable=retryable)

    media_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if media_type and not media_type.startswith('audio/'):
        circuit_breaker.record_failure()
        raise ModelCapabilityUnavailableError('tts', 'provider_invalid_audio', retryable=False)
    content = response.content
    if not content or len(content) > _MAX_AUDIO_BYTES:
        circuit_breaker.record_failure()
        raise ModelCapabilityUnavailableError('tts', 'provider_invalid_audio', retryable=False)

    circuit_breaker.record_success()
    return TtsAudio(content=content, media_type=media_type or _media_type_for_format(response_format))
