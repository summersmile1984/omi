"""Local pre-recorded STT adapter for SenseVoice."""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import httpx

from utils.sensevoice.socket import decode_pcm, get_sensevoice_recognizer
from utils.sensevoice.speaker import (
    build_window_speaker_clusterer,
    sensevoice_speaker_mode,
    speaker_window_seconds,
    validate_prerecorded_audio_bound,
)
from fork.egress_policy import assert_http_endpoint_allowed
from utils.stt.speaker_embedding import MIN_EMBEDDING_AUDIO_DURATION
from utils.stt.pre_recorded import PrerecordedSTTProvider

_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


def _as_pcm16_mono(
    audio_bytes: bytes, *, sample_rate: int, channels: int, encoding: Optional[str]
) -> Tuple[bytes, int]:
    from fork.speech import MAX_AUDIO_SECONDS
    from config.prerecorded_stt import TranscriptionOutcome
    from utils.stt.outcomes import TranscriptionFailure

    def invalid():
        return TranscriptionFailure(TranscriptionOutcome.INVALID_INPUT, provider='sensevoice', retryable=False)

    normalized = (encoding or '').strip().lower()
    if (
        not audio_bytes
        or len(audio_bytes) > 8 * 1024 * 1024
        or not 8000 <= sample_rate <= 48000
        or channels not in (1, 2)
    ):
        raise invalid()
    if normalized in {'linear16', 'pcm', 'pcm16', 's16le'}:
        if len(audio_bytes) % (2 * channels) or len(audio_bytes) > sample_rate * channels * 2 * MAX_AUDIO_SECONDS:
            raise invalid()
        if channels == 1:
            return audio_bytes, sample_rate
        import numpy as np

        samples = np.frombuffer(audio_bytes, dtype='<i2').reshape(-1, 2).astype(np.int32).mean(axis=1)
        return samples.astype('<i2').tobytes(), sample_rate
    try:
        # Bound decoded output before buffering it; compressed input cannot turn
        # into an unbounded AudioSegment. No nested network protocols are allowed.
        result = subprocess.run(
            [
                'ffmpeg',
                '-v',
                'error',
                '-nostdin',
                '-protocol_whitelist',
                'pipe',
                '-i',
                'pipe:0',
                '-t',
                str(MAX_AUDIO_SECONDS + 0.1),
                '-f',
                's16le',
                '-ac',
                '1',
                '-ar',
                '16000',
                'pipe:1',
            ],
            input=audio_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=True,
        )
    except subprocess.TimeoutExpired as error:
        raise TranscriptionFailure(TranscriptionOutcome.TIMEOUT, provider='sensevoice') from error
    except (OSError, subprocess.CalledProcessError) as error:
        raise invalid() from error
    pcm = result.stdout
    if not pcm or len(pcm) > 16000 * 2 * MAX_AUDIO_SECONDS:
        raise invalid()
    return pcm, 16000


def _bounded_pcm_windows(pcm: bytes, sample_rate: int) -> List[Tuple[int, bytes]]:
    """Split PCM into bounded ASR/embedding windows, merging a tiny tail."""

    validate_prerecorded_audio_bound(len(pcm), sample_rate)
    window_bytes = max(2, int(speaker_window_seconds() * sample_rate) * 2)
    minimum_bytes = max(2, int(MIN_EMBEDDING_AUDIO_DURATION * sample_rate) * 2)
    windows = [(offset, pcm[offset : offset + window_bytes]) for offset in range(0, len(pcm), window_bytes)]
    if len(windows) > 1 and len(windows[-1][1]) < minimum_bytes:
        tail_offset, tail = windows.pop()
        previous_offset, previous = windows[-1]
        if previous_offset + len(previous) != tail_offset:
            raise RuntimeError('SenseVoice PCM window planner produced a gap')
        windows[-1] = (previous_offset, previous + tail)
    return windows


class SenseVoicePrerecordedProvider(PrerecordedSTTProvider):
    """Run the offline SenseVoice recognizer on complete recordings."""

    def __init__(self, recognizer: Any = None) -> None:
        self._recognizer = recognizer

    def _transcribe_bytes(
        self,
        audio_bytes: bytes,
        *,
        sample_rate: int,
        channels: int,
        encoding: Optional[str],
        language: Optional[str],
        diarize: bool,
        speakers_count: Optional[int],
    ) -> Tuple[List[Dict[str, Any]], str]:
        from fork.speech import language as validate_language
        from config.prerecorded_stt import TranscriptionOutcome
        from utils.stt.outcomes import TranscriptionFailure

        validate_language(language)
        if speakers_count is not None and speakers_count > 1:
            raise TranscriptionFailure(TranscriptionOutcome.INVALID_INPUT, provider='sensevoice', retryable=False)
        pcm, prepared_rate = _as_pcm16_mono(
            audio_bytes,
            sample_rate=sample_rate,
            channels=channels,
            encoding=encoding,
        )
        recognizer = self._recognizer or get_sensevoice_recognizer()
        if not diarize or sensevoice_speaker_mode() == 'single_speaker':
            text = decode_pcm(recognizer, prepared_rate, pcm)
            duration = len(pcm) / max(1, 2 * prepared_rate)
            words = [] if not text else [{'timestamp': [0.0, duration], 'speaker': 'SPEAKER_00', 'text': text}]
            return words, language or 'multi'

        windows = _bounded_pcm_windows(pcm, prepared_rate)
        clusterer = build_window_speaker_clusterer(requested_speakers=speakers_count)
        words = []
        speaker_window_index = 0
        for offset, window in windows:
            text = decode_pcm(recognizer, prepared_rate, window)
            if not text:
                continue
            speaker = clusterer.assign_pcm(speaker_window_index, window, prepared_rate)
            speaker_window_index += 1
            start = offset / (2 * prepared_rate)
            end = (offset + len(window)) / (2 * prepared_rate)
            words.append({'timestamp': [start, end], 'speaker': speaker, 'text': text})
        return words, language or 'multi'

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
        self._validate_keywords(keywords)
        # This synchronous download does not use the shared HTTP client pool;
        # enforce the same neutral/self-host egress policy before handing a
        # caller-controlled audio URL to httpx.
        assert_http_endpoint_allowed(audio_url)
        # Reject redirects, so an admitted signed URL cannot change authority.
        with httpx.stream('GET', audio_url, timeout=_DOWNLOAD_TIMEOUT, follow_redirects=False) as response:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError('speech audio redirects are not allowed')
            chunks, length = [], 0
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > 8 * 1024 * 1024:
                    raise ValueError('speech audio download exceeds input bound')
                chunks.append(chunk)
            content_type = response.headers.get('content-type')
        words, detected_language = self._transcribe_bytes(
            b''.join(chunks),
            sample_rate=16000,
            channels=1,
            encoding=content_type,
            language=language,
            diarize=diarize,
            speakers_count=speakers_count,
        )
        return (words, detected_language) if return_language else words

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
        self._validate_keywords(keywords)
        words, detected_language = self._transcribe_bytes(
            audio_bytes,
            sample_rate=sample_rate,
            channels=channels,
            encoding=encoding,
            language=language,
            diarize=diarize,
            speakers_count=None,
        )
        return (words, detected_language) if return_language else words

    @staticmethod
    def _validate_keywords(keywords):
        if keywords:
            from config.prerecorded_stt import TranscriptionOutcome
            from utils.stt.outcomes import TranscriptionFailure

            raise TranscriptionFailure(TranscriptionOutcome.INVALID_INPUT, provider='sensevoice', retryable=False)
