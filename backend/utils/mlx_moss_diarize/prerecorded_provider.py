"""Bounded prerecorded STT adapter for an operator-owned mlx-audio service."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import wave
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union, cast
from urllib.parse import urlsplit

import httpx
import numpy as np
from pydub import AudioSegment  # type: ignore[reportMissingImports]  # pydub is untyped

from config.prerecorded_stt import (
    MlxMossDiarizeConfig,
    PrerecordedSTTService,
    TranscriptionOutcome,
    get_mlx_moss_diarize_config,
    is_private_operator_hostname,
)
from config.stt_provider_policy import normalized_stt_language
from utils.executors import run_blocking, sync_executor
from utils.http_client import (
    UnsafeWebhookURLError,
    get_stt_client,
    get_stt_semaphore,
    get_webhook_circuit_breaker,
    safe_request_target,
)
from utils.observability.fallback import record_fallback
from utils.stt.outcomes import TranscriptionFailure
from utils.stt.speaker_embedding import (
    MIN_EMBEDDING_AUDIO_DURATION,
    SPEAKER_MATCH_THRESHOLD,
    SpeakerEmbeddingUnavailable,
    async_extract_embedding_from_bytes,
    compare_embeddings,
    validate_speaker_embedding_configuration,
)

MAX_AUDIO_BYTES = 256 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 6 * 60 * 60
REQUEST_CHUNK_DURATION_SECONDS = 4 * 60
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SEGMENTS_PER_CHUNK = 4096
MAX_TOTAL_SEGMENTS = 32768
MAX_TOKENS = 8192
MAX_HOTWORDS = 100
MAX_HOTWORD_CHARACTERS = 100
MAX_CONTEXT_CHARACTERS = 4000
MAX_EMBEDDING_CLIP_SECONDS = 30.0
MAX_GLOBAL_SPEAKERS = 32
PER_REQUEST_TIMEOUT_SECONDS = 300.0
TOTAL_TRANSCRIPTION_TIMEOUT_SECONDS = 30 * 60.0
DOWNLOAD_TIMEOUT_SECONDS = 120.0
_TIMESTAMP_TOLERANCE_SECONDS = 0.25
_SPEAKER_PATTERN = re.compile(r'^S(?P<number>[0-9]{1,4})$')


class MlxMossDiarizeResponseError(RuntimeError):
    """The configured mlx-audio authority returned an invalid bounded wire response."""


@dataclass(frozen=True)
class _AudioChunk:
    index: int
    offset_seconds: float
    duration_seconds: float
    wav_bytes: bytes


def _invalid_input(error: BaseException | None = None) -> TranscriptionFailure:
    failure = TranscriptionFailure(
        TranscriptionOutcome.INVALID_INPUT,
        provider=PrerecordedSTTService.MLX_MOSS_DIARIZE,
        retryable=False,
    )
    if error is not None:
        failure.__cause__ = error
    return failure


def _upstream_failure(error: BaseException | None = None) -> TranscriptionFailure:
    failure = TranscriptionFailure(
        TranscriptionOutcome.UPSTREAM_ERROR,
        provider=PrerecordedSTTService.MLX_MOSS_DIARIZE,
    )
    if error is not None:
        failure.__cause__ = error
    return failure


def _timeout_failure(error: BaseException | None = None) -> TranscriptionFailure:
    failure = TranscriptionFailure(
        TranscriptionOutcome.TIMEOUT,
        provider=PrerecordedSTTService.MLX_MOSS_DIARIZE,
    )
    if error is not None:
        failure.__cause__ = error
    return failure


def _config_failure(error: BaseException | None = None) -> TranscriptionFailure:
    failure = TranscriptionFailure(
        TranscriptionOutcome.CONFIG_ERROR,
        provider=PrerecordedSTTService.MLX_MOSS_DIARIZE,
        retryable=False,
    )
    if error is not None:
        failure.__cause__ = error
    return failure


def _bounded_context(keywords: Optional[Sequence[str]]) -> str | None:
    if keywords is None:
        return None
    if isinstance(keywords, (str, bytes)):
        raise _invalid_input()
    unique: List[str] = []
    seen: set[str] = set()
    for raw_keyword in keywords:
        keyword = ' '.join(raw_keyword.split())
        if not keyword:
            continue
        if ',' in keyword or len(keyword) > MAX_HOTWORD_CHARACTERS or any(ord(character) < 32 for character in keyword):
            raise _invalid_input()
        if keyword in seen:
            continue
        seen.add(keyword)
        unique.append(keyword)
        if len(unique) > MAX_HOTWORDS:
            raise _invalid_input()
    context = ','.join(unique)
    if len(context) > MAX_CONTEXT_CHARACTERS:
        raise _invalid_input()
    return context or None


def _bounded_language(language: Optional[str]) -> str | None:
    normalized = normalized_stt_language(language)
    if not normalized or normalized == 'multi':
        return None
    if re.fullmatch(r'[a-z]{2,3}', normalized) is None:
        raise _invalid_input()
    return normalized


def _context_for_mode(keywords: Optional[Sequence[str]], *, diarize: bool) -> str | None:
    context = _bounded_context(keywords)
    if context is None or not diarize:
        return context
    # The installed mlx-audio hotword patch currently changes verbose_json to
    # a flat segment without speaker_id when used with the MOSS Diarize model.
    # Preserve the explicitly requested diarization contract without a second
    # outbound attempt, and make the vocabulary degradation operationally loud.
    record_fallback(
        component='stt_selection',
        from_mode='mlx_moss_diarization_with_context',
        to_mode='mlx_moss_diarization_without_context',
        reason='capability_mismatch',
        outcome='degraded',
    )
    return None


def _format_hint(encoding: Optional[str]) -> Optional[str]:
    normalized = (encoding or '').strip().lower().split(';', 1)[0]
    if '/' in normalized:
        normalized = normalized.split('/', 1)[1]
    return {'mpeg': 'mp3', 'x-wav': 'wav', 'wave': 'wav'}.get(normalized, normalized or None)


def _is_raw_pcm(encoding: Optional[str]) -> bool:
    return (encoding or '').strip().lower() in {'linear16', 'pcm', 'pcm16', 's16le', 'audio/pcm'}


def _decode_audio(
    audio_bytes: bytes,
    *,
    sample_rate: int,
    channels: int,
    encoding: Optional[str],
) -> AudioSegment:
    if not audio_bytes or len(audio_bytes) > MAX_AUDIO_BYTES:
        raise _invalid_input()
    if sample_rate <= 0 or channels <= 0:
        raise _invalid_input()
    try:
        if _is_raw_pcm(encoding):
            if len(audio_bytes) % (2 * channels) != 0:
                raise ValueError('PCM16 byte count is not frame-aligned')
            decoded = AudioSegment(
                data=audio_bytes,
                sample_width=2,
                frame_rate=sample_rate,
                channels=channels,
            )
        else:
            decoded = AudioSegment.from_file(BytesIO(audio_bytes), format=_format_hint(encoding))
        prepared = decoded.set_channels(1).set_frame_rate(16000).set_sample_width(2)
    except TranscriptionFailure:
        raise
    except Exception as error:
        raise _invalid_input(error) from error
    if len(prepared) / 1000.0 > MAX_AUDIO_DURATION_SECONDS:
        raise _invalid_input()
    return prepared


def _wav_bytes(segment: AudioSegment) -> bytes:
    output = BytesIO()
    raw_data = segment.raw_data
    if not isinstance(raw_data, bytes):
        raise _invalid_input()
    with wave.open(output, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(raw_data)
    return output.getvalue()


def _audio_chunks(prepared: AudioSegment) -> List[_AudioChunk]:
    chunk_milliseconds = REQUEST_CHUNK_DURATION_SECONDS * 1000
    chunks: List[_AudioChunk] = []
    for index, start_ms in enumerate(range(0, len(prepared), chunk_milliseconds)):
        chunk = cast(AudioSegment, prepared[start_ms : start_ms + chunk_milliseconds])
        chunks.append(
            _AudioChunk(
                index=index,
                offset_seconds=start_ms / 1000.0,
                duration_seconds=len(chunk) / 1000.0,
                wav_bytes=_wav_bytes(chunk),
            )
        )
    return chunks


def _origin_identity(url: str) -> tuple[str, str, int | None] | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return parsed.scheme, parsed.hostname.rstrip('.').lower(), port


def _download_request_target(audio_url: str) -> tuple[str, Dict[str, Any]]:
    parsed = urlsplit(audio_url)
    hostname = (parsed.hostname or '').rstrip('.').lower()
    if (
        parsed.scheme not in {'http', 'https'}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
    ):
        raise _invalid_input()

    if is_private_operator_hostname(hostname):
        requested_origin = _origin_identity(audio_url)
        configured_origins = {
            identity
            for identity in (
                _origin_identity(os.getenv('MINIO_PUBLIC_ENDPOINT', '').strip()),
                _origin_identity(os.getenv('MINIO_ENDPOINT', '').strip()),
            )
            if identity is not None
        }
        if requested_origin not in configured_origins:
            raise _invalid_input()
        return audio_url, {}

    # Public object URLs must use HTTPS and are pinned to the address that
    # passed the public-host check, closing the DNS-rebinding gap.
    if parsed.scheme != 'https':
        raise _invalid_input()
    try:
        return safe_request_target(audio_url)
    except UnsafeWebhookURLError as error:
        raise _invalid_input(error) from error


async def _bounded_response_body(response: httpx.Response) -> bytes:
    content_length = response.headers.get('content-length')
    if content_length:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise MlxMossDiarizeResponseError('response exceeds configured byte limit')
        except ValueError as error:
            raise MlxMossDiarizeResponseError('response content-length is invalid') from error
    body = bytearray()
    async for part in response.aiter_bytes():
        body.extend(part)
        if len(body) > MAX_RESPONSE_BYTES:
            raise MlxMossDiarizeResponseError('response exceeds configured byte limit')
    return bytes(body)


def _segment_speaker(speaker_id: str, chunk_index: int) -> str:
    match = _SPEAKER_PATTERN.fullmatch(speaker_id)
    if match is None:
        raise MlxMossDiarizeResponseError('segment speaker_id is invalid')
    local_speaker = int(match.group('number'))
    if chunk_index == 0:
        return f'SPEAKER_{local_speaker:02d}'
    # MOSS speaker IDs are local to one request. Namespace later chunks so S01
    # from two chunks is never asserted to be the same person. A separate local
    # embedding reconciliation step may merge these identities later.
    return f'SPEAKER_{chunk_index * 10000 + local_speaker:05d}'


def _strip_matching_speaker_prefix(text: str, speaker_id: str) -> str:
    return re.sub(rf'^\[{re.escape(speaker_id)}\]\s*', '', text.strip(), count=1).strip()


def _parse_segments(payload: bytes, chunk: _AudioChunk, *, diarize: bool) -> List[Dict[str, Any]]:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MlxMossDiarizeResponseError('response is not valid JSON') from error
    if not isinstance(document, dict) or not isinstance(document.get('segments'), list):
        raise MlxMossDiarizeResponseError('response is missing segments')
    raw_segments = document['segments']
    if len(raw_segments) > MAX_SEGMENTS_PER_CHUNK:
        raise MlxMossDiarizeResponseError('response exceeds segment limit')

    parsed: List[Dict[str, Any]] = []
    previous_start = -1.0
    previous_end = -1.0
    maximum_end = 0.0
    for raw_segment in raw_segments:
        required_keys = {'start', 'end', 'text'}
        allowed_keys = required_keys | {'speaker_id'}
        if (
            not isinstance(raw_segment, dict)
            or not required_keys.issubset(raw_segment)
            or not set(raw_segment).issubset(allowed_keys)
            or (diarize and 'speaker_id' not in raw_segment)
        ):
            raise MlxMossDiarizeResponseError('segment shape is invalid')
        start = raw_segment['start']
        end = raw_segment['end']
        speaker_id = raw_segment.get('speaker_id')
        text = raw_segment['text']
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
        ):
            raise MlxMossDiarizeResponseError('segment timestamps are invalid')
        start = float(start)
        end = float(end)
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0.0
            or end < start
            or start < previous_start
            or start < previous_end - _TIMESTAMP_TOLERANCE_SECONDS
            or end > chunk.duration_seconds + _TIMESTAMP_TOLERANCE_SECONDS
        ):
            raise MlxMossDiarizeResponseError('segment timestamps are out of bounds')
        if speaker_id is not None and (
            not isinstance(speaker_id, str) or _SPEAKER_PATTERN.fullmatch(speaker_id) is None
        ):
            raise MlxMossDiarizeResponseError('segment speaker_id is missing or invalid')
        if diarize and speaker_id is None:
            raise MlxMossDiarizeResponseError('segment speaker_id is missing or invalid')
        if not isinstance(text, str):
            raise MlxMossDiarizeResponseError('segment text is invalid')
        normalized_text = _strip_matching_speaker_prefix(text, speaker_id) if speaker_id is not None else text.strip()
        if not normalized_text:
            raise MlxMossDiarizeResponseError('segment text is empty')
        parsed.append(
            {
                'timestamp': [start + chunk.offset_seconds, end + chunk.offset_seconds],
                'speaker': _segment_speaker(speaker_id, chunk.index) if diarize and speaker_id else 'SPEAKER_00',
                'text': normalized_text,
            }
        )
        previous_start = start
        previous_end = end
        maximum_end = max(maximum_end, end)

    # `total_time` is provider processing latency, not audio duration. Segment
    # duration is derived only from max(end); the bounded check above anchors it
    # to the submitted chunk without trusting any top-level timing field.
    _ = maximum_end
    return parsed


class MlxMossDiarizePrerecordedProvider:
    """Use multipart mlx-audio batch transcription without hosted-MOSS fallback."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Client construction remains lazy: a selected but misconfigured provider
        # fails before get_stt_client() creates a pool or opens a connection.
        self._client = client

    def _client_for_request(self) -> httpx.AsyncClient:
        return self._client or get_stt_client()

    async def _transcribe_chunk(
        self,
        config: MlxMossDiarizeConfig,
        chunk: _AudioChunk,
        *,
        context: str | None,
        language: str | None,
        diarize: bool,
    ) -> List[Dict[str, Any]]:
        circuit = get_webhook_circuit_breaker(config.endpoint)
        if not circuit.allow_request():
            raise _upstream_failure()
        headers = {'Authorization': f'Bearer {config.api_key}'} if config.api_key else {}
        try:
            async with asyncio.timeout(PER_REQUEST_TIMEOUT_SECONDS):
                async with get_stt_semaphore():
                    request_data = {
                        'model': config.model,
                        'response_format': 'verbose_json',
                        'max_tokens': str(MAX_TOKENS),
                    }
                    if context is not None:
                        request_data['context'] = context
                    if language is not None:
                        request_data['language'] = language
                    async with self._client_for_request().stream(
                        'POST',
                        config.endpoint,
                        headers=headers,
                        files={'file': (f'chunk-{chunk.index:04d}.wav', chunk.wav_bytes, 'audio/wav')},
                        data=request_data,
                    ) as response:
                        if response.status_code in {401, 403, 404}:
                            circuit.record_success()
                            raise _config_failure()
                        if response.status_code in {400, 413, 422}:
                            circuit.record_success()
                            raise _invalid_input()
                        response.raise_for_status()
                        body = await _bounded_response_body(response)
            segments = _parse_segments(body, chunk, diarize=diarize)
        except (TimeoutError, httpx.TimeoutException) as error:
            circuit.record_failure()
            raise _timeout_failure(error) from error
        except TranscriptionFailure as error:
            if error.retryable:
                circuit.record_failure()
            raise
        except (httpx.HTTPError, MlxMossDiarizeResponseError) as error:
            circuit.record_failure()
            raise _upstream_failure(error) from error
        except Exception as error:
            circuit.record_failure()
            raise _upstream_failure(error) from error
        circuit.record_success()
        return segments

    @staticmethod
    def _speaker_chunk_index(speaker: str) -> int:
        try:
            value = int(speaker.split('_', 1)[1])
        except (IndexError, ValueError) as error:
            raise MlxMossDiarizeResponseError('internal speaker label is invalid') from error
        return value // 10000 if value >= 10000 else 0

    @staticmethod
    def _speaker_clip(prepared: AudioSegment, segments: Sequence[Dict[str, Any]]) -> bytes:
        clip = AudioSegment.empty()
        maximum_milliseconds = int(MAX_EMBEDDING_CLIP_SECONDS * 1000)
        for segment in segments:
            start_ms = max(0, int(float(segment['timestamp'][0]) * 1000))
            end_ms = min(len(prepared), int(float(segment['timestamp'][1]) * 1000))
            if end_ms <= start_ms:
                continue
            remaining = maximum_milliseconds - len(clip)
            if remaining <= 0:
                break
            clip += prepared[start_ms : min(end_ms, start_ms + remaining)]
        if len(clip) < int(MIN_EMBEDDING_AUDIO_DURATION * 1000):
            raise MlxMossDiarizeResponseError('speaker has insufficient audio for cross-chunk reconciliation')
        return _wav_bytes(clip)

    async def _reconcile_chunk_speakers(
        self,
        prepared: AudioSegment,
        words: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for word in words:
            grouped.setdefault(str(word['speaker']), []).append(word)

        clusters: List[np.ndarray[Any, Any]] = []
        observations: List[int] = []
        assigned_by_chunk: Dict[int, set[int]] = {}
        mapping: Dict[str, str] = {}
        for speaker, speaker_segments in grouped.items():
            chunk_index = self._speaker_chunk_index(speaker)
            try:
                embedding = await async_extract_embedding_from_bytes(
                    self._speaker_clip(prepared, speaker_segments),
                    filename=f'mlx-moss-speaker-{chunk_index:04d}.wav',
                )
            except MlxMossDiarizeResponseError:
                raise
            except (SpeakerEmbeddingUnavailable, ValueError, TypeError) as error:
                raise MlxMossDiarizeResponseError('speaker embedding is unavailable') from error
            except Exception as error:
                raise MlxMossDiarizeResponseError('speaker embedding failed') from error

            used_in_chunk = assigned_by_chunk.setdefault(chunk_index, set())
            best_index: int | None = None
            best_distance = math.inf
            for index, centroid in enumerate(clusters):
                if index in used_in_chunk:
                    continue
                distance = compare_embeddings(embedding, centroid)
                if math.isfinite(distance) and distance < best_distance:
                    best_index = index
                    best_distance = distance
            if best_index is None or best_distance >= SPEAKER_MATCH_THRESHOLD:
                if len(clusters) >= MAX_GLOBAL_SPEAKERS:
                    raise MlxMossDiarizeResponseError('speaker count exceeds reconciliation limit')
                best_index = len(clusters)
                clusters.append(embedding)
                observations.append(1)
            else:
                count = observations[best_index]
                combined = clusters[best_index] * count + embedding
                norm = float(np.linalg.norm(combined.astype(np.float64)))
                if not math.isfinite(norm) or norm <= 0:
                    raise MlxMossDiarizeResponseError('speaker centroid is invalid')
                clusters[best_index] = combined / norm
                observations[best_index] = count + 1
            used_in_chunk.add(best_index)
            mapping[speaker] = f'SPEAKER_{best_index:02d}'

        for word in words:
            word['speaker'] = mapping[str(word['speaker'])]
        return words

    async def _transcribe_prepared(
        self,
        config: MlxMossDiarizeConfig,
        prepared: AudioSegment,
        *,
        context: str | None,
        language: str | None,
        diarize: bool,
    ) -> List[Dict[str, Any]]:
        chunks = _audio_chunks(prepared)
        if diarize and len(chunks) > 1:
            try:
                if validate_speaker_embedding_configuration() != 'sherpa_onnx':
                    raise SpeakerEmbeddingUnavailable('local sherpa_onnx speaker embedding is required')
            except SpeakerEmbeddingUnavailable as error:
                raise _config_failure(error) from error
        words: List[Dict[str, Any]] = []
        try:
            async with asyncio.timeout(TOTAL_TRANSCRIPTION_TIMEOUT_SECONDS):
                for chunk in chunks:
                    words.extend(
                        await self._transcribe_chunk(
                            config,
                            chunk,
                            context=context,
                            language=language,
                            diarize=diarize,
                        )
                    )
                    if len(words) > MAX_TOTAL_SEGMENTS:
                        raise MlxMossDiarizeResponseError('transcription exceeds total segment limit')
                if diarize and len(chunks) > 1:
                    words = await self._reconcile_chunk_speakers(prepared, words)
        except TimeoutError as error:
            raise _timeout_failure(error) from error
        except MlxMossDiarizeResponseError as error:
            raise _upstream_failure(error) from error
        return words

    async def _download_audio(self, audio_url: str) -> bytes:
        try:
            request_url, request_kwargs = await run_blocking(sync_executor, _download_request_target, audio_url)
            async with asyncio.timeout(DOWNLOAD_TIMEOUT_SECONDS):
                async with get_stt_semaphore():
                    async with self._client_for_request().stream('GET', request_url, **request_kwargs) as response:
                        response.raise_for_status()
                        content_length = response.headers.get('content-length')
                        if content_length and int(content_length) > MAX_AUDIO_BYTES:
                            raise _invalid_input()
                        body = bytearray()
                        async for part in response.aiter_bytes():
                            body.extend(part)
                            if len(body) > MAX_AUDIO_BYTES:
                                raise _invalid_input()
            return bytes(body)
        except TranscriptionFailure:
            raise
        except (TimeoutError, httpx.TimeoutException) as error:
            raise _timeout_failure(error) from error
        except (httpx.HTTPError, ValueError) as error:
            raise _upstream_failure(error) from error

    def transcribe_url(
        self,
        audio_url: str,
        speakers_count: Optional[int] = None,
        attempts: int = 0,
        return_language: bool = False,
        diarize: bool = True,
        language: Optional[str] = None,
        keywords: Optional[Sequence[str]] = None,
    ) -> Union[List[Dict[str, Any]], Tuple[List[Dict[str, Any]], str]]:
        config = get_mlx_moss_diarize_config()
        context = _context_for_mode(keywords, diarize=diarize)
        normalized_language = _bounded_language(language)

        async def run() -> List[Dict[str, Any]]:
            audio_bytes = await self._download_audio(audio_url)
            prepared = _decode_audio(
                audio_bytes,
                sample_rate=16000,
                channels=1,
                encoding=None,
            )
            return await self._transcribe_prepared(
                config,
                prepared,
                context=context,
                language=normalized_language,
                diarize=diarize,
            )

        words = asyncio.run(run())
        return (words, language or 'multi') if return_language else words

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        sample_rate: int = 16000,
        diarize: bool = True,
        attempts: int = 0,
        encoding: Optional[str] = None,
        channels: int = 1,
        language: Optional[str] = None,
        return_language: bool = False,
        keywords: Optional[Sequence[str]] = None,
    ) -> Union[List[Dict[str, Any]], Tuple[List[Dict[str, Any]], str]]:
        config = get_mlx_moss_diarize_config()
        context = _context_for_mode(keywords, diarize=diarize)
        normalized_language = _bounded_language(language)
        prepared = _decode_audio(
            audio_bytes,
            sample_rate=sample_rate,
            channels=channels,
            encoding=encoding,
        )
        words = asyncio.run(
            self._transcribe_prepared(
                config,
                prepared,
                context=context,
                language=normalized_language,
                diarize=diarize,
            )
        )
        return (words, language or 'multi') if return_language else words
